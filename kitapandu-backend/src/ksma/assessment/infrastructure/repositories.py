"""Repositories for assessment + reporting."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.assessment.domain import (
    Assessment,
    AssessmentEvidence,
    AssessmentResult,
    AssessmentStatus,
    EvidenceVisibility,
    ResultState,
)
from ksma.reporting.domain import (
    Certificate,
    CertificateStatus,
    CompletionDecision,
    CompletionOutcome,
    ReportPublication,
    ReportStatus,
)


def _json(d: Any) -> str:
    import json

    return json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


class AssessmentRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, a: Assessment) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO assessment
                  (id, tenant_id, unit_id, enrollment_id, rubric_version_id,
                   assessor_id, assessment_date, status, supersedes_id,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :eid, :rvid, :aid, :ad, :status, :sup,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(a.id),
                "tid": str(self.tenant_id),
                "uid": str(a.unit_id),
                "eid": str(a.enrollment_id),
                "rvid": str(a.rubric_version_id),
                "aid": str(a.assessor_id),
                "ad": a.assessment_date,
                "status": a.status.value,
                "sup": str(a.supersedes_id) if a.supersedes_id else None,
                "cb": a.created_by,
                "ub": a.updated_by,
            },
        )

    async def get(self, id: UUID) -> Assessment | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, enrollment_id, rubric_version_id,
                           assessor_id, assessment_date, status, supersedes_id,
                           created_at, created_by, updated_at, updated_by
                    FROM assessment WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_assessment(row) if row else None

    async def update_status(self, id: UUID, *, status: AssessmentStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE assessment SET status=:s, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def list_for_enrollment(self, enrollment_id: UUID) -> list[Assessment]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, enrollment_id, rubric_version_id,
                           assessor_id, assessment_date, status, supersedes_id,
                           created_at, created_by, updated_at, updated_by
                    FROM assessment
                    WHERE enrollment_id = :eid AND tenant_id = :tid
                    ORDER BY created_at DESC
                    """
                ),
                {"eid": str(enrollment_id), "tid": str(self.tenant_id)},
            )
        ).all()
        return [_to_assessment(r) for r in rows]


class AssessmentResultRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def upsert(self, r: AssessmentResult) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO assessment_result
                  (id, tenant_id, assessment_id, indicator_id, numeric_value,
                   category_code, narrative_value, result_state, follow_up,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :aid, :iid, :nv, :cc, :nrv, :rs, :fu,
                   now(), :cb, now(), :ub)
                ON CONFLICT (assessment_id, indicator_id) DO UPDATE
                  SET numeric_value = EXCLUDED.numeric_value,
                      category_code = EXCLUDED.category_code,
                      narrative_value = EXCLUDED.narrative_value,
                      result_state = EXCLUDED.result_state,
                      follow_up = EXCLUDED.follow_up,
                      updated_at = now(),
                      updated_by = EXCLUDED.created_by
                """
            ),
            {
                "id": str(r.id),
                "tid": str(self.tenant_id),
                "aid": str(r.assessment_id),
                "iid": str(r.indicator_id),
                "nv": r.numeric_value,
                "cc": r.category_code,
                "nrv": r.narrative_value,
                "rs": r.result_state.value,
                "fu": r.follow_up,
                "cb": r.created_by,
                "ub": r.updated_by,
            },
        )

    async def list_for_assessment(self, assessment_id: UUID) -> list[AssessmentResult]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, assessment_id, indicator_id, numeric_value,
                           category_code, narrative_value, result_state, follow_up,
                           created_at, created_by, updated_at, updated_by
                    FROM assessment_result
                    WHERE assessment_id = :aid AND tenant_id = :tid
                    ORDER BY created_at
                    """
                ),
                {"aid": str(assessment_id), "tid": str(self.tenant_id)},
            )
        ).all()
        return [_to_result(r) for r in rows]


class AssessmentEvidenceRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, e: AssessmentEvidence) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO assessment_evidence
                  (id, tenant_id, assessment_id, result_id, attachment_id,
                   caption, visibility, created_at, created_by)
                VALUES
                  (:id, :tid, :aid, :rid, :att, :cap, :vis, now(), :cb)
                """
            ),
            {
                "id": str(e.id),
                "tid": str(self.tenant_id),
                "aid": str(e.assessment_id),
                "rid": str(e.result_id) if e.result_id else None,
                "att": str(e.attachment_id),
                "cap": e.caption,
                "vis": e.visibility.value,
                "cb": e.created_by,
            },
        )

    async def list_for_assessment(self, assessment_id: UUID) -> list[AssessmentEvidence]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, assessment_id, result_id, attachment_id,
                           caption, visibility, created_at, created_by
                    FROM assessment_evidence
                    WHERE assessment_id = :aid AND tenant_id = :tid
                    ORDER BY created_at
                    """
                ),
                {"aid": str(assessment_id), "tid": str(self.tenant_id)},
            )
        ).all()
        return [_to_evidence(r) for r in rows]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class ReportPublicationRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, r: ReportPublication) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO report_publication
                  (id, tenant_id, unit_id, enrollment_id, publication_no,
                   source_assessment_ids, snapshot, publication, attachment_id,
                   supersedes_id, status,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :eid, :no, CAST(:src AS uuid[]),
                   CAST(:snap AS JSONB), CAST(:pub AS JSONB), :att,
                   :sup, :status,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(r.id),
                "tid": str(self.tenant_id),
                "uid": str(r.unit_id),
                "eid": str(r.enrollment_id),
                "no": r.publication_no,
                "src": [str(x) for x in r.source_assessment_ids],
                "snap": _json(r.snapshot),
                "pub": _json(r.publication),
                "att": str(r.attachment_id) if r.attachment_id else None,
                "sup": str(r.supersedes_id) if r.supersedes_id else None,
                "status": r.status.value,
                "cb": r.created_by,
                "ub": r.updated_by,
            },
        )

    async def get(self, id: UUID) -> ReportPublication | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, enrollment_id, publication_no,
                           source_assessment_ids, snapshot, publication, attachment_id,
                           supersedes_id, status,
                           created_at, created_by, updated_at, updated_by
                    FROM report_publication
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_publication(row) if row else None

    async def next_publication_no(self, enrollment_id: UUID) -> int:
        row = (
            await self.session.execute(
                text(
                    "SELECT COALESCE(MAX(publication_no), 0) AS n FROM report_publication "
                    "WHERE enrollment_id = :eid AND tenant_id = :tid"
                ),
                {"eid": str(enrollment_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return (row.n or 0) + 1

    async def update_status(self, id: UUID, *, status: ReportStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE report_publication SET status=:s, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def list_for_enrollment(self, enrollment_id: UUID) -> list[ReportPublication]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, enrollment_id, publication_no,
                           source_assessment_ids, snapshot, publication, attachment_id,
                           supersedes_id, status,
                           created_at, created_by, updated_at, updated_by
                    FROM report_publication
                    WHERE enrollment_id = :eid AND tenant_id = :tid
                    ORDER BY publication_no DESC
                    """
                ),
                {"eid": str(enrollment_id), "tid": str(self.tenant_id)},
            )
        ).all()
        return [_to_publication(r) for r in rows]


class CompletionDecisionRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, c: CompletionDecision) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO completion_decision
                  (id, tenant_id, unit_id, enrollment_id, outcome,
                   evidence_refs, policy_id, decision, supersedes_id,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :uid, :eid, :outcome, CAST(:refs AS JSONB),
                   :pol, CAST(:dec AS JSONB), :sup,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(c.id),
                "tid": str(self.tenant_id),
                "uid": str(c.unit_id),
                "eid": str(c.enrollment_id),
                "outcome": c.outcome.value,
                "refs": _json(c.evidence_refs),
                "pol": str(c.policy_id) if c.policy_id else None,
                "dec": _json(c.decision),
                "sup": str(c.supersedes_id) if c.supersedes_id else None,
                "cb": c.created_by,
                "ub": c.updated_by,
            },
        )

    async def get(self, id: UUID) -> CompletionDecision | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, enrollment_id, outcome,
                           evidence_refs, policy_id, decision, supersedes_id,
                           created_at, created_by, updated_at, updated_by
                    FROM completion_decision
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_completion(row) if row else None

    async def latest_for_enrollment(self, enrollment_id: UUID) -> CompletionDecision | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, enrollment_id, outcome,
                           evidence_refs, policy_id, decision, supersedes_id,
                           created_at, created_by, updated_at, updated_by
                    FROM completion_decision
                    WHERE enrollment_id = :eid AND tenant_id = :tid
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"eid": str(enrollment_id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_completion(row) if row else None


class CertificateRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, c: Certificate) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO certificate
                  (id, tenant_id, unit_id, completion_id, numbering_scope,
                   certificate_no, snapshot, attachment_id, status,
                   replacement_id, revocation,
                   created_at, created_by)
                VALUES
                  (:id, :tid, :uid, :cid, :scope, :no, CAST(:snap AS JSONB),
                   :att, :status, :repl, CAST(:rev AS JSONB),
                   now(), :cb)
                """
            ),
            {
                "id": str(c.id),
                "tid": str(self.tenant_id),
                "uid": str(c.unit_id),
                "cid": str(c.completion_id),
                "scope": c.numbering_scope,
                "no": c.certificate_no,
                "snap": _json(c.snapshot),
                "att": str(c.attachment_id) if c.attachment_id else None,
                "status": c.status.value,
                "repl": str(c.replacement_id) if c.replacement_id else None,
                "rev": _json(c.revocation) if c.revocation else None,
                "cb": c.created_by,
            },
        )

    async def get(self, id: UUID) -> Certificate | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, unit_id, completion_id, numbering_scope,
                           certificate_no, snapshot, attachment_id, status,
                           replacement_id, revocation,
                           created_at, created_by
                    FROM certificate WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_certificate(row) if row else None

    async def next_certificate_no(self, scope: str) -> str:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT COALESCE(MAX(CAST(SUBSTRING(certificate_no FROM '([0-9]+)$') AS integer)), 0) AS n
                    FROM certificate
                    WHERE tenant_id = :tid AND numbering_scope = :scope
                      AND certificate_no ~ '[0-9]+$'
                    """
                ),
                {"tid": str(self.tenant_id), "scope": scope},
            )
        ).first()
        n = (row.n or 0) + 1
        return f"CERT-{n:06d}"


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _to_assessment(row: Any) -> Assessment:
    return Assessment(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        enrollment_id=row.enrollment_id,
        rubric_version_id=row.rubric_version_id,
        assessor_id=row.assessor_id,
        assessment_date=row.assessment_date,
        status=AssessmentStatus(row.status),
        supersedes_id=row.supersedes_id,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_result(row: Any) -> AssessmentResult:
    return AssessmentResult(
        id=row.id,
        tenant_id=row.tenant_id,
        assessment_id=row.assessment_id,
        indicator_id=row.indicator_id,
        numeric_value=row.numeric_value,
        category_code=row.category_code,
        narrative_value=row.narrative_value,
        result_state=ResultState(row.result_state),
        follow_up=row.follow_up,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_evidence(row: Any) -> AssessmentEvidence:
    return AssessmentEvidence(
        id=row.id,
        tenant_id=row.tenant_id,
        assessment_id=row.assessment_id,
        result_id=row.result_id,
        attachment_id=row.attachment_id,
        caption=row.caption,
        visibility=EvidenceVisibility(row.visibility),
        created_at=row.created_at,
        created_by=row.created_by,
    )


def _to_publication(row: Any) -> ReportPublication:
    return ReportPublication(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        enrollment_id=row.enrollment_id,
        publication_no=row.publication_no,
        source_assessment_ids=list(row.source_assessment_ids or []),
        snapshot=row.snapshot,
        publication=row.publication,
        attachment_id=row.attachment_id,
        supersedes_id=row.supersedes_id,
        status=ReportStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_completion(row: Any) -> CompletionDecision:
    return CompletionDecision(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        enrollment_id=row.enrollment_id,
        outcome=CompletionOutcome(row.outcome),
        evidence_refs=list(row.evidence_refs or []),
        policy_id=row.policy_id,
        decision=row.decision,
        supersedes_id=row.supersedes_id,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_certificate(row: Any) -> Certificate:
    return Certificate(
        id=row.id,
        tenant_id=row.tenant_id,
        unit_id=row.unit_id,
        completion_id=row.completion_id,
        numbering_scope=row.numbering_scope,
        certificate_no=row.certificate_no,
        snapshot=row.snapshot,
        attachment_id=row.attachment_id,
        status=CertificateStatus(row.status),
        replacement_id=row.replacement_id,
        revocation=row.revocation,
        created_at=row.created_at,
        created_by=row.created_by,
    )