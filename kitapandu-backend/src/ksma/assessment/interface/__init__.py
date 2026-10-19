"""Combined FastAPI router for assessment + reporting."""
from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ksma.assessment.application import (
    attach_evidence,
    create_assessment,
    decide_completion,
    draft_report,
    issue_certificate,
    publish_report,
    replace_certificate,
    review_assessment,
    revoke_certificate,
    revoke_report,
    set_result,
    submit_assessment,
    supersede_assessment,
    supersede_report,
)
from ksma.assessment.domain import ResultState
from ksma.assessment.infrastructure.repositories import (
    AssessmentEvidenceRepository,
    AssessmentRepository,
    AssessmentResultRepository,
)
from ksma.reporting.domain import CompletionOutcome
from ksma.reporting.infrastructure.repositories import (
    CertificateRepository,
    CompletionDecisionRepository,
    ReportPublicationRepository,
)
from ksma.shared.persistence import session_scope
from ksma.shared.tenancy import TenantContext

router = APIRouter(prefix="/api/v1", tags=["assessment", "reporting"])


def require_tenant_context() -> TenantContext:  # pragma: no cover
    raise NotImplementedError


# --- Assessment schemas ------------------------------------------------------


class CreateAssessmentBody(BaseModel):
    unit_id: UUID
    enrollment_id: UUID
    rubric_version_id: UUID
    assessor_id: UUID
    assessment_date: date | None = None


class AssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    enrollment_id: UUID
    rubric_version_id: UUID
    assessor_id: UUID
    assessment_date: date
    status: str
    supersedes_id: UUID | None = None


class SetResultBody(BaseModel):
    indicator_id: UUID
    numeric_value: float | None = None
    category_code: str | None = None
    narrative_value: str | None = None
    result_state: ResultState | None = None
    follow_up: str | None = None


class AssessmentResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    assessment_id: UUID
    indicator_id: UUID
    numeric_value: float | None = None
    category_code: str | None = None
    narrative_value: str | None = None
    result_state: str
    follow_up: str | None = None


class AttachEvidenceBody(BaseModel):
    attachment_id: UUID
    caption: str | None = Field(default=None, max_length=1000)
    result_id: UUID | None = None


class AssessmentEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    assessment_id: UUID
    attachment_id: UUID
    caption: str | None = None


# --- Reporting schemas -------------------------------------------------------


class DraftReportBody(BaseModel):
    unit_id: UUID
    enrollment_id: UUID
    source_assessment_ids: list[UUID]
    snapshot: dict[str, Any]


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    enrollment_id: UUID
    publication_no: int
    source_assessment_ids: list[UUID]
    snapshot: dict[str, Any]
    publication: dict[str, Any]
    status: str
    supersedes_id: UUID | None = None


class RevokeBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class DecideCompletionBody(BaseModel):
    unit_id: UUID
    enrollment_id: UUID
    outcome: CompletionOutcome
    evidence_refs: list[dict[str, Any]]


class CompletionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    enrollment_id: UUID
    outcome: str
    evidence_refs: list[dict[str, Any]]
    decision: dict[str, Any]
    supersedes_id: UUID | None = None


class IssueCertificateBody(BaseModel):
    unit_id: UUID
    completion_id: UUID
    numbering_scope: str = Field(default="default", max_length=80)


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    completion_id: UUID
    numbering_scope: str
    certificate_no: str
    snapshot: dict[str, Any]
    status: str
    replacement_id: UUID | None = None


# --- Assessment endpoints ----------------------------------------------------


