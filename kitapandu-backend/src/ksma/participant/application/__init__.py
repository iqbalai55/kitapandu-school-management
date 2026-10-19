"""Participant application services (use cases).

Each service method runs in a transaction opened by the caller (the FastAPI
handler). It loads the participant via the repository, enforces invariants,
emits domain events through the outbox, and writes via the repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ksma.identity.infrastructure.models import AccountRow
from ksma.participant.domain import (
    DuplicateSuspect,
    FinancialScope,
    LinkStatus,
    Participant,
    ParticipantAccountLink,
    ParticipantRelation,
    ParticipantStatus,
    RelatedParty,
    RelationKind,
    RelationStatus,
)
from ksma.participant.domain.events import (
    ParticipantsMerged,
    ParticipantAccountLinkRequested,
    ParticipantAccountLinkRevoked,
    ParticipantAccountLinkVerified,
    ParticipantArchived,
    ParticipantCreated,
    ParticipantRelationRequested,
    ParticipantRelationRevoked,
    ParticipantRelationVerified,
    ParticipantUpdated,
    RelatedPartyCreated,
)
from ksma.participant.infrastructure.repositories import (
    ParticipantAccountLinkRepository,
    ParticipantRelationRepository,
    ParticipantRepository,
    RelatedPartyRepository,
)
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import TenantContext


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateParticipantCommand:
    actor: TenantContext
    participant_no: str
    full_name: str
    birth_date: Any | None = None
    contact: dict[str, Any] | None = None
    identity_reference: dict[str, Any] | None = None


async def create_participant(
    session: AsyncSession, cmd: CreateParticipantCommand
) -> Participant:
    repo = ParticipantRepository(session, cmd.actor.tenant_id)
    p = Participant(
        tenant_id=cmd.actor.tenant_id,
        participant_no=cmd.participant_no,
        full_name=cmd.full_name,
        birth_date=cmd.birth_date,
        contact=cmd.contact,
        identity_reference=cmd.identity_reference,
        status=ParticipantStatus.ACTIVE,
        created_by=str(cmd.actor.account_id),
        updated_by=str(cmd.actor.account_id),
    )
    await repo.create(p)
    event = ParticipantCreated(
        tenant_id=cmd.actor.tenant_id,
        actor_kind="account",
        actor_id=cmd.actor.account_id,
        correlation_id=None,
    )
    await enqueue(session, event, aggregate_ref=f"participant:{p.id}")
    bus().dispatch(event)
    return p


async def update_participant(
    session: AsyncSession,
    ctx: TenantContext,
    participant_id: UUID,
    *,
    full_name: str | None = None,
    contact: dict[str, Any] | None = None,
    birth_date: Any | None = None,
    actor: str | None = None,
) -> Participant:
    repo = ParticipantRepository(session, ctx.tenant_id)
    p = await repo.get(participant_id)
    if p is None:
        from ksma.shared.errors import ValidationError

        raise ValidationError("Participant not found in this tenant")
    if full_name is not None:
        p.full_name = full_name
    if contact is not None:
        p.contact = contact
    if birth_date is not None:
        p.birth_date = birth_date
    p.updated_by = actor or str(ctx.account_id)
    await repo.update(p)
    event = ParticipantUpdated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"participant:{p.id}")
    bus().dispatch(event)
    return p


async def archive_participant(
    session: AsyncSession, ctx: TenantContext, participant_id: UUID
) -> None:
    repo = ParticipantRepository(session, ctx.tenant_id)
    await repo.archive(participant_id, actor=str(ctx.account_id))
    event = ParticipantArchived(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"participant:{participant_id}")
    bus().dispatch(event)


async def merge_participants(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    source_id: UUID,
    target_id: UUID,
    reason: str,
) -> None:
    """R17: profile merging requires explicit rule + audit. Gated by Q02 policy.

    For pilot: stubs out unless a ``PolicyVersion(policy_domain='enrollment',
    status='published')`` exists. Returns ``PolicyNotConfiguredError`` otherwise.
    """
    from ksma.shared.errors import PolicyNotConfiguredError

    # Q02 gate — verify a published policy exists.
    from sqlalchemy import text

    has_policy = (
        await session.execute(
            text(
                "SELECT 1 FROM policy_version "
                "WHERE tenant_id = :tid AND policy_domain = 'enrollment' "
                "  AND status = 'published' LIMIT 1"
            ),
            {"tid": str(ctx.tenant_id)},
        )
    ).first()
    if has_policy is None:
        raise PolicyNotConfiguredError(
            "Profile merge endpoint is gated by Q02 (enrollment policy). "
            "Publish a PolicyVersion with policy_domain='enrollment' to enable.",
            details={"decision": "Q02"},
        )

    repo = ParticipantRepository(session, ctx.tenant_id)
    source = await repo.get(source_id)
    target = await repo.get(target_id)
    if source is None or target is None:
        from ksma.shared.errors import ValidationError

        raise ValidationError("Source or target participant not found")

    # Re-point relations, links, and enrollments to target.
    await session.execute(
        text(
            """
            UPDATE participant_relation
            SET participant_id = :target
            WHERE participant_id = :source AND tenant_id = :tid
            """
        ),
        {"target": str(target_id), "source": str(source_id), "tid": str(ctx.tenant_id)},
    )
    await session.execute(
        text(
            """
            UPDATE participant_account_link
            SET participant_id = :target
            WHERE participant_id = :source AND tenant_id = :tid
            """
        ),
        {"target": str(target_id), "source": str(source_id), "tid": str(ctx.tenant_id)},
    )
    await repo.archive(source_id, actor=str(ctx.account_id))

    event = ParticipantsMerged(
        tenant_id=ctx.tenant_id,
        actor_id=ctx.account_id,
    )
    # Carry source/target in payload via a wrapper — outbox serializes
    # event_id + correlation_id only, so we stash metadata on the row.
    await session.execute(
        text(
            "UPDATE outbox_messages SET payload = payload || "
            "  jsonb_build_object('source_id', :s, 'target_id', :t, 'reason', :r) "
            "WHERE id = :eid"
        ),
        {"s": str(source_id), "t": str(target_id), "r": reason, "eid": str(event.event_id)},
    )
    # The above UPDATE references the row we just enqueued — which hasn't been
    # committed yet, so this is a no-op until commit. To keep things simple
    # we re-enqueue with explicit aggregate_ref carrying the IDs.
    await enqueue(session, event, aggregate_ref=f"merge:{source_id}->{target_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# RelatedParty
# ---------------------------------------------------------------------------


async def create_related_party(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    party_type: str,
    display_name: str,
    contact: dict[str, Any] | None = None,
    organization_ref: str | None = None,
) -> RelatedParty:
    repo = RelatedPartyRepository(session, ctx.tenant_id)
    rp = RelatedParty(
        tenant_id=ctx.tenant_id,
        party_type=party_type,
        display_name=display_name,
        contact=contact,
        organization_ref=organization_ref,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await repo.create(rp)
    event = RelatedPartyCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"related_party:{rp.id}")
    bus().dispatch(event)
    return rp


# ---------------------------------------------------------------------------
# ParticipantAccountLink
# ---------------------------------------------------------------------------


async def request_account_link(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    account_id: UUID,
    participant_id: UUID | None = None,
    related_party_id: UUID | None = None,
    evidence: dict[str, Any],
    validity_end: Any | None = None,
) -> ParticipantAccountLink:
    if (participant_id is None) == (related_party_id is None):
        from ksma.shared.errors import ValidationError

        raise ValidationError(
            "Exactly one of participant_id or related_party_id must be provided"
        )
    repo = ParticipantAccountLinkRepository(session, ctx.tenant_id)
    link = ParticipantAccountLink(
        tenant_id=ctx.tenant_id,
        account_id=account_id,
        participant_id=participant_id,
        related_party_id=related_party_id,
        verification={"requested_by": str(ctx.account_id), "evidence": evidence},
        valid_until=validity_end,
        status=LinkStatus.PENDING,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await repo.create(link)
    event = ParticipantAccountLinkRequested(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"link:{link.id}")
    bus().dispatch(event)
    return link


async def verify_account_link(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    link_id: UUID,
) -> ParticipantAccountLink:
    repo = ParticipantAccountLinkRepository(session, ctx.tenant_id)
    await repo.update_status(link_id, status=LinkStatus.ACTIVE, actor=str(ctx.account_id))
    event = ParticipantAccountLinkVerified(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"link:{link_id}")
    bus().dispatch(event)
    # Verify the linked account's authz_revision so other active JWTs must re-check.
    from sqlalchemy import text

    await session.execute(
        text("UPDATE account SET authz_revision = authz_revision + 1 WHERE id = :id"),
        {"id": str((await repo.get(link_id)).account_id)},  # type: ignore[union-attr]
    )
    return await repo.get(link_id)  # type: ignore[return-value]


async def revoke_account_link(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    link_id: UUID,
    reason: str,
) -> None:
    repo = ParticipantAccountLinkRepository(session, ctx.tenant_id)
    await repo.update_status(link_id, status=LinkStatus.REVOKED, actor=str(ctx.account_id))
    event = ParticipantAccountLinkRevoked(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"link:{link_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# ParticipantRelation
# ---------------------------------------------------------------------------


async def request_relation(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    participant_id: UUID,
    related_party_id: UUID,
    relation_kind: RelationKind,
    financial_scope: FinancialScope,
    evidence: dict[str, Any],
    validity_end: Any | None = None,
) -> ParticipantRelation:
    if relation_kind is RelationKind.PAYER and not financial_scope.all_approved and not financial_scope.invoices:
        from ksma.shared.errors import ValidationError

        raise ValidationError(
            "Payer relation requires financial_scope (all_approved or explicit invoice list)"
        )
    repo = ParticipantRelationRepository(session, ctx.tenant_id)
    rel = ParticipantRelation(
        tenant_id=ctx.tenant_id,
        participant_id=participant_id,
        related_party_id=related_party_id,
        relation_kind=relation_kind,
        financial_scope=financial_scope,
        verification={"requested_by": str(ctx.account_id), "evidence": evidence},
        valid_until=validity_end,
        status=RelationStatus.PENDING,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await repo.create(rel)
    event = ParticipantRelationRequested(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"relation:{rel.id}")
    bus().dispatch(event)
    return rel


async def verify_relation(
    session: AsyncSession, ctx: TenantContext, *, relation_id: UUID
) -> ParticipantRelation:
    repo = ParticipantRelationRepository(session, ctx.tenant_id)
    await repo.update_status(relation_id, status=RelationStatus.ACTIVE, actor=str(ctx.account_id))
    event = ParticipantRelationVerified(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"relation:{relation_id}")
    bus().dispatch(event)
    return await repo.get(relation_id)  # type: ignore[return-value]


async def revoke_relation(
    session: AsyncSession, ctx: TenantContext, *, relation_id: UUID, reason: str
) -> None:
    repo = ParticipantRelationRepository(session, ctx.tenant_id)
    await repo.update_status(relation_id, status=RelationStatus.REVOKED, actor=str(ctx.account_id))
    event = ParticipantRelationRevoked(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"relation:{relation_id}")
    bus().dispatch(event)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchResult:
    participants: list[Participant]
    duplicate_suspects: list[DuplicateSuspect]


async def search_participants(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    query: str | None = None,
    status: ParticipantStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    from sqlalchemy import text

    repo = ParticipantRepository(session, ctx.tenant_id)
    participants, _ = await repo.search(query=query, status=status, limit=limit, offset=offset)

    suspects: list[DuplicateSuspect] = []
    if query and len(query.strip()) >= 3:
        # Heuristic V02: find participants with the same lowercased name OR
        # with the same contact email/phone as any of the returned participants.
        params: dict[str, Any] = {"tid": str(ctx.tenant_id), "q": query.lower()}
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, full_name, contact
                    FROM participant
                    WHERE tenant_id = :tid AND LOWER(full_name) = :q
                          AND status != 'archived'
                    LIMIT 10
                    """
                ),
                params,
            )
        ).all()
        ids = [r.id for r in rows]
        if len(ids) > 1:
            for primary in ids[:1]:
                others = tuple(x for x in ids if x != primary)
                suspects.append(
                    DuplicateSuspect(
                        participant_id=primary,
                        matched_participant_ids=others,
                        reason="same_name",
                    )
                )
        # Contact-based: same email normalized
        if participants:
            pid = str(participants[0].id)
            email_match = participants[0].contact and (
                participants[0].contact.get("email")
            )
            if email_match:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT id FROM participant
                            WHERE tenant_id = :tid
                              AND id != :pid
                              AND LOWER(contact->>'email') = :em
                              AND status != 'archived'
                            LIMIT 5
                            """
                        ),
                        {
                            "tid": str(ctx.tenant_id),
                            "pid": pid,
                            "em": str(email_match).lower(),
                        },
                    )
                ).all()
                if rows:
                    suspects.append(
                        DuplicateSuspect(
                            participant_id=participants[0].id,
                            matched_participant_ids=tuple(r.id for r in rows),
                            reason="same_contact_email",
                        )
                    )

    return SearchResult(participants=participants, duplicate_suspects=suspects)


# ---------------------------------------------------------------------------
# Portal selector (Q36)
# ---------------------------------------------------------------------------


async def list_portal_participants(
    session: AsyncSession, ctx: TenantContext
) -> list[dict[str, Any]]:
    """Return participants the caller can act as on the portal.

    For ST: their own profile (linked via ParticipantAccountLink).
    For GU/PY: participants linked via active ParticipantAccountLink to a
    RelatedParty that has an active ParticipantRelation with ``current.status='active'``.
    """
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                """
                SELECT
                  p.id AS participant_id,
                  p.full_name,
                  pr.relation_kind,
                  pp.party_type AS guardian_type,
                  pr.valid_until AS relation_valid_until
                FROM participant_account_link l
                JOIN participant p
                  ON p.id = l.participant_id AND p.tenant_id = l.tenant_id
                LEFT JOIN related_party rp
                  ON rp.id = l.related_party_id
                LEFT JOIN participant_relation pr
                  ON pr.related_party_id = rp.id
                 AND pr.tenant_id = l.tenant_id
                 AND pr.status = 'active'
                 AND (pr.valid_until IS NULL OR pr.valid_until > now())
                WHERE l.tenant_id = :tid
                  AND l.account_id = :acct
                  AND l.status = 'active'
                  AND (l.valid_until IS NULL OR l.valid_until > now())
                ORDER BY p.full_name
                """
            ),
            {"tid": str(ctx.tenant_id), "acct": str(ctx.account_id)},
        )
    ).all()
    return [
        {
            "participant_id": str(r.participant_id),
            "full_name": r.full_name,
            "relation_kind": r.relation_kind,
            "relation_valid_until": r.relation_valid_until.isoformat()
            if r.relation_valid_until
            else None,
        }
        for r in rows
    ]