"""Assessment + Reporting application services.

Both contexts share use cases that close the academic lifecycle:
``create_assessment`` → ``set_result`` → ``submit_assessment`` →
``draft_report`` → ``publish_report`` → ``decide_completion`` →
``issue_certificate`` (with policy gates).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

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
from ksma.assessment.domain.events import (
    AssessmentCreated,
    AssessmentEvidenceAttached,
    AssessmentResultSet,
    AssessmentReviewed,
    AssessmentSubmitted,
    AssessmentSuperseded,
)
from ksma.assessment.infrastructure.repositories import (
    AssessmentEvidenceRepository,
    AssessmentRepository,
    AssessmentResultRepository,
)
from ksma.program.domain import IndicatorValueKind, RubricVersionStatus
from ksma.program.infrastructure.repositories import RubricRepository
from ksma.reporting.domain import (
    Certificate,
    CertificateStatus,
    CompletionDecision,
    CompletionOutcome,
    ReportPublication,
    ReportStatus,
)
from ksma.reporting.domain.events import (
    CertificateIssued,
    CertificateReplaced,
    CertificateRevoked,
    CompletionDecided,
    ReportDrafted,
    ReportPublished,
    ReportRevoked,
    ReportSuperseded,
)
from ksma.reporting.infrastructure.repositories import (
    CertificateRepository,
    CompletionDecisionRepository,
    ReportPublicationRepository,
)
from ksma.shared.errors import (
    PolicyNotConfiguredError,
    StateConflictError,
    ValidationError,
)
from ksma.shared.events import bus
from ksma.shared.outbox import enqueue
from ksma.shared.tenancy import TenantContext
from ksma.shared.time import clock


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


async def create_assessment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    enrollment_id: UUID,
    rubric_version_id: UUID,
    assessor_id: UUID,
    assessment_date: date | None = None,
) -> Assessment:
    rrepo = RubricRepository(session, ctx.tenant_id)
    rv = await rrepo.get_version(rubric_version_id)
    if rv is None:
        raise ValidationError("Rubric version not found")
    if rv.status is not RubricVersionStatus.PUBLISHED:
        raise StateConflictError("Cannot create an assessment on a non-published rubric")
    arepo = AssessmentRepository(session, ctx.tenant_id)
    a = Assessment(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        enrollment_id=enrollment_id,
        rubric_version_id=rubric_version_id,
        assessor_id=assessor_id,
        assessment_date=assessment_date or clock().today(),
        status=AssessmentStatus.DRAFT,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await arepo.create(a)
    event = AssessmentCreated(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"assessment:{a.id}")
    bus().dispatch(event)
    return a


async def set_result(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    assessment_id: UUID,
    indicator_id: UUID,
    numeric_value: float | None = None,
    category_code: str | None = None,
    narrative_value: str | None = None,
    result_state: ResultState | None = None,
    follow_up: str | None = None,
) -> AssessmentResult:
    arepo = AssessmentRepository(session, ctx.tenant_id)
    a = await arepo.get(assessment_id)
    if a is None:
        raise ValidationError("Assessment not found")
    if a.status is AssessmentStatus.SUPERSEDED:
        raise StateConflictError("Cannot edit a superseded assessment")
    rrepo = RubricRepository(session, ctx.tenant_id)
    indicators = await rrepo.list_indicators(a.rubric_version_id)
    indicator = next((i for i in indicators if i.id == indicator_id), None)
    if indicator is None:
        raise ValidationError("Indicator does not belong to this assessment's rubric")
    # V10: at most one typed value per indicator's value_kind.
    set_values = sum(
        1 for v in (numeric_value, category_code, narrative_value) if v is not None
    )
    if set_values > 1:
        raise ValidationError(
            "Assessment result accepts exactly one value type per V10"
        )
    if indicator.value_kind is IndicatorValueKind.NUMERIC and numeric_value is None:
        raise ValidationError("Numeric indicator expects numeric_value")
    if indicator.value_kind is IndicatorValueKind.CATEGORICAL and category_code is None:
        raise ValidationError("Categorical indicator expects category_code")
    if indicator.value_kind is IndicatorValueKind.NARRATIVE and narrative_value is None:
        raise ValidationError("Narrative indicator expects narrative_value")
    # Scale validation.
    if numeric_value is not None and not indicator.scale.validate_value(numeric_value):
        raise ValidationError(
            f"numeric_value {numeric_value} out of scale range "
            f"[{indicator.scale.min}, {indicator.scale.max}]"
        )
    if category_code is not None and not indicator.scale.validate_value(category_code):
        raise ValidationError(
            f"category_code {category_code!r} not in rubric scale categories"
        )
    rs = result_state or (
        ResultState.ASSESSED if set_values == 1 else ResultState.NOT_ASSESSED
    )
    result = AssessmentResult(
        tenant_id=ctx.tenant_id,
        assessment_id=assessment_id,
        indicator_id=indicator_id,
        numeric_value=numeric_value,
        category_code=category_code,
        narrative_value=narrative_value,
        result_state=rs,
        follow_up=follow_up,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    r_repo = AssessmentResultRepository(session, ctx.tenant_id)
    await r_repo.upsert(result)
    event = AssessmentResultSet(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"assessment_result:{result.id}")
    bus().dispatch(event)
    return result


async def submit_assessment(
    session: AsyncSession, ctx: TenantContext, *, assessment_id: UUID
) -> Assessment:
    arepo = AssessmentRepository(session, ctx.tenant_id)
    a = await arepo.get(assessment_id)
    if a is None:
        raise ValidationError("Assessment not found")
    if a.status is not AssessmentStatus.DRAFT:
        raise StateConflictError(
            f"Cannot submit assessment in status {a.status.value}"
        )
    # Required indicators must be filled.
    rrepo = RubricRepository(session, ctx.tenant_id)
    indicators = await rrepo.list_indicators(a.rubric_version_id)
    required = [i for i in indicators if i.is_required]
    r_repo = AssessmentResultRepository(session, ctx.tenant_id)
    results = await r_repo.list_for_assessment(assessment_id)
    by_indicator = {r.indicator_id: r for r in results}
    missing = [
        i for i in required
        if by_indicator.get(i.id) is None
        or by_indicator[i.id].result_state is ResultState.NOT_ASSESSED
    ]
    if missing:
        raise ValidationError(
            f"Cannot submit: {len(missing)} required indicators are not assessed",
            details={"missing_indicator_codes": [i.indicator_code for i in missing]},
        )
    await arepo.update_status(assessment_id, status=AssessmentStatus.SUBMITTED, actor=str(ctx.account_id))
    event = AssessmentSubmitted(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"assessment:{assessment_id}")
    bus().dispatch(event)
    a.status = AssessmentStatus.SUBMITTED
    return a


async def review_assessment(
    session: AsyncSession, ctx: TenantContext, *, assessment_id: UUID
) -> Assessment:
    arepo = AssessmentRepository(session, ctx.tenant_id)
    a = await arepo.get(assessment_id)
    if a is None:
        raise ValidationError("Assessment not found")
    if a.status is not AssessmentStatus.SUBMITTED:
        raise StateConflictError("Only submitted assessments can be reviewed")
    await arepo.update_status(assessment_id, status=AssessmentStatus.REVIEWED, actor=str(ctx.account_id))
    event = AssessmentReviewed(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"assessment:{assessment_id}")
    bus().dispatch(event)
    a.status = AssessmentStatus.REVIEWED
    return a


async def supersede_assessment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    assessment_id: UUID,
    new_rubric_version_id: UUID | None = None,
) -> Assessment:
    """Create a new Assessment pointing at the old via ``supersedes_id``."""
    arepo = AssessmentRepository(session, ctx.tenant_id)
    old = await arepo.get(assessment_id)
    if old is None:
        raise ValidationError("Assessment not found")
    new = Assessment(
        tenant_id=ctx.tenant_id,
        unit_id=old.unit_id,
        enrollment_id=old.enrollment_id,
        rubric_version_id=new_rubric_version_id or old.rubric_version_id,
        assessor_id=old.assessor_id,
        assessment_date=clock().today(),
        status=AssessmentStatus.DRAFT,
        supersedes_id=old.id,
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await arepo.create(new)
    await arepo.update_status(assessment_id, status=AssessmentStatus.SUPERSEDED, actor=str(ctx.account_id))
    event = AssessmentSuperseded(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"assessment:{assessment_id}")
    bus().dispatch(event)
    return new


async def attach_evidence(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    assessment_id: UUID,
    attachment_id: UUID,
    caption: str | None = None,
    result_id: UUID | None = None,
) -> AssessmentEvidence:
    arepo = AssessmentRepository(session, ctx.tenant_id)
    a = await arepo.get(assessment_id)
    if a is None:
        raise ValidationError("Assessment not found")
    e = AssessmentEvidence(
        tenant_id=ctx.tenant_id,
        assessment_id=assessment_id,
        result_id=result_id,
        attachment_id=attachment_id,
        caption=caption,
        visibility=EvidenceVisibility.INTERNAL_ASSESSMENT,
        created_by=str(ctx.account_id),
    )
    erepo = AssessmentEvidenceRepository(session, ctx.tenant_id)
    await erepo.create(e)
    event = AssessmentEvidenceAttached(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"assessment_evidence:{e.id}")
    bus().dispatch(event)
    return e


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


async def draft_report(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    enrollment_id: UUID,
    source_assessment_ids: list[UUID],
    snapshot: dict[str, Any],
) -> ReportPublication:
    prepo = ReportPublicationRepository(session, ctx.tenant_id)
    pub_no = await prepo.next_publication_no(enrollment_id)
    pub = ReportPublication(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        enrollment_id=enrollment_id,
        publication_no=pub_no,
        source_assessment_ids=source_assessment_ids,
        snapshot=snapshot,
        publication={"drafted_by": str(ctx.account_id), "drafted_at": clock().now().isoformat()},
        status=ReportStatus.PUBLISHED,  # 'draft' is internal source projection per §3.31
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await prepo.create(pub)
    event = ReportDrafted(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"report_publication:{pub.id}")
    bus().dispatch(event)
    return pub


async def publish_report(
    session: AsyncSession, ctx: TenantContext, *, publication_id: UUID
) -> ReportPublication:
    """Freeze the snapshot at publish time. Per §3.31, ``status='published'``."""
    prepo = ReportPublicationRepository(session, ctx.tenant_id)
    pub = await prepo.get(publication_id)
    if pub is None:
        raise ValidationError("Report publication not found")
    if pub.status is not ReportStatus.PUBLISHED:
        raise StateConflictError(f"Cannot publish a report in status {pub.status.value}")
    await session.execute(
        text(
            "UPDATE report_publication SET publication = publication || "
            "  jsonb_build_object('publisher', :a, 'published_at', :t) "
            "WHERE id = :id AND tenant_id = :tid"
        ),
        {
            "a": str(ctx.account_id),
            "t": clock().now().isoformat(),
            "id": str(publication_id),
            "tid": str(ctx.tenant_id),
        },
    )
    event = ReportPublished(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"report_publication:{publication_id}")
    bus().dispatch(event)
    return pub  # type: ignore[return-value]


async def supersede_report(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    old_publication_id: UUID,
    new_publication_id: UUID,
) -> None:
    prepo = ReportPublicationRepository(session, ctx.tenant_id)
    await prepo.update_status(old_publication_id, status=ReportStatus.SUPERSEDED, actor=str(ctx.account_id))
    await session.execute(
        text(
            "UPDATE report_publication SET supersedes_id=:o "
            "WHERE id=:n AND tenant_id=:tid"
        ),
        {"o": str(old_publication_id), "n": str(new_publication_id), "tid": str(ctx.tenant_id)},
    )
    event = ReportSuperseded(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"report_publication:{old_publication_id}")
    bus().dispatch(event)


async def revoke_report(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    publication_id: UUID,
    reason: str,
) -> None:
    if not reason:
        raise ValidationError("reason is required for revocation")
    prepo = ReportPublicationRepository(session, ctx.tenant_id)
    await prepo.update_status(publication_id, status=ReportStatus.REVOKED, actor=str(ctx.account_id))
    await session.execute(
        text(
            "UPDATE report_publication SET publication = publication || "
            "  jsonb_build_object('revocation', jsonb_build_object('actor', :a, 'reason', :r, 'at', :t)) "
            "WHERE id = :id AND tenant_id = :tid"
        ),
        {
            "a": str(ctx.account_id),
            "r": reason,
            "t": clock().now().isoformat(),
            "id": str(publication_id),
            "tid": str(ctx.tenant_id),
        },
    )
    event = ReportRevoked(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"report_publication:{publication_id}")
    bus().dispatch(event)


async def decide_completion(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    enrollment_id: UUID,
    outcome: CompletionOutcome,
    evidence_refs: list[dict[str, Any]],
) -> CompletionDecision:
    """BR09: completion requires valid evidence_refs (report_publication or assessment).

    Money or session count alone is NOT sufficient (BR09).
    """
    if not evidence_refs:
        raise ValidationError("evidence_refs is required (BR09)")
    allowed_kinds = {"report_publication", "assessment"}
    for ref in evidence_refs:
        if not isinstance(ref, dict) or ref.get("kind") not in allowed_kinds:
            raise ValidationError(
                f"evidence_refs must point to report_publication or assessment; got {ref!r}"
            )
    crepo = CompletionDecisionRepository(session, ctx.tenant_id)
    c = CompletionDecision(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        enrollment_id=enrollment_id,
        outcome=outcome,
        evidence_refs=evidence_refs,
        decision={"actor": str(ctx.account_id), "decided_at": clock().now().isoformat()},
        created_by=str(ctx.account_id),
        updated_by=str(ctx.account_id),
    )
    await crepo.create(c)
    event = CompletionDecided(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"completion_decision:{c.id}")
    bus().dispatch(event)
    return c


async def issue_certificate(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    unit_id: UUID,
    completion_id: UUID,
    numbering_scope: str = "default",
) -> Certificate:
    """Issue an internal certificate on a valid completion decision.

    Q12 deferred — pilot uses sequential numbering per tenant + scope.
    """
    crepo = CompletionDecisionRepository(session, ctx.tenant_id)
    completion = await crepo.get(completion_id)
    if completion is None:
        raise ValidationError("Completion decision not found")
    if completion.outcome is not CompletionOutcome.COMPLETED:
        raise ValidationError(
            f"Certificate requires outcome=completed; got {completion.outcome.value}"
        )
    certrepo = CertificateRepository(session, ctx.tenant_id)
    cert_no = await certrepo.next_certificate_no(numbering_scope)
    cert = Certificate(
        tenant_id=ctx.tenant_id,
        unit_id=unit_id,
        completion_id=completion_id,
        numbering_scope=numbering_scope,
        certificate_no=cert_no,
        snapshot={
            "completion_id": str(completion_id),
            "enrollment_id": str(completion.enrollment_id),
            "issued_at": clock().now().isoformat(),
            "publisher": str(ctx.account_id),
        },
        status=CertificateStatus.ISSUED,
        created_by=str(ctx.account_id),
    )
    await certrepo.create(cert)
    event = CertificateIssued(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"certificate:{cert.id}")
    bus().dispatch(event)
    return cert


async def replace_certificate(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    old_certificate_id: UUID,
) -> Certificate:
    certrepo = CertificateRepository(session, ctx.tenant_id)
    old = await certrepo.get(old_certificate_id)
    if old is None:
        raise ValidationError("Certificate not found")
    if old.status is CertificateStatus.REVOKED:
        raise StateConflictError("Cannot replace a revoked certificate")
    new = Certificate(
        tenant_id=ctx.tenant_id,
        unit_id=old.unit_id,
        completion_id=old.completion_id,
        numbering_scope=old.numbering_scope,
        certificate_no=await certrepo.next_certificate_no(old.numbering_scope),
        snapshot={**old.snapshot, "replaces": str(old_certificate_id), "replaced_at": clock().now().isoformat()},
        status=CertificateStatus.ISSUED,
        replacement_id=None,
        created_by=str(ctx.account_id),
    )
    await certrepo.create(new)
    await session.execute(
        text(
            "UPDATE certificate SET status='replaced', replacement_id=:n, updated_at=now() "
            "WHERE id=:o AND tenant_id=:tid"
        ),
        {"n": str(new.id), "o": str(old_certificate_id), "tid": str(ctx.tenant_id)},
    )
    await session.execute(
        text(
            "UPDATE certificate SET replacement_id=:o WHERE id=:n AND tenant_id=:tid"
        ),
        {"o": str(old_certificate_id), "n": str(new.id), "tid": str(ctx.tenant_id)},
    )
    event = CertificateReplaced(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"certificate:{old_certificate_id}")
    bus().dispatch(event)
    return new


async def revoke_certificate(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    certificate_id: UUID,
    reason: str,
) -> None:
    if not reason:
        raise ValidationError("reason is required for revocation")
    certrepo = CertificateRepository(session, ctx.tenant_id)
    await session.execute(
        text(
            """
            UPDATE certificate
            SET status='revoked',
                revocation = jsonb_build_object('actor', :a, 'reason', :r, 'at', :t),
                updated_at = now()
            WHERE id = :id AND tenant_id = :tid
            """
        ),
        {
            "a": str(ctx.account_id),
            "r": reason,
            "t": clock().now().isoformat(),
            "id": str(certificate_id),
            "tid": str(ctx.tenant_id),
        },
    )
    event = CertificateRevoked(tenant_id=ctx.tenant_id, actor_id=ctx.account_id)
    await enqueue(session, event, aggregate_ref=f"certificate:{certificate_id}")
    bus().dispatch(event)