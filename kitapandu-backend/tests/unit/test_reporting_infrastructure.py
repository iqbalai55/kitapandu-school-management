"""Unit tests for the reporting SQL repositories (#49).

Seam: ``ksma.reporting.infrastructure.repositories`` — ``ReportPublicationRepository``,
``CompletionDecisionRepository`` and ``CertificateRepository``.

The session is the ``FakeAsyncSession`` from ``tests/unit/conftest_helpers.py``.
The rows come back as plain dicts, exactly as a Postgres row would arrive, so
the ``or {}`` / ``or []`` fallbacks inside the mapping adapters are observable:
a JSONB document that was really stored must come back out of the adapter, not
be quietly replaced by an empty default.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from ksma.reporting.domain import (
    Certificate,
    CertificateStatus,
    CompletionDecision,
    CompletionOutcome,
    ReportPublication,
    ReportStatus,
)
from ksma.reporting.infrastructure.repositories import (
    CertificateRepository,
    CompletionDecisionRepository,
    ReportPublicationRepository,
)
from tests.unit.conftest_helpers import FakeAsyncSession


def _last_execute(session: FakeAsyncSession, fragment: str) -> Any:
    """The most recent ``execute`` whose SQL mentions ``fragment``."""
    calls = [call for call in session.calls if call.op == "execute" and fragment in call.sql]
    assert calls, f"no execute mentioning {fragment!r}; recorded: {[c.sql[:60] for c in session.calls]}"
    return calls[-1]


class TestReportPublicationRepository:
    async def test_create_serialises_snapshot_and_publication(self) -> None:
        session = FakeAsyncSession()
        attachment_id, supersedes_id = uuid4(), uuid4()
        publication = ReportPublication(
            tenant_id=uuid4(),
            unit_id=uuid4(),
            enrollment_id=uuid4(),
            publication_no=3,
            source_assessment_ids=[uuid4(), uuid4()],
            snapshot={"marks": [80, 90]},
            publication={"issued_on": "2025-06-30"},
            attachment_id=attachment_id,
            supersedes_id=supersedes_id,
            status=ReportStatus.SUPERSEDED,
            created_by="registrar",
        )

        await ReportPublicationRepository(session, publication.tenant_id).create(publication)

        call = session.assert_called(op="execute", sql_contains="CAST(:snap AS JSONB)")
        assert json.loads(call.params["snap"]) == {"marks": [80, 90]}
        assert json.loads(call.params["pub"]) == {"issued_on": "2025-06-30"}
        assert call.params["att"] == str(attachment_id)
        assert call.params["sup"] == str(supersedes_id)
        assert call.params["status"] == "superseded"
        assert call.params["no"] == 3

    async def test_create_without_attachment_or_supersession_stores_null(self) -> None:
        session = FakeAsyncSession()
        publication = ReportPublication(tenant_id=uuid4())

        await ReportPublicationRepository(session, publication.tenant_id).create(publication)

        call = session.assert_called(op="execute")
        assert call.params["att"] is None
        assert call.params["sup"] is None

    async def test_source_assessment_ids_survive_the_row_adapter(self) -> None:
        """A non-empty ``uuid[]`` column must not degrade to ``[]``."""
        session = FakeAsyncSession()
        publication_id, tenant_id, enrollment_id = uuid4(), uuid4(), uuid4()
        assessment_ids = [str(uuid4()), str(uuid4())]
        session.seed(
            "report_publication",
            {
                "id": str(publication_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "enrollment_id": str(enrollment_id),
                "publication_no": 1,
                "source_assessment_ids": assessment_ids,
                "snapshot": {},
                "publication": {},
                "attachment_id": None,
                "supersedes_id": None,
                "status": "published",
                "created_at": None,
                "created_by": "registrar",
                "updated_at": None,
                "updated_by": "registrar",
            },
        )

        found = await ReportPublicationRepository(session, tenant_id).get(publication_id)

        assert found is not None
        assert found.source_assessment_ids == assessment_ids

    async def test_absent_source_assessment_ids_read_back_as_an_empty_list(self) -> None:
        session = FakeAsyncSession()
        publication_id, tenant_id = uuid4(), uuid4()
        session.seed(
            "report_publication",
            {
                "id": str(publication_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "enrollment_id": str(uuid4()),
                "publication_no": 1,
                "source_assessment_ids": None,
                "snapshot": {},
                "publication": {},
                "attachment_id": None,
                "supersedes_id": None,
                "status": "published",
                "created_at": None,
                "created_by": "registrar",
                "updated_at": None,
                "updated_by": "registrar",
            },
        )

        found = await ReportPublicationRepository(session, tenant_id).get(publication_id)

        assert found is not None
        assert found.source_assessment_ids == []

    async def test_list_for_enrollment_is_scoped_to_the_enrollment(self) -> None:
        session = FakeAsyncSession()
        tenant_id, enrollment_id, other = uuid4(), uuid4(), uuid4()
        shared = {
            "tenant_id": str(tenant_id),
            "unit_id": str(uuid4()),
            "source_assessment_ids": [],
            "snapshot": {},
            "publication": {},
            "attachment_id": None,
            "supersedes_id": None,
            "status": "published",
            "created_at": None,
            "created_by": "registrar",
            "updated_at": None,
            "updated_by": "registrar",
        }
        session.seed(
            "report_publication",
            [
                {"id": str(uuid4()), "enrollment_id": str(enrollment_id), "publication_no": 1, **shared},
                {"id": str(uuid4()), "enrollment_id": str(enrollment_id), "publication_no": 2, **shared},
                {"id": str(uuid4()), "enrollment_id": str(other), "publication_no": 9, **shared},
            ],
        )

        found = await ReportPublicationRepository(session, tenant_id).list_for_enrollment(enrollment_id)

        assert sorted(p.publication_no for p in found) == [1, 2]
        call = _last_execute(session, "FROM report_publication")
        assert call.params["eid"] == str(enrollment_id)
        assert "ORDER BY publication_no DESC" in call.sql

    async def test_set_revocation_nests_the_payload_under_revocation(self) -> None:
        session = FakeAsyncSession()
        tenant_id, publication_id = uuid4(), uuid4()
        payload = {"reason": "issued in error", "revoked_by": "registrar"}

        await ReportPublicationRepository(session, tenant_id).set_revocation(
            publication_id, payload=payload
        )

        call = session.calls[-1]
        assert "jsonb_build_object('revocation'" in call.sql
        assert json.loads(call.params["p"]) == payload
        assert call.params["id"] == str(publication_id)
        assert call.params["tid"] == str(tenant_id)

    async def test_append_publication_merges_at_the_top_level(self) -> None:
        session = FakeAsyncSession()
        tenant_id, publication_id = uuid4(), uuid4()
        payload = {"amended": True}

        await ReportPublicationRepository(session, tenant_id).append_publication(
            publication_id, payload=payload
        )

        call = session.calls[-1]
        assert "publication = publication || CAST(:p AS JSONB)" in call.sql
        assert json.loads(call.params["p"]) == payload

    async def test_set_supersedes_links_the_new_row_to_the_old_one(self) -> None:
        session = FakeAsyncSession()
        tenant_id, new_id, old_id = uuid4(), uuid4(), uuid4()

        await ReportPublicationRepository(session, tenant_id).set_supersedes(new_id, old_id)

        call = session.calls[-1]
        assert call.params["n"] == str(new_id)
        assert call.params["o"] == str(old_id)
        assert call.params["tid"] == str(tenant_id)

    async def test_update_status_writes_the_status_and_the_actor(self) -> None:
        session = FakeAsyncSession()
        tenant_id, publication_id = uuid4(), uuid4()

        await ReportPublicationRepository(session, tenant_id).update_status(
            publication_id, status=ReportStatus.REVOKED, actor="registrar"
        )

        call = session.calls[-1]
        assert call.params["s"] == "revoked"
        assert call.params["u"] == "registrar"

    async def test_next_publication_no_steps_past_the_current_maximum(self) -> None:
        tenant_id, enrollment_id = uuid4(), uuid4()
        session = FakeAsyncSession(
            {
                "report_publication": [
                    {"tenant_id": str(tenant_id), "enrollment_id": str(enrollment_id), "n": 7}
                ]
            }
        )

        assert (
            await ReportPublicationRepository(session, tenant_id).next_publication_no(enrollment_id) == 8
        )

    async def test_next_publication_no_of_an_empty_history_starts_at_one(self) -> None:
        tenant_id, enrollment_id = uuid4(), uuid4()
        for value in (0, None):
            session = FakeAsyncSession(
                {
                    "report_publication": [
                        {"tenant_id": str(tenant_id), "enrollment_id": str(enrollment_id), "n": value}
                    ]
                }
            )
            repo = ReportPublicationRepository(session, tenant_id)
            assert await repo.next_publication_no(enrollment_id) == 1


class TestCompletionDecisionRepository:
    async def test_create_serialises_evidence_refs_and_decision(self) -> None:
        session = FakeAsyncSession()
        policy_id, supersedes_id = uuid4(), uuid4()
        decision = CompletionDecision(
            tenant_id=uuid4(),
            unit_id=uuid4(),
            enrollment_id=uuid4(),
            outcome=CompletionOutcome.COMPLETED,
            evidence_refs=[{"indicator": "I-1", "value": 90}],
            policy_id=policy_id,
            decision={"rule": "all-required-pass"},
            supersedes_id=supersedes_id,
            created_by="assessor-1",
        )

        await CompletionDecisionRepository(session, decision.tenant_id).create(decision)

        call = session.assert_called(op="execute", sql_contains="CAST(:refs AS JSONB)")
        assert json.loads(call.params["refs"]) == [{"indicator": "I-1", "value": 90}]
        assert json.loads(call.params["dec"]) == {"rule": "all-required-pass"}
        assert call.params["pol"] == str(policy_id)
        assert call.params["sup"] == str(supersedes_id)
        assert call.params["outcome"] == "completed"

    async def test_create_without_policy_stores_null(self) -> None:
        session = FakeAsyncSession()
        decision = CompletionDecision(tenant_id=uuid4(), policy_id=None, supersedes_id=None)

        await CompletionDecisionRepository(session, decision.tenant_id).create(decision)

        call = session.assert_called(op="execute")
        assert call.params["pol"] is None
        assert call.params["sup"] is None

    async def test_evidence_refs_survive_the_row_adapter(self) -> None:
        """A non-empty JSONB array must not degrade to ``[]``."""
        session = FakeAsyncSession()
        decision_id, tenant_id = uuid4(), uuid4()
        refs = [{"indicator": "I-1", "value": 90}, {"indicator": "I-2", "value": 80}]
        session.seed(
            "completion_decision",
            {
                "id": str(decision_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "enrollment_id": str(uuid4()),
                "outcome": "completed",
                "evidence_refs": refs,
                "policy_id": None,
                "decision": {},
                "supersedes_id": None,
                "created_at": None,
                "created_by": "assessor-1",
                "updated_at": None,
                "updated_by": "assessor-1",
            },
        )

        found = await CompletionDecisionRepository(session, tenant_id).get(decision_id)

        assert found is not None
        assert found.evidence_refs == refs
        assert found.outcome is CompletionOutcome.COMPLETED

    async def test_absent_evidence_refs_read_back_as_an_empty_list(self) -> None:
        session = FakeAsyncSession()
        decision_id, tenant_id = uuid4(), uuid4()
        session.seed(
            "completion_decision",
            {
                "id": str(decision_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "enrollment_id": str(uuid4()),
                "outcome": "not_completed",
                "evidence_refs": None,
                "policy_id": None,
                "decision": {},
                "supersedes_id": None,
                "created_at": None,
                "created_by": "assessor-1",
                "updated_at": None,
                "updated_by": "assessor-1",
            },
        )

        found = await CompletionDecisionRepository(session, tenant_id).get(decision_id)

        assert found is not None
        assert found.evidence_refs == []

    async def test_latest_for_enrollment_is_enrollment_scoped(self) -> None:
        session = FakeAsyncSession()
        tenant_id, enrollment_id, other = uuid4(), uuid4(), uuid4()
        shared = {
            "tenant_id": str(tenant_id),
            "unit_id": str(uuid4()),
            "evidence_refs": [],
            "policy_id": None,
            "decision": {},
            "supersedes_id": None,
            "created_at": None,
            "created_by": "assessor-1",
            "updated_at": None,
            "updated_by": "assessor-1",
        }
        session.seed(
            "completion_decision",
            [
                {"id": str(uuid4()), "enrollment_id": str(enrollment_id), "outcome": "completed", **shared},
                {"id": str(uuid4()), "enrollment_id": str(other), "outcome": "not_completed", **shared},
            ],
        )

        found = await CompletionDecisionRepository(session, tenant_id).latest_for_enrollment(
            enrollment_id
        )

        assert found is not None
        assert found.outcome is CompletionOutcome.COMPLETED
        call = _last_execute(session, "FROM completion_decision")
        assert call.params["eid"] == str(enrollment_id)
        assert "ORDER BY created_at DESC LIMIT 1" in call.sql


class TestCertificateRepository:
    async def test_create_serialises_the_snapshot(self) -> None:
        session = FakeAsyncSession()
        certificate = Certificate(
            tenant_id=uuid4(),
            unit_id=uuid4(),
            completion_id=uuid4(),
            numbering_scope="2025",
            certificate_no="CERT-000001",
            snapshot={"student": "A. Student"},
            attachment_id=uuid4(),
            created_by="registrar",
        )

        await CertificateRepository(session, certificate.tenant_id).create(certificate)

        call = session.assert_called(op="execute", sql_contains="CAST(:snap AS JSONB)")
        assert json.loads(call.params["snap"]) == {"student": "A. Student"}
        assert call.params["scope"] == "2025"
        assert call.params["no"] == "CERT-000001"
        assert call.params["status"] == "issued"
        # No revocation on a freshly issued certificate.
        assert call.params["rev"] is None
        assert call.params["repl"] is None

    async def test_get_maps_the_status_enum(self) -> None:
        session = FakeAsyncSession()
        certificate_id, tenant_id, replacement_id = uuid4(), uuid4(), uuid4()
        session.seed(
            "certificate",
            {
                "id": str(certificate_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "completion_id": str(uuid4()),
                "numbering_scope": "2025",
                "certificate_no": "CERT-000007",
                "snapshot": {},
                "attachment_id": None,
                "status": "replaced",
                "replacement_id": str(replacement_id),
                "revocation": None,
                "created_at": None,
                "created_by": "registrar",
            },
        )

        found = await CertificateRepository(session, tenant_id).get(certificate_id)

        assert found is not None
        assert found.status is CertificateStatus.REPLACED
        assert found.certificate_no == "CERT-000007"

    async def test_mark_replaced_retires_the_old_certificate(self) -> None:
        session = FakeAsyncSession()
        tenant_id, old_id, new_id = uuid4(), uuid4(), uuid4()

        await CertificateRepository(session, tenant_id).mark_replaced(old_id, new_id)

        call = session.calls[-1]
        assert "status='replaced'" in call.sql
        assert call.params["o"] == str(old_id)
        assert call.params["n"] == str(new_id)
        assert call.params["tid"] == str(tenant_id)

    async def test_set_revocation_stores_the_payload_and_the_revoked_status(self) -> None:
        session = FakeAsyncSession()
        tenant_id, certificate_id = uuid4(), uuid4()
        payload = {"reason": "duplicate issue", "revoked_by": "registrar"}

        await CertificateRepository(session, tenant_id).set_revocation(
            certificate_id, payload=payload
        )

        call = session.calls[-1]
        assert "status='revoked'" in call.sql
        assert "revocation = CAST(:p AS JSONB)" in call.sql
        assert json.loads(call.params["p"]) == payload
        assert call.params["id"] == str(certificate_id)
        assert call.params["tid"] == str(tenant_id)

    async def test_next_certificate_no_is_zero_padded_and_steps_past_the_max(self) -> None:
        tenant_id = uuid4()
        session = FakeAsyncSession(
            {"certificate": [{"tenant_id": str(tenant_id), "numbering_scope": "2025", "n": 41}]}
        )

        assert await CertificateRepository(session, tenant_id).next_certificate_no("2025") == (
            "CERT-000042"
        )

    async def test_next_certificate_no_of_an_empty_scope_starts_at_one(self) -> None:
        tenant_id = uuid4()
        for value in (0, None):
            session = FakeAsyncSession(
                {
                    "certificate": [
                        {"tenant_id": str(tenant_id), "numbering_scope": "2025", "n": value}
                    ]
                }
            )
            repo = CertificateRepository(session, tenant_id)
            assert await repo.next_certificate_no("2025") == "CERT-000001"