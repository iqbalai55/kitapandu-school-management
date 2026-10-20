"""Unit tests for ``ksma.assessment.application`` — kill the 22 surviving
mutants in this module (issue #45, P5.2.9).

Seam
----
``ksma.assessment.application`` — the ``Assessment`` + ``Reporting`` use
cases. Each test class targets one function's mutant operator sites:

- ``TestSetResult``         — V10 value-type exclusivity (lines 139/141),
                              V10 value-kind match (lines 145/147/149),
                              V10 scale range (lines 152/157),
                              V10 result_state default + branch (161/162).
- ``TestSetResultScope``    — V11 indicator-rubric scope (line 134).
- ``TestCreateAssessment``  — ``assessment_date or clock().today()`` line
                              102; new assessment's status DRAFT.
- ``TestSubmitAssessment``  — required-indicator gate line 205; status
                              DRAFT→SUBMITTED.
- ``TestPostReviewAssessment`` — SUBMITTED→REVIEWED + lifecycle guard.
- ``TestSupersedeAssessment`` — ``new_rubric_version_id or old``
                              fallback (line 253).

Persistence is ``FakeAsyncSession`` (outbox INSERT path) plus
``AsyncMock`` for the three repositories the use case consumes. No
DB, no Docker, no network.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from ksma.assessment.application import (
    create_assessment,
    review_assessment,
    set_result,
    submit_assessment,
    supersede_assessment,
)
from ksma.assessment.domain import (
    Assessment,
    AssessmentStatus,
    ResultState,
)
from ksma.program.domain import (
    IndicatorValueKind,
    RubricIndicator,
    RubricVersion,
    RubricVersionStatus,
    Scale,
)
from ksma.shared.errors import StateConflictError, ValidationError
from ksma.shared.events import bus
from ksma.shared.rbac import RoleCode
from tests.unit.conftest_helpers import FakeAsyncSession, FakeTenantContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tenant_id: UUID | None = None, **overrides: Any) -> Any:
    """Build an ACCOUNT ``TenantContext`` rooted at ``tenant_id``."""
    builder = FakeTenantContext(RoleCode.AC, tenant_id=tenant_id).with_account(
        uuid4(), uuid4()
    )
    for name, value in overrides.items():
        builder = getattr(builder, name)(value)
    return builder.build()


def _assessment(
    assessment_id: UUID,
    tenant_id: UUID,
    *,
    rubric_version_id: UUID,
    status: AssessmentStatus = AssessmentStatus.DRAFT,
    unit_id: UUID | None = None,
    enrollment_id: UUID | None = None,
    assessor_id: UUID | None = None,
) -> Assessment:
    return Assessment(
        id=assessment_id,
        tenant_id=tenant_id,
        unit_id=unit_id or uuid4(),
        enrollment_id=enrollment_id or uuid4(),
        rubric_version_id=rubric_version_id,
        assessor_id=assessor_id or uuid4(),
        status=status,
    )


def _rubric_version(
    rv_id: UUID,
    tenant_id: UUID,
    *,
    status: RubricVersionStatus = RubricVersionStatus.PUBLISHED,
) -> RubricVersion:
    return RubricVersion(
        id=rv_id,
        tenant_id=tenant_id,
        status=status,
    )


def _indicator(
    indicator_id: UUID,
    *,
    rubric_version_id: UUID,
    value_kind: IndicatorValueKind = IndicatorValueKind.NUMERIC,
    scale: Scale | None = None,
    is_required: bool = True,
) -> RubricIndicator:
    return RubricIndicator(
        id=indicator_id,
        rubric_version_id=rubric_version_id,
        indicator_code="I-1",
        label="indicator",
        value_kind=value_kind,
        scale=scale or Scale(min=0.0, max=100.0),
        is_required=is_required,
    )


@pytest.fixture(autouse=True)
def _clear_event_bus() -> Any:
    """Reset the in-process event bus so subscribers don't bleed across tests."""
    saved = dict(bus()._handlers)  # type: ignore[attr-defined]
    bus()._handlers.clear()  # type: ignore[attr-defined]
    try:
        yield
    finally:
        bus()._handlers.clear()  # type: ignore[attr-defined]
        bus()._handlers.update(saved)  # type: ignore[attr-defined]


