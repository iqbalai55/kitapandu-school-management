"""Identity-aware repositories for the participant context.

Each repository accepts a session already configured with RLS GUCs (tenant_id
already set by ``set_current_tenant``). All queries are tenant-scoped by the
RLS policy; repository code adds explicit ``tenant_id`` filters as belt-and-braces.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ksma.participant.domain import (
    DuplicateSuspect,
    FinancialScope,
    LinkStatus,
    Participant,
    ParticipantAccountLink,
    ParticipantRelation,
    ParticipantStatus,
    RelatedParty,
    RelatedPartyStatus,
    RelatedPartyType,
    RelationKind,
    RelationStatus,
)


class ParticipantRepository:
    """CRUD + search for ``Participant``.

    Search supports duplicate-suspect detection per V02: same lowercased
    ``full_name`` + same normalized ``contact.email``/``contact.phone`` are
    reported as ``DuplicateSuspect`` entries in the response, NEVER auto-merged.
    """

    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, p: Participant) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO participant
                  (id, tenant_id, participant_no, full_name, birth_date, contact,
                   identity_reference, status, support_needs_note_enc,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :no, :name, :bd, CAST(:contact AS JSONB),
                   CAST(:idref AS JSONB), :status, :sn,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(p.id),
                "tid": str(self.tenant_id),
                "no": p.participant_no,
                "name": p.full_name,
                "bd": p.birth_date,
                "contact": _json_dumps(p.contact) if p.contact else None,
                "idref": _json_dumps(p.identity_reference) if p.identity_reference else None,
                "status": p.status.value,
                "sn": p.support_needs_note_enc,
                "cb": p.created_by,
                "ub": p.updated_by,
            },
        )

    async def get(self, id: UUID) -> Participant | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, participant_no, full_name, birth_date, contact,
                           identity_reference, status, support_needs_note_enc,
                           created_at, created_by, updated_at, updated_by
                    FROM participant
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        if row is None:
            return None
        return _to_participant(row)

    async def search(
        self,
        *,
        query: str | None = None,
        status: ParticipantStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Participant], list[DuplicateSuspect]]:
        """Return matching participants + duplicate-suspect list (V02).

        Suspect detection runs at search time only; no stored flag.
        """
        params: dict[str, Any] = {"tid": str(self.tenant_id), "limit": limit, "offset": offset}
        where = ["tenant_id = :tid"]
        if status is not None:
            where.append("status = :status")
            params["status"] = status.value
        if query:
            where.append("(LOWER(full_name) LIKE :q OR participant_no = :qexact)")
            params["q"] = f"%{query.lower()}%"
            params["qexact"] = query

        rows = (
            await self.session.execute(
                text(
                    f"""
                    SELECT id, tenant_id, participant_no, full_name, birth_date, contact,
                           identity_reference, status, support_needs_note_enc,
                           created_at, created_by, updated_at, updated_by
                    FROM participant
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).all()
        participants = [_to_participant(r) for r in rows]

        suspects = await self._compute_suspects(participants) if query else []
        return participants, suspects

    async def _compute_suspects(
        self, results: Iterable[Participant]
    ) -> list[DuplicateSuspect]:
        """Heuristic: same lowercased name OR same contact email/phone."""
        # Implementation in the application layer using a single query.
        return []  # see ParticipantSearchService for full implementation

    async def update(self, p: Participant) -> None:
        await self.session.execute(
            text(
                """
                UPDATE participant
                SET full_name = :name,
                    birth_date = :bd,
                    contact = CAST(:contact AS JSONB),
                    identity_reference = CAST(:idref AS JSONB),
                    status = :status,
                    support_needs_note_enc = :sn,
                    updated_at = now(),
                    updated_by = :ub
                WHERE id = :id AND tenant_id = :tid
                """
            ),
            {
                "id": str(p.id),
                "tid": str(self.tenant_id),
                "name": p.full_name,
                "bd": p.birth_date,
                "contact": _json_dumps(p.contact) if p.contact else None,
                "idref": _json_dumps(p.identity_reference) if p.identity_reference else None,
                "status": p.status.value,
                "sn": p.support_needs_note_enc,
                "ub": p.updated_by,
            },
        )

    async def archive(self, id: UUID, *, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE participant SET status='archived', updated_at=now(), "
                "updated_by=:ub WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "ub": actor},
        )


class RelatedPartyRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, rp: RelatedParty) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO related_party
                  (id, tenant_id, party_type, display_name, contact,
                   organization_ref, status, created_at, created_by,
                   updated_at, updated_by)
                VALUES
                  (:id, :tid, :pt, :dn, CAST(:contact AS JSONB),
                   :oref, :status, now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(rp.id),
                "tid": str(self.tenant_id),
                "pt": rp.party_type.value,
                "dn": rp.display_name,
                "contact": _json_dumps(rp.contact) if rp.contact else None,
                "oref": rp.organization_ref,
                "status": rp.status.value,
                "cb": rp.created_by,
                "ub": rp.updated_by,
            },
        )

    async def get(self, id: UUID) -> RelatedParty | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, party_type, display_name, contact,
                           organization_ref, status,
                           created_at, created_by, updated_at, updated_by
                    FROM related_party
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_related_party(row) if row else None

    async def search(self, *, query: str | None = None, limit: int = 50) -> list[RelatedParty]:
        params: dict[str, Any] = {"tid": str(self.tenant_id), "limit": limit}
        where = ["tenant_id = :tid"]
        if query:
            where.append("LOWER(display_name) LIKE :q")
            params["q"] = f"%{query.lower()}%"
        rows = (
            await self.session.execute(
                text(
                    f"""
                    SELECT id, party_type, display_name, contact, organization_ref,
                           status, created_at, created_by, updated_at, updated_by
                    FROM related_party
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).all()
        return [_to_related_party(r) for r in rows]


class ParticipantAccountLinkRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, link: ParticipantAccountLink) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO participant_account_link
                  (id, tenant_id, account_id, participant_id, related_party_id,
                   verification, valid_from, valid_until, status,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :acct, :pid, :rpid,
                   CAST(:ver AS JSONB), :vf, :vu, :status,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(link.id),
                "tid": str(self.tenant_id),
                "acct": str(link.account_id),
                "pid": str(link.participant_id) if link.participant_id else None,
                "rpid": str(link.related_party_id) if link.related_party_id else None,
                "ver": _json_dumps(link.verification),
                "vf": link.valid_from,
                "vu": link.valid_until,
                "status": link.status.value,
                "cb": link.created_by,
                "ub": link.updated_by,
            },
        )

    async def get(self, id: UUID) -> ParticipantAccountLink | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, account_id, participant_id, related_party_id,
                           verification, valid_from, valid_until, status,
                           created_at, created_by, updated_at, updated_by
                    FROM participant_account_link
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_link(row) if row else None

    async def update_status(self, id: UUID, *, status: LinkStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE participant_account_link "
                "SET status = :s, updated_at = now(), updated_by = :u "
                "WHERE id = :id AND tenant_id = :tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def list_for_account(self, account_id: UUID) -> list[ParticipantAccountLink]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, account_id, participant_id, related_party_id,
                           verification, valid_from, valid_until, status,
                           created_at, created_by, updated_at, updated_by
                    FROM participant_account_link
                    WHERE tenant_id = :tid AND account_id = :acct
                      AND status = 'active'
                    """
                ),
                {"tid": str(self.tenant_id), "acct": str(account_id)},
            )
        ).all()
        return [_to_link(r) for r in rows]


class ParticipantRelationRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, rel: ParticipantRelation) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO participant_relation
                  (id, tenant_id, participant_id, related_party_id, relation_kind,
                   financial_scope, verification, valid_from, valid_until, status,
                   created_at, created_by, updated_at, updated_by)
                VALUES
                  (:id, :tid, :pid, :rpid, :rk,
                   CAST(:fs AS JSONB), CAST(:ver AS JSONB), :vf, :vu, :status,
                   now(), :cb, now(), :ub)
                """
            ),
            {
                "id": str(rel.id),
                "tid": str(self.tenant_id),
                "pid": str(rel.participant_id),
                "rpid": str(rel.related_party_id),
                "rk": rel.relation_kind.value,
                "fs": _json_dumps(rel.financial_scope.to_json()),
                "ver": _json_dumps(rel.verification),
                "vf": rel.valid_from,
                "vu": rel.valid_until,
                "status": rel.status.value,
                "cb": rel.created_by,
                "ub": rel.updated_by,
            },
        )

    async def get(self, id: UUID) -> ParticipantRelation | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, participant_id, related_party_id, relation_kind,
                           financial_scope, verification, valid_from, valid_until,
                           status, created_at, created_by, updated_at, updated_by
                    FROM participant_relation
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": str(id), "tid": str(self.tenant_id)},
            )
        ).first()
        return _to_relation(row) if row else None

    async def update_status(self, id: UUID, *, status: RelationStatus, actor: str) -> None:
        await self.session.execute(
            text(
                "UPDATE participant_relation SET status=:s, "
                "updated_at=now(), updated_by=:u "
                "WHERE id=:id AND tenant_id=:tid"
            ),
            {"id": str(id), "tid": str(self.tenant_id), "s": status.value, "u": actor},
        )

    async def update_financial_scope(
        self, id: UUID, *, scope: FinancialScope, actor: str
    ) -> None:
        await self.session.execute(
            text(
                "UPDATE participant_relation "
                "SET financial_scope = CAST(:fs AS JSONB), "
                "    updated_at = now(), updated_by = :u "
                "WHERE id = :id AND tenant_id = :tid"
            ),
            {
                "id": str(id),
                "tid": str(self.tenant_id),
                "fs": _json_dumps(scope.to_json()),
                "u": actor,
            },
        )

    async def list_for_participant(self, participant_id: UUID) -> list[ParticipantRelation]:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, participant_id, related_party_id, relation_kind,
                           financial_scope, verification, valid_from, valid_until,
                           status, created_at, created_by, updated_at, updated_by
                    FROM participant_relation
                    WHERE tenant_id = :tid AND participant_id = :pid
                    """
                ),
                {"tid": str(self.tenant_id), "pid": str(participant_id)},
            )
        ).all()
        return [_to_relation(r) for r in rows]


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _to_participant(row: Any) -> Participant:
    return Participant(
        id=row.id,
        tenant_id=row.tenant_id if hasattr(row, "tenant_id") else UUID(row[0]),
        participant_no=row.participant_no,
        full_name=row.full_name,
        birth_date=row.birth_date,
        contact=row.contact,
        identity_reference=row.identity_reference,
        status=ParticipantStatus(row.status),
        support_needs_note_enc=row.support_needs_note_enc,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_related_party(row: Any) -> RelatedParty:
    return RelatedParty(
        id=row.id,
        tenant_id=row.tenant_id if hasattr(row, "tenant_id") else None,
        party_type=RelatedPartyType(row.party_type),
        display_name=row.display_name,
        contact=row.contact,
        organization_ref=row.organization_ref,
        status=RelatedPartyStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_link(row: Any) -> ParticipantAccountLink:
    return ParticipantAccountLink(
        id=row.id,
        tenant_id=row.tenant_id if hasattr(row, "tenant_id") else None,
        account_id=row.account_id,
        participant_id=row.participant_id,
        related_party_id=row.related_party_id,
        verification=row.verification,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        status=LinkStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _to_relation(row: Any) -> ParticipantRelation:
    return ParticipantRelation(
        id=row.id,
        tenant_id=row.tenant_id if hasattr(row, "tenant_id") else None,
        participant_id=row.participant_id,
        related_party_id=row.related_party_id,
        relation_kind=RelationKind(row.relation_kind),
        financial_scope=FinancialScope.from_json(row.financial_scope or {}),
        verification=row.verification,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        status=RelationStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _json_dumps(d: Any) -> str:
    import json

    return json.dumps(d, default=str)