@router.post("/assessments", response_model=AssessmentOut, status_code=status.HTTP_201_CREATED)
async def post_assessment(
    body: CreateAssessmentBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AssessmentOut:
    async with session_scope() as session:
        a = await create_assessment(
            session,
            ctx,
            unit_id=body.unit_id,
            enrollment_id=body.enrollment_id,
            rubric_version_id=body.rubric_version_id,
            assessor_id=body.assessor_id,
            assessment_date=body.assessment_date,
        )
        return AssessmentOut(
            id=a.id,
            enrollment_id=a.enrollment_id,
            rubric_version_id=a.rubric_version_id,
            assessor_id=a.assessor_id,
            assessment_date=a.assessment_date,
            status=a.status.value,
            supersedes_id=a.supersedes_id,
        )


@router.get("/assessments/{assessment_id}", response_model=AssessmentOut)
async def get_assessment(
    assessment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AssessmentOut:
    async with session_scope() as session:
        repo = AssessmentRepository(session, ctx.tenant_id)
        a = await repo.get(assessment_id)
        if a is None:
            raise HTTPException(status_code=404, detail="not found")
        return AssessmentOut(
            id=a.id,
            enrollment_id=a.enrollment_id,
            rubric_version_id=a.rubric_version_id,
            assessor_id=a.assessor_id,
            assessment_date=a.assessment_date,
            status=a.status.value,
            supersedes_id=a.supersedes_id,
        )


@router.post(
    "/assessments/{assessment_id}/results",
    response_model=AssessmentResultOut,
)
async def post_result(
    assessment_id: UUID,
    body: SetResultBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AssessmentResultOut:
    async with session_scope() as session:
        r = await set_result(
            session,
            ctx,
            assessment_id=assessment_id,
            indicator_id=body.indicator_id,
            numeric_value=body.numeric_value,
            category_code=body.category_code,
            narrative_value=body.narrative_value,
            result_state=body.result_state,
            follow_up=body.follow_up,
        )
        return AssessmentResultOut(
            id=r.id,
            assessment_id=r.assessment_id,
            indicator_id=r.indicator_id,
            numeric_value=r.numeric_value,
            category_code=r.category_code,
            narrative_value=r.narrative_value,
            result_state=r.result_state.value,
            follow_up=r.follow_up,
        )


@router.get(
    "/assessments/{assessment_id}/results",
    response_model=list[AssessmentResultOut],
)
async def get_results(
    assessment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[AssessmentResultOut]:
    async with session_scope() as session:
        repo = AssessmentResultRepository(session, ctx.tenant_id)
        rows = await repo.list_for_assessment(assessment_id)
        return [
            AssessmentResultOut(
                id=r.id,
                assessment_id=r.assessment_id,
                indicator_id=r.indicator_id,
                numeric_value=r.numeric_value,
                category_code=r.category_code,
                narrative_value=r.narrative_value,
                result_state=r.result_state.value,
                follow_up=r.follow_up,
            )
            for r in rows
        ]


@router.post("/assessments/{assessment_id}/submit", status_code=status.HTTP_204_NO_CONTENT)
async def post_submit_assessment(
    assessment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await submit_assessment(session, ctx, assessment_id=assessment_id)


@router.post("/assessments/{assessment_id}/review", status_code=status.HTTP_204_NO_CONTENT)
async def post_review_assessment(
    assessment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await review_assessment(session, ctx, assessment_id=assessment_id)


@router.post(
    "/assessments/{assessment_id}/supersede",
    response_model=AssessmentOut,
)
async def post_supersede_assessment(
    assessment_id: UUID,
    new_rubric_version_id: UUID | None = None,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AssessmentOut:
    async with session_scope() as session:
        a = await supersede_assessment(
            session,
            ctx,
            assessment_id=assessment_id,
            new_rubric_version_id=new_rubric_version_id,
        )
        return AssessmentOut(
            id=a.id,
            enrollment_id=a.enrollment_id,
            rubric_version_id=a.rubric_version_id,
            assessor_id=a.assessor_id,
            assessment_date=a.assessment_date,
            status=a.status.value,
            supersedes_id=a.supersedes_id,
        )


@router.post(
    "/assessments/{assessment_id}/evidence",
    response_model=AssessmentEvidenceOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_evidence(
    assessment_id: UUID,
    body: AttachEvidenceBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> AssessmentEvidenceOut:
    async with session_scope() as session:
        e = await attach_evidence(
            session,
            ctx,
            assessment_id=assessment_id,
            attachment_id=body.attachment_id,
            caption=body.caption,
            result_id=body.result_id,
        )
        return AssessmentEvidenceOut(
            id=e.id,
            assessment_id=e.assessment_id,
            attachment_id=e.attachment_id,
            caption=e.caption,
        )


@router.get(
    "/enrollments/{enrollment_id}/assessments",
    response_model=list[AssessmentOut],
)
async def get_enrollment_assessments(
    enrollment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[AssessmentOut]:
    async with session_scope() as session:
        repo = AssessmentRepository(session, ctx.tenant_id)
        rows = await repo.list_for_enrollment(enrollment_id)
        return [
            AssessmentOut(
                id=a.id,
                enrollment_id=a.enrollment_id,
                rubric_version_id=a.rubric_version_id,
                assessor_id=a.assessor_id,
                assessment_date=a.assessment_date,
                status=a.status.value,
                supersedes_id=a.supersedes_id,
            )
            for a in rows
        ]


# --- Reporting endpoints -----------------------------------------------------


@router.post("/reports", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
async def post_report(
    body: DraftReportBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ReportOut:
    async with session_scope() as session:
        r = await draft_report(
            session,
            ctx,
            unit_id=body.unit_id,
            enrollment_id=body.enrollment_id,
            source_assessment_ids=body.source_assessment_ids,
            snapshot=body.snapshot,
        )
        return ReportOut(
            id=r.id,
            enrollment_id=r.enrollment_id,
            publication_no=r.publication_no,
            source_assessment_ids=r.source_assessment_ids,
            snapshot=r.snapshot,
            publication=r.publication,
            status=r.status.value,
            supersedes_id=r.supersedes_id,
        )


@router.post("/reports/{publication_id}/publish", status_code=status.HTTP_204_NO_CONTENT)
async def post_publish_report(
    publication_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await publish_report(session, ctx, publication_id=publication_id)


@router.post("/reports/{publication_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def post_revoke_report(
    publication_id: UUID,
    body: RevokeBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await revoke_report(session, ctx, publication_id=publication_id, reason=body.reason)


@router.post(
    "/reports/{publication_id}/supersede",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_supersede_report(
    publication_id: UUID,
    new_publication_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await supersede_report(
            session,
            ctx,
            old_publication_id=publication_id,
            new_publication_id=new_publication_id,
        )


@router.get("/reports/{publication_id}", response_model=ReportOut)
async def get_report(
    publication_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> ReportOut:
    async with session_scope() as session:
        repo = ReportPublicationRepository(session, ctx.tenant_id)
        r = await repo.get(publication_id)
        if r is None:
            raise HTTPException(status_code=404, detail="not found")
        return ReportOut(
            id=r.id,
            enrollment_id=r.enrollment_id,
            publication_no=r.publication_no,
            source_assessment_ids=r.source_assessment_ids,
            snapshot=r.snapshot,
            publication=r.publication,
            status=r.status.value,
            supersedes_id=r.supersedes_id,
        )


@router.get(
    "/enrollments/{enrollment_id}/reports",
    response_model=list[ReportOut],
)
async def get_enrollment_reports(
    enrollment_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> list[ReportOut]:
    async with session_scope() as session:
        repo = ReportPublicationRepository(session, ctx.tenant_id)
        rows = await repo.list_for_enrollment(enrollment_id)
        return [
            ReportOut(
                id=r.id,
                enrollment_id=r.enrollment_id,
                publication_no=r.publication_no,
                source_assessment_ids=r.source_assessment_ids,
                snapshot=r.snapshot,
                publication=r.publication,
                status=r.status.value,
                supersedes_id=r.supersedes_id,
            )
            for r in rows
        ]


@router.post("/completions", response_model=CompletionOut, status_code=status.HTTP_201_CREATED)
async def post_completion(
    body: DecideCompletionBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> CompletionOut:
    async with session_scope() as session:
        c = await decide_completion(
            session,
            ctx,
            unit_id=body.unit_id,
            enrollment_id=body.enrollment_id,
            outcome=body.outcome,
            evidence_refs=body.evidence_refs,
        )
        return CompletionOut(
            id=c.id,
            enrollment_id=c.enrollment_id,
            outcome=c.outcome.value,
            evidence_refs=c.evidence_refs,
            decision=c.decision,
            supersedes_id=c.supersedes_id,
        )


@router.post(
    "/certificates",
    response_model=CertificateOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_certificate(
    body: IssueCertificateBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> CertificateOut:
    async with session_scope() as session:
        cert = await issue_certificate(
            session,
            ctx,
            unit_id=body.unit_id,
            completion_id=body.completion_id,
            numbering_scope=body.numbering_scope,
        )
        return CertificateOut(
            id=cert.id,
            completion_id=cert.completion_id,
            numbering_scope=cert.numbering_scope,
            certificate_no=cert.certificate_no,
            snapshot=cert.snapshot,
            status=cert.status.value,
            replacement_id=cert.replacement_id,
        )


@router.post(
    "/certificates/{cert_id}/replace",
    response_model=CertificateOut,
)
async def post_replace_certificate(
    cert_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> CertificateOut:
    async with session_scope() as session:
        cert = await replace_certificate(session, ctx, old_certificate_id=cert_id)
        return CertificateOut(
            id=cert.id,
            completion_id=cert.completion_id,
            numbering_scope=cert.numbering_scope,
            certificate_no=cert.certificate_no,
            snapshot=cert.snapshot,
            status=cert.status.value,
            replacement_id=cert.replacement_id,
        )


@router.post(
    "/certificates/{cert_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def post_revoke_certificate(
    cert_id: UUID,
    body: RevokeBody,
    ctx: TenantContext = Depends(require_tenant_context),
) -> None:
    async with session_scope() as session:
        await revoke_certificate(session, ctx, certificate_id=cert_id, reason=body.reason)


@router.get(
    "/certificates/{cert_id}",
    response_model=CertificateOut,
)
async def get_certificate(
    cert_id: UUID,
    ctx: TenantContext = Depends(require_tenant_context),
) -> CertificateOut:
    async with session_scope() as session:
        repo = CertificateRepository(session, ctx.tenant_id)
        cert = await repo.get(cert_id)
        if cert is None:
            raise HTTPException(status_code=404, detail="not found")
        return CertificateOut(
            id=cert.id,
            completion_id=cert.completion_id,
            numbering_scope=cert.numbering_scope,
            certificate_no=cert.certificate_no,
            snapshot=cert.snapshot,
            status=cert.status.value,
            replacement_id=cert.replacement_id,
        )