"""Reporting repositories (raw SQL, mirroring the assessment layer style)."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.reporting.domain import (
    Certificate,
    CertificateStatus,
    CompletionDecision,
    CompletionOutcome,
    ReportPublication,
    ReportStatus,
)


def _json(d: Any) -> str:
    return json.dumps(d, default=str)


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
        return int(row.n or 0) + 1

    async def update_status(self, id: UUID, *, status: ReportStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE report_publication SET status=:s, updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def set_supersedes(self, new_id: UUID, old_id: UUID) -> None:
        await self.session.execute(
            text(
                "UPDATE report_publication SET supersedes_id=:o "
                "WHERE id=:n AND tenant_id=:tid"
            ),
            {"o": str(old_id), "n": str(new_id), "tid": str(self.tenant_id)},
        )

    async def append_publication(self, id: UUID, *, payload: dict[str, Any]) -> None:
        await self.session.execute(
            text(
                "UPDATE report_publication SET publication = publication || CAST(:p AS JSONB) "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"p": _json(payload), "id": str(id), "tid": str(self.tenant_id)},
        )

    async def set_revocation(self, id: UUID, *, payload: dict[str, Any]) -> None:
        await self.session.execute(
            text(
                "UPDATE report_publication SET publication = publication || "
                "jsonb_build_object('revocation', CAST(:p AS JSONB)) "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"p": _json(payload), "id": str(id), "tid": str(self.tenant_id)},
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
        # Serialize per scope within the tenant to avoid concurrent collisions.
        # We rely on a per-row lock on the certificate table by reading + writing
        # in the same transaction; callers wrap this in SELECT FOR UPDATE on
        # ``policy_version`` for the certificate domain.
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT COALESCE(
                      MAX(CAST(SUBSTRING(certificate_no FROM '([0-9]+)$') AS integer)),
                      0
                    ) AS n
                    FROM certificate
                    WHERE tenant_id = :tid AND numbering_scope = :scope
                      AND certificate_no ~ '[0-9]+$'
                    FOR UPDATE
                    """
                ),
                {"tid": str(self.tenant_id), "scope": scope},
            )
        ).first()
        n = int(row.n or 0) + 1
        return f"CERT-{n:06d}"

    async def mark_replaced(self, old_id: UUID, new_id: UUID) -> None:
        await self.session.execute(
            text(
                "UPDATE certificate SET status='replaced', replacement_id=:n "
                "WHERE id=:o AND tenant_id=:tid"
            ),
            {"o": str(old_id), "n": str(new_id), "tid": str(self.tenant_id)},
        )

    async def set_revocation(self, id: UUID, *, payload: dict[str, Any]) -> None:
        await self.session.execute(
            text(
                """
                UPDATE certificate
                SET status='revoked',
                    revocation = CAST(:p AS JSONB),
                    updated_at = now()
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {"p": _json(payload), "id": str(id), "tid": str(self.tenant_id)},
        )


# --- Mapping --------------------------------------------------------------


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


__all__ = [
    "ReportPublicationRepository",
    "CompletionDecisionRepository",
    "CertificateRepository",
]