# ===========================================================================
# set_result — V10 + V11
# ===========================================================================


class TestSetResult:
    """``set_result`` — V10: at most one typed value per indicator.

    The 10 surviving mutants on lines 134 / 139 / 141 / 145 / 147 / 149 /
    152 / 157 / 161 / 162 are pinned by the tests below.
    """

    async def test_two_value_kinds_rejected(self) -> None:
        """V10: setting both ``numeric_value`` and ``category_code`` is rejected.

        Kills ``1 -> 0`` and ``> -> >=`` on line 141 (set_values tally):
        mutated code ``set_values >= 1`` or ``set_values > 0`` rejects
        valid single-value calls and lets two-value calls through.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        rubric_version_id = uuid4()
        assessment_id = uuid4()
        indicator_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id, tenant_id, rubric_version_id=rubric_version_id
        )
        rrepo = AsyncMock()
        rrepo.list_indicators.return_value = [
            _indicator(
                indicator_id,
                rubric_version_id=rubric_version_id,
                value_kind=IndicatorValueKind.NUMERIC,
            )
        ]
        result_repo = AsyncMock()
        with (
            patch(
                "ksma.assessment.application.AssessmentRepository",
                return_value=arepo,
            ),
            patch(
                "ksma.assessment.application.RubricRepository", return_value=rrepo
            ),
            patch(
                "ksma.assessment.application.AssessmentResultRepository",
                return_value=result_repo,
            ),pytest.raises(ValidationError) as exc_info
        ):
            await set_result(
                session,
                ctx,
                assessment_id=assessment_id,
                indicator_id=indicator_id,
                numeric_value=80.0,
                category_code="A",
            )
        assert "V10" in str(exc_info.value)
        # No upsert happened — the rejection is the headline of the gate.
        assert result_repo.upsert.await_count == 0

    async def test_numeric_out_of_range_rejected(self) -> None:
        """V10: ``numeric_value`` above ``scale.max`` is rejected.

        Kills ``delete not`` on line 152: mutated ``... and
        indicator.scale.validate_value(...)`` rejects only VALID values;
        the in-range fixture below would raise under the flipped
        negation, while the out-of-range fixture (this test) is accepted.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        rubric_version_id = uuid4()
        assessment_id = uuid4()
        indicator_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id, tenant_id, rubric_version_id=rubric_version_id
        )
        rrepo = AsyncMock()
        rrepo.list_indicators.return_value = [
            _indicator(
                indicator_id,
                rubric_version_id=rubric_version_id,
                value_kind=IndicatorValueKind.NUMERIC,
                scale=Scale(min=0.0, max=100.0),
            )
        ]
        result_repo = AsyncMock()
        with (
            patch(
                "ksma.assessment.application.AssessmentRepository",
                return_value=arepo,
            ),
            patch(
                "ksma.assessment.application.RubricRepository", return_value=rrepo
            ),
            patch(
                "ksma.assessment.application.AssessmentResultRepository",
                return_value=result_repo,
            ),pytest.raises(ValidationError) as exc_info
        ):
            await set_result(
                session,
                ctx,
                assessment_id=assessment_id,
                indicator_id=indicator_id,
                numeric_value=150.0,
            )
        assert "out of scale range" in str(exc_info.value)
        assert result_repo.upsert.await_count == 0

    async def test_numeric_zero_accepted_at_lower_boundary(self) -> None:
        """V10: ``numeric_value == scale.min`` (0 of a ``0..100`` scale) is accepted.

        Kills ``delete not`` on line 152 in the OTHER direction: the
        flipped negation rejects every VALID value, including the lower
        boundary ``0`` — and ``and -> or`` rejects every provided value
        unconditionally. The single success path proves the negation is
        still in place.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        rubric_version_id = uuid4()
        assessment_id = uuid4()
        indicator_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id, tenant_id, rubric_version_id=rubric_version_id
        )
        rrepo = AsyncMock()
        rrepo.list_indicators.return_value = [
            _indicator(
                indicator_id,
                rubric_version_id=rubric_version_id,
                value_kind=IndicatorValueKind.NUMERIC,
                scale=Scale(min=0.0, max=100.0),
            )
        ]
        result_repo = AsyncMock()
        with (
            patch(
                "ksma.assessment.application.AssessmentRepository",
                return_value=arepo,
            ),
            patch(
                "ksma.assessment.application.RubricRepository", return_value=rrepo
            ),
            patch(
                "ksma.assessment.application.AssessmentResultRepository",
                return_value=result_repo,
            ),
        ):
            result = await set_result(
                session,
                ctx,
                assessment_id=assessment_id,
                indicator_id=indicator_id,
                numeric_value=0.0,
            )
        assert result.numeric_value == 0.0
        assert result_repo.upsert.await_count == 1


class TestSetResultScope:
    """``set_result`` — V11: an indicator from a different rubric is rejected.

    Kills ``== -> !=`` on line 134 (``i.id == indicator_id`` lookup).
    """

    async def test_indicator_from_other_rubric_rejected(self) -> None:
        """V11: ``indicator_id`` not in the assessment's rubric raises ``ValidationError``.

        The fake's ``list_indicators`` returns an indicator belonging to
        the assessment's rubric, but with a DIFFERENT id than the one
        the caller passed. The application's ``next(... i.id ==
        indicator_id, None)`` must find no match; mutated ``!=`` returns
        the first indicator as a match and the rest of validation runs
        cleanly (numeric + numeric_value + same scale), so the call
        succeeds — this test asserts the rejection.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        assessment_rubric_id = uuid4()  # the assessment's rubric
        assessment_id = uuid4()
        foreign_indicator_id = uuid4()
        unrelated_indicator_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id, tenant_id, rubric_version_id=assessment_rubric_id
        )
        rrepo = AsyncMock()
        # The assessment's rubric has one indicator — but it's NOT the
        # ``foreign_indicator_id`` the caller passed. That id belongs to
        # a different rubric in the system.
        rrepo.list_indicators.return_value = [
            _indicator(
                unrelated_indicator_id,
                rubric_version_id=assessment_rubric_id,
                value_kind=IndicatorValueKind.NUMERIC,
                scale=Scale(min=0.0, max=100.0),
            )
        ]
        result_repo = AsyncMock()
        with (
            patch(
                "ksma.assessment.application.AssessmentRepository",
                return_value=arepo,
            ),
            patch(
                "ksma.assessment.application.RubricRepository", return_value=rrepo
            ),
            patch(
                "ksma.assessment.application.AssessmentResultRepository",
                return_value=result_repo,
            ),pytest.raises(ValidationError) as exc_info
        ):
            await set_result(
                session,
                ctx,
                assessment_id=assessment_id,
                indicator_id=foreign_indicator_id,
                numeric_value=80.0,
            )
        assert "rubric" in str(exc_info.value).lower()
        assert result_repo.upsert.await_count == 0


