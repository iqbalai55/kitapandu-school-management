"""Participant FastAPI router.

All routes protected via ``require_action`` from ``shared.rbac``. Tenant context
is resolved from the JWT and used to set RLS GUCs on the session.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ksma.participant.application import (
    CreateParticipantCommand,
    archive_participant,
    create_participant,
    create_related_party,
    list_portal_participants,
    merge_participants,
    request_account_link,
    request_relation,
    revoke_account_link,
    revoke_relation,
    search_participants,
    update_participant,
    verify_account_link,
    verify_relation,
)
from ksma.participant.domain import (
    FinancialScope,
    ParticipantStatus,
    RelatedPartyType,
    RelationKind,
)
from ksma.participant.infrastructure.repositories import ParticipantRepository
from ksma.shared.errors import ValidationError
from ksma.shared.persistence import session_scope
from ksma.shared.rbac import ActionCode, Target, evaluate
from ksma.shared.tenancy import TenantContext
from ksma.shared.auth.dependencies import tenant_context_dep as require_tenant_context

router = APIRouter(prefix="/api/v1", tags=["participant"])


# --- Schemas -----------------------------------------------------------------


class CreateParticipantBody(BaseModel):
    participant_no: str = Field(min_length=1, max_length=40)
    full_name: str = Field(min_length=1, max_length=200)
    birth_date: date | None = None
    contact: dict[str, Any] | None = None
    identity_reference: dict[str, Any] | None = None


class UpdateParticipantBody(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    birth_date: date | None = None
    contact: dict[str, Any] | None = None


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    participant_no: str
    full_name: str
    status: str
    birth_date: date | None = None
    contact: dict[str, Any] | None = None
    identity_reference: dict[str, Any] | None = None
    has_support_needs: bool = False


class DuplicateSuspectOut(BaseModel):
    participant_id: UUID
    matched_participant_ids: list[UUID]
    reason: str


class SearchResponse(BaseModel):
    items: list[ParticipantOut]
    duplicate_suspects: list[DuplicateSuspectOut] = []
    next_offset: int | None = None


class CreateRelatedPartyBody(BaseModel):
    party_type: RelatedPartyType
    display_name: str = Field(min_length=1, max_length=200)
    contact: dict[str, Any] | None = None
    organization_ref: str | None = Field(default=None, max_length=120)


class RelatedPartyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    party_type: str
    display_name: str
    contact: dict[str, Any] | None = None
    organization_ref: str | None = None
    status: str


class RequestAccountLinkBody(BaseModel):
    account_id: UUID
    participant_id: UUID | None = None
    related_party_id: UUID | None = None
    evidence: dict[str, Any]
    valid_until: datetime | None = None


class AccountLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    participant_id: UUID | None = None
    related_party_id: UUID | None = None
    status: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class RequestRelationBody(BaseModel):
    participant_id: UUID
    related_party_id: UUID
    relation_kind: RelationKind
    financial_scope_all_approved: bool = False
    financial_scope_invoices: list[UUID] = []
    evidence: dict[str, Any]
    valid_until: datetime | None = None


class RelationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    participant_id: UUID
    related_party_id: UUID
    relation_kind: str
    status: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class MergeBody(BaseModel):
    source_id: UUID
    target_id: UUID
    reason: str = Field(min_length=1, max_length=1000)


class PortalParticipantOut(BaseModel):
    participant_id: UUID
    full_name: str
    relation_kind: str | None = None
    relation_valid_until: datetime | None = None


# --- Tenant context dependency (filled in by main.py at startup) ------------



def require_action(action: ActionCode, target: Target = Target()):  # pragma: no cover
    """Provided by ``main.install_routes`` to evaluate RBAC before the handler."""
    raise NotImplementedError


# --- Routes ------------------------------------------------------------------


@router.post("/participants", response_model=ParticipantOut, status_code=status.HTTP_201_CREATED)
async def post_participants(
    body: CreateParticipantBody,
    ctx: TenantContext = Depends(require_tenant_context),
    _=Depends(lambda: None),  # placeholder; replaced in main.py with require_action
) -> ParticipantOut:
    """REG-A02 Create profile (AC)."""
    raise NotImplementedError  # wired in main.py with require_action(REG_A02)


@router.get("/participants", response_model=SearchResponse)
async def get_participants(
    q: str | None = Query(default=None, max_length=200),
    status_filter: ParticipantStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: TenantContext = Depends(require_tenant_context),
) -> SearchResponse:
    async with session_scope() as session:
        result = await search_participants(
            session,
            ctx,
            query=q,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        items = []
        for p in result.participants:
            items.append(
                ParticipantOut(
                    id=p.id,
                    participant_no=p.participant_no,
                    full_name=p.full_name,
                    status=p.status.value,
                    birth_date=p.birth_date,
                    contact=p.contact,
                    identity_reference=p.identity_reference,
                    has_support_needs=p.support_needs_note_enc is not None,
                )
            )
        return SearchResponse(
            items=items,
            duplicate_suspects=[
                DuplicateSuspectOut(
                    participant_id=s.participant_id,
                    matched_participant_ids=list(s.matched_participant_ids),
                    reason=s.reason,
                )
                for s in result.duplicate_suspects
            ],
            next_offset=offset + len(items) if len(items) == limit else None,
        )


@router.get("/participants/{participant_id}", response_model=ParticipantOut)
async def get_participant(
    participant_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ParticipantOut:
    async with session_scope() as session:
        repo = ParticipantRepository(session, ctx.tenant_id)
        p = await repo.get(participant_id)
        if p is None:
            raise HTTPException(status_code=404, detail="not found")
        return ParticipantOut(
            id=p.id,
            participant_no=p.participant_no,
            full_name=p.full_name,
            status=p.status.value,
            birth_date=p.birth_date,
            contact=p.contact,
            identity_reference=p.identity_reference,
            has_support_needs=p.support_needs_note_enc is not None,
        )


@router.post("/related-parties", response_model=RelatedPartyOut, status_code=status.HTTP_201_CREATED)
async def post_related_party(
    body: CreateRelatedPartyBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> RelatedPartyOut:
    async with session_scope() as session:
        rp = await create_related_party(
            session,
            ctx,
            party_type=body.party_type,
            display_name=body.display_name,
            contact=body.contact,
            organization_ref=body.organization_ref,
        )
        return RelatedPartyOut(
            id=rp.id,
            party_type=rp.party_type.value,
            display_name=rp.display_name,
            contact=rp.contact,
            organization_ref=rp.organization_ref,
            status=rp.status.value,
        )


@router.get("/related-parties", response_model=list[RelatedPartyOut])
async def get_related_parties(
    q: str | None = Query(default=None, max_length=200),
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[RelatedPartyOut]:
    async with session_scope() as session:
        from ksma.participant.infrastructure.repositories import RelatedPartyRepository

        repo = RelatedPartyRepository(session, ctx.tenant_id)
        rows = await repo.search(query=q)
        return [
            RelatedPartyOut(
                id=r.id,
                party_type=r.party_type.value,
                display_name=r.display_name,
                contact=r.contact,
                organization_ref=r.organization_ref,
                status=r.status.value,
            )
            for r in rows
        ]


@router.post(
    "/participants/{participant_id}/account-link",
    response_model=AccountLinkOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_account_link(
    participant_id: UUID,
    body: RequestAccountLinkBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AccountLinkOut:
    async with session_scope() as session:
        link = await request_account_link(
            session,
            ctx,
            account_id=body.account_id,
            participant_id=body.participant_id or participant_id,
            related_party_id=body.related_party_id,
            evidence=body.evidence,
            validity_end=body.valid_until,
        )
        return AccountLinkOut(
            id=link.id,
            account_id=link.account_id,
            participant_id=link.participant_id,
            related_party_id=link.related_party_id,
            status=link.status.value,
            valid_from=link.valid_from,
            valid_until=link.valid_until,
        )


@router.post(
    "/participants/{participant_id}/account-link/{link_id}/verify",
    response_model=AccountLinkOut,
)
async def post_verify_account_link(
    participant_id: UUID,
    link_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AccountLinkOut:
    async with session_scope() as session:
        link = await verify_account_link(session, ctx, link_id=link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="not found")
        return AccountLinkOut(
            id=link.id,
            account_id=link.account_id,
            participant_id=link.participant_id,
            related_party_id=link.related_party_id,
            status=link.status.value,
            valid_from=link.valid_from,
            valid_until=link.valid_until,
        )


@router.post(
    "/participants/{participant_id}/account-link/{link_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_revoke_account_link(
    participant_id: UUID,
    link_id: UUID,
    reason: str = Query(min_length=1, max_length=500),
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await revoke_account_link(session, ctx, link_id=link_id, reason=reason)


@router.post(
    "/participants/{participant_id}/relations",
    response_model=RelationOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_relation(
    participant_id: UUID,
    body: RequestRelationBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> RelationOut:
    async with session_scope() as session:
        scope = FinancialScope(
            all_approved=body.financial_scope_all_approved,
            invoices=tuple(body.financial_scope_invoices),
        )
        rel = await request_relation(
            session,
            ctx,
            participant_id=body.participant_id,
            related_party_id=body.related_party_id,
            relation_kind=body.relation_kind,
            financial_scope=scope,
            evidence=body.evidence,
            validity_end=body.valid_until,
        )
        return RelationOut(
            id=rel.id,
            participant_id=rel.participant_id,
            related_party_id=rel.related_party_id,
            relation_kind=rel.relation_kind.value,
            status=rel.status.value,
            valid_from=rel.valid_from,
            valid_until=rel.valid_until,
        )


@router.post(
    "/participants/{participant_id}/relations/{relation_id}/verify",
    response_model=RelationOut,
)
async def post_verify_relation(
    participant_id: UUID,
    relation_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> RelationOut:
    async with session_scope() as session:
        rel = await verify_relation(session, ctx, relation_id=relation_id)
        if rel is None:
            raise HTTPException(status_code=404, detail="not found")
        return RelationOut(
            id=rel.id,
            participant_id=rel.participant_id,
            related_party_id=rel.related_party_id,
            relation_kind=rel.relation_kind.value,
            status=rel.status.value,
            valid_from=rel.valid_from,
            valid_until=rel.valid_until,
        )


@router.post(
    "/participants/{participant_id}/relations/{relation_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_revoke_relation(
    participant_id: UUID,
    relation_id: UUID,
    reason: str = Query(min_length=1, max_length=500),
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await revoke_relation(session, ctx, relation_id=relation_id, reason=reason)


@router.post("/participants/merge", status_code=status.HTTP_204_NO_CONTENT)
async def post_merge(
    body: MergeBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    """R17: profile merging requires published Q02 enrollment policy."""
    async with session_scope() as session:
        await merge_participants(
            session,
            ctx,
            source_id=body.source_id,
            target_id=body.target_id,
            reason=body.reason,
        )


@router.get("/me/portal-participants", response_model=list[PortalParticipantOut])
async def get_me_portal_participants(
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[PortalParticipantOut]:
    async with session_scope() as session:
        rows = await list_portal_participants(session, ctx)
        return [PortalParticipantOut(**r) for r in rows]