"""Unit tests for the assessment SQL repositories (#49).

Seam: ``ksma.assessment.infrastructure.repositories`` — ``AssessmentRepository``,
``AssessmentResultRepository`` and ``AssessmentEvidenceRepository``, plus the
reporting repositories that share the module (``ReportPublicationRepository``,
``CompletionDecisionRepository``, ``CertificateRepository``).

The session is the ``FakeAsyncSession`` from ``tests/unit/conftest_helpers.py``.
``AssessmentResultRepository.upsert`` is the interesting one: it is a single
``INSERT … ON CONFLICT (assessment_id, indicator_id) DO UPDATE``, so a second
write for the same indicator must carry the conflict branch and overwrite the
value columns rather than append a duplicate row.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from ksma.assessment.domain import (
    Assessment,
    AssessmentEvidence,
    AssessmentResult,
    AssessmentStatus,
    EvidenceVisibility,
    ResultState,
)
from ksma.assessment.infrastructure.repositories import (
    AssessmentEvidenceRepository,
    AssessmentRepository,
    AssessmentResultRepository,
    CertificateRepository,
    CompletionDecisionRepository,
    ReportPublicationRepository,
)
from ksma.reporting.domain import CertificateStatus, CompletionOutcome
from tests.unit.conftest_helpers import FakeAsyncSession


def _last_execute(session: FakeAsyncSession, fragment: str) -> Any:
    """The most recent ``execute`` whose SQL mentions ``fragment``."""
    calls = [call for call in session.calls if call.op == "execute" and fragment in call.sql]
    assert calls, f"no execute mentioning {fragment!r}; recorded: {[c.sql[:60] for c in session.calls]}"
    return calls[-1]


class TestAssessmentRepository:
    async def test_create_writes_the_rubric_and_supersession_links(self) -> None:
        session = FakeAsyncSession()
        supersedes_id = uuid4()
        assessment = Assessment(
            tenant_id=uuid4(),
            unit_id=uuid4(),
            enrollment_id=uuid4(),
            rubric_version_id=uuid4(),
            assessor_id=uuid4(),
            assessment_date="2025-05-01",
            status=AssessmentStatus.SUBMITTED,
            supersedes_id=supersedes_id,
            created_by="assessor-1",
            updated_by="assessor-2",
        )

        await AssessmentRepository(session, assessment.tenant_id).create(assessment)

        call = session.assert_called(op="execute", params_contains="assessor-1")
        assert call.params["sup"] == str(supersedes_id)
        assert call.params["status"] == "submitted"
        assert call.params["ub"] == "assessor-2"

    async def test_create_of_a_first_assessment_stores_no_supersession(self) -> None:
        session = FakeAsyncSession()
        assessment = Assessment(tenant_id=uuid4(), supersedes_id=None)

        await AssessmentRepository(session, assessment.tenant_id).create(assessment)

        assert session.assert_called(op="execute").params["sup"] is None

    async def test_get_returns_none_when_the_row_is_absent(self) -> None:
        session = FakeAsyncSession({"assessment": [{"status": "draft"}]})

        assert await AssessmentRepository(session, uuid4()).get(uuid4()) is None

    async def test_list_for_enrollment_is_enrollment_scoped(self) -> None:
        session = FakeAsyncSession()
        tenant_id, enrollment_id, other = uuid4(), uuid4(), uuid4()
        shared = {
            "tenant_id": str(tenant_id),
            "unit_id": str(uuid4()),
            "rubric_version_id": str(uuid4()),
            "assessor_id": str(uuid4()),
            "assessment_date": None,
            "status": "reviewed",
            "supersedes_id": None,
            "created_at": None,
            "created_by": "assessor-1",
            "updated_at": None,
            "updated_by": "assessor-1",
        }
        session.seed(
            "assessment",
            [
                {"id": str(uuid4()), "enrollment_id": str(enrollment_id), **shared},
                {"id": str(uuid4()), "enrollment_id": str(other), **shared},
            ],
        )

        found = await AssessmentRepository(session, tenant_id).list_for_enrollment(enrollment_id)

        assert len(found) == 1
        assert found[0].status is AssessmentStatus.REVIEWED
        call = _last_execute(session, "FROM assessment\n")
        assert call.params["eid"] == str(enrollment_id)
        assert "ORDER BY created_at DESC" in call.sql

    async def test_update_status_writes_the_status_and_the_actor(self) -> None:
        session = FakeAsyncSession()
        tenant_id, assessment_id = uuid4(), uuid4()

        await AssessmentRepository(session, tenant_id).update_status(
            assessment_id, status=AssessmentStatus.SUPERSEDED, actor="assessor-2"
        )

        call = session.calls[-1]
        assert call.params["s"] == "superseded"
        assert call.params["u"] == "assessor-2"
        assert call.params["tid"] == str(tenant_id)


class TestAssessmentResultRepository:
    async def test_upsert_writes_each_value_kind_column(self) -> None:
        session = FakeAsyncSession()
        result = AssessmentResult(
            tenant_id=uuid4(),
            assessment_id=uuid4(),
            indicator_id=uuid4(),
            numeric_value=88.5,
            category_code=None,
            narrative_value=None,
            result_state=ResultState.ASSESSED,
            follow_up="Re-run in June",
            created_by="assessor-1",
            updated_by="assessor-1",
        )

        await AssessmentResultRepository(session, result.tenant_id).upsert(result)

        call = session.assert_called(op="execute")
        assert call.params["nv"] == 88.5
        assert call.params["cc"] is None
        assert call.params["nrv"] is None
        assert call.params["rs"] == "assessed"
        assert call.params["fu"] == "Re-run in June"

    async def test_upsert_is_a_conflict_update_on_assessment_and_indicator(self) -> None:
        """Re-scoring an indicator updates the row, it never appends a second one."""
        session = FakeAsyncSession()
        tenant_id, assessment_id, indicator_id = uuid4(), uuid4(), uuid4()
        repo = AssessmentResultRepository(session, tenant_id)
        first = AssessmentResult(
            tenant_id=tenant_id,
            assessment_id=assessment_id,
            indicator_id=indicator_id,
            numeric_value=50.0,
            result_state=ResultState.ASSESSED,
            created_by="assessor-1",
        )
        second = AssessmentResult(
            tenant_id=tenant_id,
            assessment_id=assessment_id,
            indicator_id=indicator_id,
            numeric_value=90.0,
            result_state=ResultState.ASSESSED,
            created_by="assessor-2",
        )

        await repo.upsert(first)
        await repo.upsert(second)

        call = session.calls[-1]
        assert "ON CONFLICT (assessment_id, indicator_id) DO UPDATE" in call.sql
        assert call.params["nv"] == 90.0
        assert call.params["cb"] == "assessor-2"
        assert call.params["aid"] == str(assessment_id)
        assert call.params["iid"] == str(indicator_id)

    async def test_list_for_assessment_returns_every_value_kind(self) -> None:
        session = FakeAsyncSession()
        tenant_id, assessment_id = uuid4(), uuid4()
        shared = {
            "tenant_id": str(tenant_id),
            "assessment_id": str(assessment_id),
            "follow_up": None,
            "created_at": None,
            "created_by": "assessor-1",
            "updated_at": None,
            "updated_by": "assessor-1",
        }
        session.seed(
            "assessment_result",
            [
                {
                    "id": str(uuid4()),
                    "indicator_id": str(uuid4()),
                    "numeric_value": 77.0,
                    "category_code": None,
                    "narrative_value": None,
                    "result_state": "assessed",
                    **shared,
                },
                {
                    "id": str(uuid4()),
                    "indicator_id": str(uuid4()),
                    "numeric_value": None,
                    "category_code": "B",
                    "narrative_value": None,
                    "result_state": "not_applicable",
                    **shared,
                },
                {
                    "id": str(uuid4()),
                    "indicator_id": str(uuid4()),
                    "numeric_value": None,
                    "category_code": None,
                    "narrative_value": "Clear and fluent",
                    "result_state": "assessed",
                    **shared,
                },
                {
                    "id": str(uuid4()),
                    "indicator_id": str(uuid4()),
                    "numeric_value": None,
                    "category_code": None,
                    "narrative_value": None,
                    "result_state": "not_assessed",
                    **shared,
                },
            ],
        )

        found = await AssessmentResultRepository(session, tenant_id).list_for_assessment(assessment_id)

        assert [r.result_state for r in found] == [
            ResultState.ASSESSED,
            ResultState.NOT_APPLICABLE,
            ResultState.ASSESSED,
            ResultState.NOT_ASSESSED,
        ]
        assert [r.numeric_value for r in found] == [77.0, None, None, None]
        assert [r.category_code for r in found] == [None, "B", None, None]
        assert found[2].narrative_value == "Clear and fluent"
        call = _last_execute(session, "FROM assessment_result")
        assert call.params["aid"] == str(assessment_id)


class TestAssessmentEvidenceRepository:
    async def test_create_writes_the_attachment_visibility_and_caption(self) -> None:
        session = FakeAsyncSession()
        result_id = uuid4()
        evidence = AssessmentEvidence(
            tenant_id=uuid4(),
            assessment_id=uuid4(),
            result_id=result_id,
            attachment_id=uuid4(),
            caption="Audio sample",
            visibility=EvidenceVisibility.INTERNAL_ASSESSMENT,
            created_by="assessor-1",
        )

        await AssessmentEvidenceRepository(session, evidence.tenant_id).create(evidence)

        call = session.assert_called(op="execute", params_contains="Audio sample")
        assert call.params["rid"] == str(result_id)
        assert call.params["vis"] == "internal_assessment"
        assert call.params["cb"] == "assessor-1"

    async def test_create_without_a_result_stores_null(self) -> None:
        session = FakeAsyncSession()
        evidence = AssessmentEvidence(tenant_id=uuid4(), result_id=None, caption=None)

        await AssessmentEvidenceRepository(session, evidence.tenant_id).create(evidence)

        call = session.assert_called(op="execute")
        assert call.params["rid"] is None
        assert call.params["cap"] is None

    async def test_list_for_assessment_is_assessment_scoped(self) -> None:
        session = FakeAsyncSession()
        tenant_id, assessment_id, other = uuid4(), uuid4(), uuid4()
        shared = {
            "tenant_id": str(tenant_id),
            "result_id": None,
            "attachment_id": str(uuid4()),
            "visibility": "internal_assessment",
            "created_at": None,
            "created_by": "assessor-1",
        }
        session.seed(
            "assessment_evidence",
            [
                {"id": str(uuid4()), "assessment_id": str(assessment_id), "caption": "one", **shared},
                {"id": str(uuid4()), "assessment_id": str(assessment_id), "caption": "two", **shared},
                {"id": str(uuid4()), "assessment_id": str(other), "caption": "other", **shared},
            ],
        )

        found = await AssessmentEvidenceRepository(session, tenant_id).list_for_assessment(
            assessment_id
        )

        assert [e.caption for e in found] == ["one", "two"]
        call = _last_execute(session, "FROM assessment_evidence")
        assert call.params["aid"] == str(assessment_id)
        assert call.params["tid"] == str(tenant_id)


class TestReportingRepositoriesInTheAssessmentModule:
    """The reporting repositories live in this module too — same adapters, same bugs."""

    async def test_next_publication_no_steps_past_the_current_maximum(self) -> None:
        tenant_id, enrollment_id = uuid4(), uuid4()
        session = FakeAsyncSession(
            {
                "report_publication": [
                    {"tenant_id": str(tenant_id), "enrollment_id": str(enrollment_id), "n": 2}
                ]
            }
        )

        assert (
            await ReportPublicationRepository(session, tenant_id).next_publication_no(enrollment_id)
            == 3
        )

    async def test_next_publication_no_of_an_empty_history_starts_at_one(self) -> None:
        tenant_id, enrollment_id = uuid4(), uuid4()
        session = FakeAsyncSession(
            {
                "report_publication": [
                    {"tenant_id": str(tenant_id), "enrollment_id": str(enrollment_id), "n": None}
                ]
            }
        )

        assert (
            await ReportPublicationRepository(session, tenant_id).next_publication_no(enrollment_id)
            == 1
        )

    async def test_source_assessment_ids_survive_the_row_adapter(self) -> None:
        session = FakeAsyncSession()
        tenant_id, publication_id = uuid4(), uuid4()
        assessment_ids = [str(uuid4())]
        session.seed(
            "report_publication",
            {
                "id": str(publication_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "enrollment_id": str(uuid4()),
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
        tenant_id, publication_id = uuid4(), uuid4()
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

    async def test_evidence_refs_survive_the_row_adapter(self) -> None:
        session = FakeAsyncSession()
        tenant_id, decision_id = uuid4(), uuid4()
        refs = [{"indicator": "I-1"}]
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
        tenant_id, decision_id = uuid4(), uuid4()
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

    async def test_next_certificate_no_is_zero_padded_and_steps_past_the_max(self) -> None:
        tenant_id = uuid4()
        session = FakeAsyncSession(
            {"certificate": [{"tenant_id": str(tenant_id), "numbering_scope": "2025", "n": 8}]}
        )

        assert await CertificateRepository(session, tenant_id).next_certificate_no("2025") == (
            "CERT-000009"
        )

    async def test_next_certificate_no_of_an_empty_scope_starts_at_one(self) -> None:
        tenant_id = uuid4()
        session = FakeAsyncSession(
            {"certificate": [{"tenant_id": str(tenant_id), "numbering_scope": "2025", "n": None}]}
        )

        assert await CertificateRepository(session, tenant_id).next_certificate_no("2025") == (
            "CERT-000001"
        )

    async def test_get_maps_the_certificate_status_enum(self) -> None:
        session = FakeAsyncSession()
        tenant_id, certificate_id = uuid4(), uuid4()
        session.seed(
            "certificate",
            {
                "id": str(certificate_id),
                "tenant_id": str(tenant_id),
                "unit_id": str(uuid4()),
                "completion_id": str(uuid4()),
                "numbering_scope": "2025",
                "certificate_no": "CERT-000003",
                "snapshot": json.loads('{"student": "A. Student"}'),
                "attachment_id": None,
                "status": "revoked",
                "replacement_id": None,
                "revocation": {"reason": "issued in error"},
                "created_at": None,
                "created_by": "registrar",
            },
        )

        found = await CertificateRepository(session, tenant_id).get(certificate_id)

        assert found is not None
        assert found.status is CertificateStatus.REVOKED
        assert found.revocation == {"reason": "issued in error"}