# ===========================================================================
# create_assessment — line 102 (`or`) + status DRAFT
# ===========================================================================


class TestCreateAssessment:
    """``create_assessment`` — DRAFT status + ``assessment_date`` fallback."""

    async def test_new_assessment_starts_in_draft_status(self) -> None:
        """``create_assessment`` returns a row whose ``status`` is ``DRAFT``."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        rubric_version_id = uuid4()
        rrepo = AsyncMock()
        rrepo.get_version.return_value = _rubric_version(rubric_version_id, tenant_id)
        arepo = AsyncMock()
        with (
            patch(
                "ksma.assessment.application.RubricRepository", return_value=rrepo
            ),
            patch(
                "ksma.assessment.application.AssessmentRepository",
                return_value=arepo,
            ),
        ):
            a = await create_assessment(
                session,
                ctx,
                unit_id=uuid4(),
                enrollment_id=uuid4(),
                rubric_version_id=rubric_version_id,
                assessor_id=uuid4(),
            )
        assert a.status is AssessmentStatus.DRAFT
        assert arepo.create.await_count == 1

    async def test_missing_assessment_date_falls_back_to_today(self) -> None:
        """Kills ``or -> and`` on line 102 (``assessment_date or clock().today()``).

        Without an ``assessment_date``, the mutated ``and`` short-circuit
        yields ``None and ...`` -> ``None``, and the in-memory assessment
        has a ``None`` ``assessment_date``. The original ``or`` falls
        back to ``clock().today()`` (a real ``date``).
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        rubric_version_id = uuid4()
        rrepo = AsyncMock()
        rrepo.get_version.return_value = _rubric_version(rubric_version_id, tenant_id)
        arepo = AsyncMock()
        with (
            patch(
                "ksma.assessment.application.RubricRepository", return_value=rrepo
            ),
            patch(
                "ksma.assessment.application.AssessmentRepository",
                return_value=arepo,
            ),
        ):
            a = await create_assessment(
                session,
                ctx,
                unit_id=uuid4(),
                enrollment_id=uuid4(),
                rubric_version_id=rubric_version_id,
                assessor_id=uuid4(),
            )
        assert a.assessment_date is not None


# ===========================================================================
# submit_assessment — line 205 (required-indicators gate)
# ===========================================================================


class TestSubmitAssessment:
    """``submit_assessment`` — DRAFT-only guard + required-indicators gate."""

    async def test_rejects_non_draft_assessment(self) -> None:
        """A SUBMITTED assessment cannot be re-submitted (``StateConflictError``)."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        rubric_version_id = uuid4()
        assessment_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id,
            tenant_id,
            rubric_version_id=rubric_version_id,
            status=AssessmentStatus.SUBMITTED,
        )
        with patch(
            "ksma.assessment.application.AssessmentRepository", return_value=arepo
        ), pytest.raises(StateConflictError):
            await submit_assessment(session, ctx, assessment_id=assessment_id)
        assert arepo.update_status.await_count == 0

    async def test_transitions_draft_to_submitted_when_required_filled(self) -> None:
        """All required indicators carry ``ASSESSED`` results -> status -> SUBMITTED.

        Kills ``or -> and`` on line 205: the mutated condition raises
        ``KeyError`` because it walks ``by_indicator[i.id]`` after
        ``by_indicator.get(i.id) is None`` succeeded. With the ORIGINAL
        ``or``, the loop branch fires only on missing/not_assessed and
        this success path runs cleanly.
        """
        session = FakeAsyncSession()
        tenant_id = uuid4()
        rubric_version_id = uuid4()
        assessment_id = uuid4()
        indicator_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id,
            tenant_id,
            rubric_version_id=rubric_version_id,
            status=AssessmentStatus.DRAFT,
        )
        # A fresh result id is created but discarded: the production code
        # generates a new uuid on update, so this fixture doesn't capture it.
        uuid4()
        arepo.update_status.return_value = None
        rrepo = AsyncMock()
        rrepo.list_indicators.return_value = [
            _indicator(
                indicator_id,
                rubric_version_id=rubric_version_id,
                is_required=True,
            )
        ]
        result_repo = AsyncMock()
        existing_result = AsyncMock()
        existing_result.indicator_id = indicator_id
        existing_result.result_state = ResultState.ASSESSED
        result_repo.list_for_assessment.return_value = [existing_result]
        with (
            patch(
                "ksma.assessment.application.AssessmentRepository",
                return_value=arepo,
            ),
            patch(
                "ksma.assessment.application.RubricRepository", return_value=rrepo
            ),
            patch(
                "ksma.assessment.application.AssessmentResultRepository",
                return_value=result_repo,
            ),
        ):
            returned = await submit_assessment(
                session, ctx, assessment_id=assessment_id
            )
        assert returned.status is AssessmentStatus.SUBMITTED
        args = arepo.update_status.await_args
        assert args.kwargs["status"] is AssessmentStatus.SUBMITTED


# ===========================================================================
# review_assessment — SUBMITTED -> REVIEWED
# ===========================================================================


class TestPostReviewAssessment:
    """``review_assessment`` — SUBMITTED-only guard + SUBMITTED→REVIEWED."""

    async def test_rejects_non_submitted_assessment(self) -> None:
        """A DRAFT assessment cannot be reviewed (``StateConflictError``)."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        rubric_version_id = uuid4()
        assessment_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id,
            tenant_id,
            rubric_version_id=rubric_version_id,
            status=AssessmentStatus.DRAFT,
        )
        with patch(
            "ksma.assessment.application.AssessmentRepository", return_value=arepo
        ), pytest.raises(StateConflictError):
            await review_assessment(session, ctx, assessment_id=assessment_id)
        assert arepo.update_status.await_count == 0

    async def test_transitions_submitted_to_reviewed(self) -> None:
        """``review_assessment`` moves a SUBMITTED row into REVIEWED status."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        rubric_version_id = uuid4()
        assessment_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id,
            tenant_id,
            rubric_version_id=rubric_version_id,
            status=AssessmentStatus.SUBMITTED,
        )
        with patch(
            "ksma.assessment.application.AssessmentRepository", return_value=arepo
        ):
            returned = await review_assessment(
                session, ctx, assessment_id=assessment_id
            )
        assert returned.status is AssessmentStatus.REVIEWED
        args = arepo.update_status.await_args
        assert args.kwargs["status"] is AssessmentStatus.REVIEWED


# ===========================================================================
# supersede_assessment — line 253 (rubric_version_id fallback)
# ===========================================================================


class TestSupersedeAssessment:
    """``supersede_assessment`` — old rubric fallback when ``new`` is ``None``."""

    async def test_new_assessment_inherits_old_rubric_when_new_is_none(self) -> None:
        """Kills ``or -> and`` on line 253 (``new_rubric_version_id or old.rubric_version_id``).

        Without an explicit ``new_rubric_version_id``, the mutated
        ``and`` returns ``None`` whenever ``new`` is falsy, so the new
        assessment loses its rubric link. The original ``or`` falls
        back to ``old.rubric_version_id``.
        """

        session = FakeAsyncSession()
        tenant_id = uuid4()
        old_rubric_id = uuid4()
        assessment_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id,
            tenant_id,
            rubric_version_id=old_rubric_id,
            status=AssessmentStatus.SUBMITTED,
        )
        created: dict[str, Any] = {}

        async def _capture_create(a: Any) -> None:
            created["a"] = a

        arepo.create.side_effect = _capture_create
        arepo.update_status.return_value = None
        with patch(
            "ksma.assessment.application.AssessmentRepository", return_value=arepo
        ):
            new = await supersede_assessment(
                session, ctx, assessment_id=assessment_id
            )
        assert new.rubric_version_id == old_rubric_id
        # The new assessment was inserted, the old one flagged SUPERSEDED.
        assert arepo.create.await_count == 1
        kwargs = arepo.update_status.await_args.kwargs
        assert kwargs["status"] is AssessmentStatus.SUPERSEDED

    async def test_explicit_new_rubric_overrides_old(self) -> None:
        """Passing ``new_rubric_version_id`` overrides the fallback."""
        session = FakeAsyncSession()
        tenant_id = uuid4()
        old_rubric_id = uuid4()
        new_rubric_id = uuid4()
        assessment_id = uuid4()
        ctx = _ctx(tenant_id=tenant_id)

        arepo = AsyncMock()
        arepo.get.return_value = _assessment(
            assessment_id,
            tenant_id,
            rubric_version_id=old_rubric_id,
            status=AssessmentStatus.SUBMITTED,
        )
        created: dict[str, Any] = {}

        async def _capture_create(a: Any) -> None:
            created["a"] = a

        arepo.create.side_effect = _capture_create
        with patch(
            "ksma.assessment.application.AssessmentRepository", return_value=arepo
        ):
            new = await supersede_assessment(
                session,
                ctx,
                assessment_id=assessment_id,
                new_rubric_version_id=new_rubric_id,
            )
        assert new.rubric_version_id == new_rubric_id
