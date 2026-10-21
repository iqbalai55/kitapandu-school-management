"""Unit tests for ``ksma.program.application`` — the version-lifecycle seam.

Seam
----
``ksma.program.application`` (``src/ksma/program/application/__init__.py``).
Public surface pinned here, straight from **docs/specs/003-program-and-class.md**:

- ``publish_program_version`` — the ``draft → published`` transition. Only a
  ``draft`` version may be published (:143); the ``make_current`` flag
  (:137/:148) decides whether the parent program's ``current_version_id``
  follows the publication.
- ``retire_program_version`` — the ``published → retired`` transition. Only a
  ``published`` version may be retired (:163).
- ``create_period`` — ``end_date < start_date`` is rejected (:187). The
  on-boundary case ``end_date == start_date`` is a *single-day* period and is
  legal; only a strict inversion is a validation error.
- ``publish_rubric_version`` — a rubric version may only be published when it
  carries at least one indicator (:274). Publishing an empty rubric would
  freeze a scoring sheet that can never score anyone.
- ``add_rubric_indicator`` — indicators may only be added to a ``draft`` rubric
  (:298), and a negative weight is rejected (:300). ``weight=None`` means
  "unweighted indicator", which is *not* a validation error.

Persistence is dict-backed fake repositories monkeypatched onto the
``ksma.program.application`` namespace; the ``AsyncSession`` is the in-memory
``FakeAsyncSession`` so ``enqueue`` (outbox) and ``bus().dispatch`` still run.
No DB, no Docker, no network.

What is NOT asserted here (lives in the integration tier):

- RLS policies, the real Postgres ``UPDATE ... WHERE tenant_id`` scoping.
- The JSONB curriculum/approval adapters in the SQL repositories.
- The outbox table itself (the fake records the INSERT).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from ksma.program import application as app
from ksma.program.domain import (
    AcademicPeriod,
    Curriculum,
    IndicatorValueKind,
    PeriodKind,
    Program,
    ProgramStatus,
    ProgramVersion,
    ProgramVersionStatus,
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
# Dict-backed fake repositories (patched onto the application namespace)
# ---------------------------------------------------------------------------


class _FakeProgramVersionRepo:
    """``ProgramVersionRepository`` as a dict keyed by version id."""

    def __init__(self) -> None:
        self.rows: dict[UUID, ProgramVersion] = {}
        self.created: list[ProgramVersion] = []
        self.status_calls: list[tuple[UUID, ProgramVersionStatus, str]] = []

    async def create(self, v: ProgramVersion) -> None:
        self.rows[v.id] = v
        self.created.append(v)

    async def get(self, id: UUID) -> ProgramVersion | None:
        return self.rows.get(id)

    async def set_status(
        self, id: UUID, *, status: ProgramVersionStatus, actor: str
    ) -> None:
        self.status_calls.append((id, status, actor))
        row = self.rows.get(id)
        if row is not None:
            row.status = status

    async def next_version_no(self, program_id: UUID) -> int:
        numbers = [v.version_no for v in self.rows.values() if v.program_id == program_id]
        return (max(numbers) + 1) if numbers else 1

    async def list_for_program(self, program_id: UUID) -> list[ProgramVersion]:
        return [v for v in self.rows.values() if v.program_id == program_id]


class _FakeProgramRepo:
    """``ProgramRepository`` as a dict keyed by program id."""

    def __init__(self) -> None:
        self.rows: dict[UUID, Program] = {}
        self.current_calls: list[tuple[UUID, UUID, str]] = []
        self.status_calls: list[tuple[UUID, ProgramStatus, str]] = []

    async def create(self, p: Program) -> None:
        self.rows[p.id] = p

    async def get(self, id: UUID) -> Program | None:
        return self.rows.get(id)

    async def list(
        self,
        *,
        unit_id: UUID | None = None,
        status: ProgramStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Program]:
        rows = list(self.rows.values())
        if unit_id is not None:
            rows = [r for r in rows if r.unit_id == unit_id]
        if status is not None:
            rows = [r for r in rows if r.status is status]
        return rows[offset : offset + limit]

    async def set_status(self, id: UUID, *, status: ProgramStatus, actor: str) -> None:
        self.status_calls.append((id, status, actor))
        row = self.rows.get(id)
        if row is not None:
            row.status = status

    async def set_current_version(self, id: UUID, version_id: UUID, actor: str) -> None:
        self.current_calls.append((id, version_id, actor))
        row = self.rows.get(id)
        if row is not None:
            row.current_version_id = version_id


class _FakeRubricRepo:
    """``RubricRepository`` as dicts keyed by rubric-version id."""

    def __init__(self) -> None:
        self.versions: dict[UUID, RubricVersion] = {}
        self.indicators: dict[UUID, list[RubricIndicator]] = {}
        self.published: list[tuple[UUID, str]] = []

    async def create_version(self, v: RubricVersion) -> None:
        self.versions[v.id] = v

    async def get_version(self, id: UUID) -> RubricVersion | None:
        return self.versions.get(id)

    async def next_version_no(self, program_version_id: UUID) -> int:
        return 1

    async def publish_version(self, id: UUID, actor: str) -> None:
        self.published.append((id, actor))
        row = self.versions.get(id)
        if row is not None:
            row.status = RubricVersionStatus.PUBLISHED
            row.published_at = datetime.now(UTC)

    async def add_indicator(self, ind: RubricIndicator) -> None:
        self.indicators.setdefault(ind.rubric_version_id, []).append(ind)

    async def list_indicators(self, rubric_version_id: UUID) -> list[RubricIndicator]:
        return list(self.indicators.get(rubric_version_id, []))


class _FakeAcademicPeriodRepo:
    def __init__(self) -> None:
        self.created: list[AcademicPeriod] = []

    async def create(self, p: AcademicPeriod) -> None:
        self.created.append(p)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _Repos:
    """One tenant-scoped set of fakes, patched over the application module."""

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.versions = _FakeProgramVersionRepo()
        self.programs = _FakeProgramRepo()
        self.rubrics = _FakeRubricRepo()
        self.periods = _FakeAcademicPeriodRepo()

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, "ProgramVersionRepository", lambda *a, **k: self.versions)
        monkeypatch.setattr(app, "ProgramRepository", lambda *a, **k: self.programs)
        monkeypatch.setattr(app, "RubricRepository", lambda *a, **k: self.rubrics)
        monkeypatch.setattr(app, "AcademicPeriodRepository", lambda *a, **k: self.periods)


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch) -> _Repos:
    """Replace every repository class the application binds at import time."""
    store = _Repos(uuid4())
    store.patch(monkeypatch)
    return store


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


def _ctx(tenant_id: UUID) -> Any:
    """An ACCOUNT ``TenantContext`` rooted at ``tenant_id``."""
    return (
        FakeTenantContext(RoleCode.AC, tenant_id=tenant_id)
        .with_account(uuid4(), uuid4())
        .build()
    )


def _draft_version(
    tenant_id: UUID, *, status: ProgramVersionStatus = ProgramVersionStatus.DRAFT
) -> tuple[ProgramVersion, Program]:
    """A program plus one version row sitting in ``status``."""
    program = Program(
        tenant_id=tenant_id,
        unit_id=uuid4(),
        program_code="P-001",
        name="Kriya Bengali A1",
        service_group="language",
        status=ProgramStatus.ACTIVE,
    )
    version = ProgramVersion(
        tenant_id=tenant_id,
        unit_id=program.unit_id,
        program_id=program.id,
        version_no=1,
        learning_objectives="Greet and introduce oneself",
        status=status,
    )
    return version, program


def _draft_rubric(tenant_id: UUID) -> RubricVersion:
    return RubricVersion(
        tenant_id=tenant_id,
        unit_id=uuid4(),
        program_version_id=uuid4(),
        version_no=1,
        name="Speaking rubric",
        status=RubricVersionStatus.DRAFT,
    )


def _indicator(rubric_version_id: UUID, tenant_id: UUID, code: str = "I1") -> RubricIndicator:
    return RubricIndicator(
        tenant_id=tenant_id,
        rubric_version_id=rubric_version_id,
        indicator_code=code,
        label=f"Criterion {code}",
        value_kind=IndicatorValueKind.NUMERIC,
        scale=Scale(min=0.0, max=4.0),
        is_required=True,
    )


def _outbox_refs(session: FakeAsyncSession, prefix: str) -> list[Any]:
    """``aggregate_ref`` values written to the outbox that start with ``prefix``."""
    refs: list[Any] = []
    for call in session.calls:
        if call.op != "execute" or "INSERT INTO outbox_messages" not in (call.sql or ""):
            continue
        ref = (call.params or {}).get("aggregate_ref")
        if isinstance(ref, str) and ref.startswith(prefix):
            refs.append(ref)
    return refs


# ===========================================================================
# publish_program_version — the draft → published transition
# ===========================================================================


class TestPublishProgramVersion:
    """``publish_program_version`` — lifecycle + the ``make_current`` default."""

    async def test_make_current_defaults_to_true(self, repos: _Repos) -> None:
        """Kills :137 ``make_current: bool = True -> False``.

        Publishing a version WITHOUT spelling out the flag must make it the
        program's current version — that is the whole point of publishing.
        Defaulting to ``False`` would publish versions nobody ever sees.
        """
        tenant_id = uuid4()
        version, program = _draft_version(tenant_id)
        repos.programs.rows[program.id] = program
        repos.versions.rows[version.id] = version
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        await app.publish_program_version(session, ctx, version_id=version.id)

        assert repos.programs.current_calls == [
            (program.id, version.id, str(ctx.account_id))
        ]
        assert program.current_version_id == version.id
        assert version.status is ProgramVersionStatus.PUBLISHED

    async def test_make_current_false_leaves_the_pointer_alone(self, repos: _Repos) -> None:
        """Boundary pair: the explicit opt-out keeps the previous pointer."""
        tenant_id = uuid4()
        version, program = _draft_version(tenant_id)
        previous = uuid4()
        program.current_version_id = previous
        repos.programs.rows[program.id] = program
        repos.versions.rows[version.id] = version
        session = FakeAsyncSession()

        await app.publish_program_version(
            session, _ctx(tenant_id), version_id=version.id, make_current=False
        )

        assert repos.programs.current_calls == []
        assert program.current_version_id == previous
        # …but the version itself IS published.
        assert version.status is ProgramVersionStatus.PUBLISHED

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param(ProgramVersionStatus.PUBLISHED, id="already_published"),
            pytest.param(ProgramVersionStatus.RETIRED, id="retired"),
        ],
    )
    async def test_only_a_draft_version_can_be_published(
        self, repos: _Repos, status: ProgramVersionStatus
    ) -> None:
        """``draft → published`` only; re-publishing is a state conflict."""
        tenant_id = uuid4()
        version, _program = _draft_version(tenant_id, status=status)
        repos.versions.rows[version.id] = version
        session = FakeAsyncSession()

        with pytest.raises(StateConflictError) as exc_info:
            await app.publish_program_version(session, _ctx(tenant_id), version_id=version.id)

        assert "only" in str(exc_info.value)
        assert repos.versions.status_calls == []
        assert version.status is status

    async def test_unknown_version_is_rejected(self, repos: _Repos) -> None:
        session = FakeAsyncSession()
        with pytest.raises(ValidationError):
            await app.publish_program_version(session, _ctx(uuid4()), version_id=uuid4())


# ===========================================================================
# retire_program_version — the published → retired transition
# ===========================================================================


class TestRetireProgramVersion:
    """``retire_program_version`` — precondition: only after ``published``."""

    async def test_published_version_is_retired(self, repos: _Repos) -> None:
        tenant_id = uuid4()
        version, _program = _draft_version(tenant_id, status=ProgramVersionStatus.PUBLISHED)
        repos.versions.rows[version.id] = version
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        await app.retire_program_version(session, ctx, version_id=version.id)

        assert version.status is ProgramVersionStatus.RETIRED
        assert repos.versions.status_calls == [
            (version.id, ProgramVersionStatus.RETIRED, str(ctx.account_id))
        ]

    async def test_draft_version_cannot_be_retired(self, repos: _Repos) -> None:
        """A never-published version has nothing to retire."""
        tenant_id = uuid4()
        version, _program = _draft_version(tenant_id, status=ProgramVersionStatus.DRAFT)
        repos.versions.rows[version.id] = version
        session = FakeAsyncSession()

        with pytest.raises(StateConflictError):
            await app.retire_program_version(session, _ctx(tenant_id), version_id=version.id)

        assert repos.versions.status_calls == []
        assert version.status is ProgramVersionStatus.DRAFT

    async def test_unknown_version_is_rejected(self, repos: _Repos) -> None:
        session = FakeAsyncSession()
        with pytest.raises(ValidationError):
            await app.retire_program_version(session, _ctx(uuid4()), version_id=uuid4())

    async def test_full_lifecycle_draft_published_retired(self, repos: _Repos) -> None:
        """The whole state machine: ``draft → published → retired``, in order."""
        tenant_id = uuid4()
        version, _program = _draft_version(tenant_id)
        repos.versions.rows[version.id] = version
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        assert version.status is ProgramVersionStatus.DRAFT
        await app.publish_program_version(session, ctx, version_id=version.id)
        assert version.status is ProgramVersionStatus.PUBLISHED
        await app.retire_program_version(session, ctx, version_id=version.id)
        assert version.status is ProgramVersionStatus.RETIRED

        assert [status for _id, status, _actor in repos.versions.status_calls] == [
            ProgramVersionStatus.PUBLISHED,
            ProgramVersionStatus.RETIRED,
        ]


# ===========================================================================
# create_period — the date-ordering boundary
# ===========================================================================


class TestCreatePeriod:
    """``create_period`` — ``end_date`` on or after ``start_date``."""

    @pytest.mark.parametrize(
        ("start", "end", "accepted"),
        [
            pytest.param(date(2026, 1, 5), date(2026, 1, 5), True, id="same_day_single_period"),
            pytest.param(date(2026, 1, 5), date(2026, 1, 6), True, id="one_day_long"),
            pytest.param(date(2026, 1, 5), date(2026, 3, 31), True, id="term_long"),
            pytest.param(date(2026, 1, 5), date(2026, 1, 4), False, id="one_day_inversion"),
            pytest.param(date(2026, 1, 5), date(2025, 12, 31), False, id="long_inversion"),
        ],
    )
    async def test_end_date_must_not_precede_start_date(
        self, repos: _Repos, start: date, end: date, accepted: bool
    ) -> None:
        """Boundary pair for :187 ``end_date < start_date -> <=``.

        ``end_date == start_date`` is a legal ONE-DAY period; only a strict
        inversion (``end_date`` earlier than ``start_date``) is invalid. An
        off-by-one that rejected the single-day period would silently refuse to
        schedule same-day exams, intensives and make-up sessions.
        """
        tenant_id = uuid4()
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        if accepted:
            period = await app.create_period(
                session,
                ctx,
                unit_id=uuid4(),
                period_code="2026-T1",
                name="Term 1",
                period_kind=PeriodKind.SEMESTER,
                start_date=start,
                end_date=end,
            )
            assert period.start_date == start
            assert period.end_date == end
            assert period.status.value == "planned"
            assert repos.periods.created == [period]
        else:
            with pytest.raises(ValidationError) as exc_info:
                await app.create_period(
                    session,
                    ctx,
                    unit_id=uuid4(),
                    period_code="2026-T1",
                    name="Term 1",
                    period_kind=PeriodKind.SEMESTER,
                    start_date=start,
                    end_date=end,
                )
            assert "end_date" in str(exc_info.value)
            assert repos.periods.created == []


# ===========================================================================
# publish_rubric_version — a rubric must carry indicators
# ===========================================================================


class TestPublishRubricVersion:
    """``publish_rubric_version`` — lifecycle + the "no empty rubric" rule."""

    async def test_draft_with_indicators_is_published(self, repos: _Repos) -> None:
        tenant_id = uuid4()
        rv = _draft_rubric(tenant_id)
        repos.rubrics.versions[rv.id] = rv
        repos.rubrics.indicators[rv.id] = [_indicator(rv.id, tenant_id)]
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        await app.publish_rubric_version(session, ctx, rubric_version_id=rv.id)

        assert repos.rubrics.published == [(rv.id, str(ctx.account_id))]
        assert rv.status is RubricVersionStatus.PUBLISHED

    async def test_rubric_without_indicators_cannot_be_published(self, repos: _Repos) -> None:
        """Kills :274 ``delete not`` (``if indicators:``).

        Publishing an empty rubric freezes a scoring sheet with zero criteria:
        every observation would be scored against nothing. An empty indicator
        list must be a StateConflictError, never a silent publish.
        """
        tenant_id = uuid4()
        rv = _draft_rubric(tenant_id)
        repos.rubrics.versions[rv.id] = rv
        repos.rubrics.indicators[rv.id] = []
        session = FakeAsyncSession()

        with pytest.raises(StateConflictError) as exc_info:
            await app.publish_rubric_version(session, _ctx(tenant_id), rubric_version_id=rv.id)

        assert "no indicators" in str(exc_info.value)
        assert repos.rubrics.published == []
        assert rv.status is RubricVersionStatus.DRAFT

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param(RubricVersionStatus.PUBLISHED, id="published"),
            pytest.param(RubricVersionStatus.RETIRED, id="retired"),
        ],
    )
    async def test_only_a_draft_rubric_can_be_published(
        self, repos: _Repos, status: RubricVersionStatus
    ) -> None:
        tenant_id = uuid4()
        rv = _draft_rubric(tenant_id)
        rv.status = status
        repos.rubrics.versions[rv.id] = rv
        repos.rubrics.indicators[rv.id] = [_indicator(rv.id, tenant_id)]
        session = FakeAsyncSession()

        with pytest.raises(StateConflictError):
            await app.publish_rubric_version(session, _ctx(tenant_id), rubric_version_id=rv.id)

        assert repos.rubrics.published == []

    async def test_unknown_rubric_version_is_rejected(self, repos: _Repos) -> None:
        session = FakeAsyncSession()
        with pytest.raises(ValidationError):
            await app.publish_rubric_version(session, _ctx(uuid4()), rubric_version_id=uuid4())


# ===========================================================================
# add_rubric_indicator — indicator additivity + weight rules
# ===========================================================================


class TestAddRubricIndicator:
    """``add_rubric_indicator`` — additivity only while ``draft``."""

    @pytest.mark.parametrize(
        ("weight", "accepted"),
        [
            pytest.param(None, True, id="unweighted_indicator_allowed"),
            pytest.param(0.0, True, id="zero_weight_at_lower_bound"),
            pytest.param(0.5, True, id="fractional_weight"),
            pytest.param(1.0, True, id="unit_weight"),
            pytest.param(-0.001, False, id="just_below_zero_rejected"),
            pytest.param(-1.0, False, id="negative_weight_rejected"),
        ],
    )
    async def test_weight_must_be_non_negative(
        self, repos: _Repos, weight: float | None, accepted: bool
    ) -> None:
        """Kills :300 ``and -> or``, ``< -> <=`` and ``0 -> 1``.

        Three boundary pairs on one line:

        * ``weight=None`` means "this indicator is unweighted" — the guard must
          short-circuit on ``weight is not None``. Flipping ``and`` to ``or``
          evaluates ``None < 0`` and explodes on an ordinary unweighted
          indicator.
        * ``weight=0.0`` is the on-boundary value and must be accepted; a
          ``<=`` would reject a legitimate zero-weight "not applicable"
          criterion.
        * ``weight=0.5`` is an ordinary fractional weight; a ``0 -> 1`` floor
          would reject every weight below one.
        """
        tenant_id = uuid4()
        rv = _draft_rubric(tenant_id)
        repos.rubrics.versions[rv.id] = rv
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        if accepted:
            ind = await app.add_rubric_indicator(
                session,
                ctx,
                rubric_version_id=rv.id,
                indicator_code="I1",
                label="Fluency",
                value_kind=IndicatorValueKind.NUMERIC,
                scale=Scale(min=0.0, max=4.0),
                is_required=True,
                weight=weight,
            )
            assert ind.weight == weight
            assert ind.rubric_version_id == rv.id
            assert repos.rubrics.indicators[rv.id] == [ind]
        else:
            with pytest.raises(ValidationError) as exc_info:
                await app.add_rubric_indicator(
                    session,
                    ctx,
                    rubric_version_id=rv.id,
                    indicator_code="I1",
                    label="Fluency",
                    value_kind=IndicatorValueKind.NUMERIC,
                    scale=Scale(min=0.0, max=4.0),
                    is_required=True,
                    weight=weight,
                )
            assert "weight" in str(exc_info.value)
            assert repos.rubrics.indicators.get(rv.id) in (None, [])

    async def test_indicators_accumulate_on_a_draft_rubric(self, repos: _Repos) -> None:
        """Additivity: each add appends; the rubric keeps every indicator."""
        tenant_id = uuid4()
        rv = _draft_rubric(tenant_id)
        repos.rubrics.versions[rv.id] = rv
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        for index in (1, 2, 3):
            await app.add_rubric_indicator(
                session,
                ctx,
                rubric_version_id=rv.id,
                indicator_code=f"I{index}",
                label=f"Criterion {index}",
                value_kind=IndicatorValueKind.NUMERIC,
                scale=Scale(min=0.0, max=4.0),
                is_required=index == 1,
                weight=float(index),
            )

        assert [i.indicator_code for i in repos.rubrics.indicators[rv.id]] == ["I1", "I2", "I3"]
        assert [i.weight for i in repos.rubrics.indicators[rv.id]] == [1.0, 2.0, 3.0]
        assert [i.is_required for i in repos.rubrics.indicators[rv.id]] == [True, False, False]

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param(RubricVersionStatus.PUBLISHED, id="published"),
            pytest.param(RubricVersionStatus.RETIRED, id="retired"),
        ],
    )
    async def test_published_rubric_is_frozen(
        self, repos: _Repos, status: RubricVersionStatus
    ) -> None:
        """Once published, the indicator set may no longer change."""
        tenant_id = uuid4()
        rv = _draft_rubric(tenant_id)
        rv.status = status
        repos.rubrics.versions[rv.id] = rv
        session = FakeAsyncSession()

        with pytest.raises(StateConflictError):
            await app.add_rubric_indicator(
                session,
                _ctx(tenant_id),
                rubric_version_id=rv.id,
                indicator_code="I9",
                label="Late criterion",
                value_kind=IndicatorValueKind.NUMERIC,
                scale=Scale(min=0.0, max=4.0),
                is_required=True,
                weight=1.0,
            )

        assert repos.rubrics.indicators.get(rv.id) in (None, [])

    async def test_unknown_rubric_version_is_rejected(self, repos: _Repos) -> None:
        session = FakeAsyncSession()
        with pytest.raises(ValidationError):
            await app.add_rubric_indicator(
                session,
                _ctx(uuid4()),
                rubric_version_id=uuid4(),
                indicator_code="I1",
                label="Fluency",
                value_kind=IndicatorValueKind.NUMERIC,
                scale=Scale(min=0.0, max=4.0),
                is_required=True,
            )


# ===========================================================================
# Outbox: every accepted lifecycle transition writes an audit envelope
# ===========================================================================


class TestOutboxEnvelopes:
    """AuditWriteBoundary — state changes write, refusals do not."""

    async def test_publish_and_retire_each_enqueue_one_row(self, repos: _Repos) -> None:
        tenant_id = uuid4()
        version, _program = _draft_version(tenant_id)
        repos.versions.rows[version.id] = version
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        await app.publish_program_version(session, ctx, version_id=version.id)
        await app.retire_program_version(session, ctx, version_id=version.id)

        assert _outbox_refs(session, "program_version:") == [
            f"program_version:{version.id}",
            f"program_version:{version.id}",
        ]

    async def test_rejected_transition_enqueues_nothing(self, repos: _Repos) -> None:
        """A refused state change must not leave an audit trail behind."""
        tenant_id = uuid4()
        version, _program = _draft_version(tenant_id, status=ProgramVersionStatus.DRAFT)
        repos.versions.rows[version.id] = version
        session = FakeAsyncSession()

        with pytest.raises(StateConflictError):
            await app.retire_program_version(session, _ctx(tenant_id), version_id=version.id)

        assert _outbox_refs(session, "program_version:") == []

    async def test_adding_an_indicator_enqueues_one_row(self, repos: _Repos) -> None:
        tenant_id = uuid4()
        rv = _draft_rubric(tenant_id)
        repos.rubrics.versions[rv.id] = rv
        session = FakeAsyncSession()

        ind = await app.add_rubric_indicator(
            session,
            _ctx(tenant_id),
            rubric_version_id=rv.id,
            indicator_code="I1",
            label="Fluency",
            value_kind=IndicatorValueKind.NUMERIC,
            scale=Scale(min=0.0, max=4.0),
            is_required=True,
            weight=1.0,
        )

        assert _outbox_refs(session, "rubric_indicator:") == [f"rubric_indicator:{ind.id}"]


# ===========================================================================
# create_program_version — next_version_no drives the version numbering
# ===========================================================================


class TestCreateProgramVersion:
    """``create_program_version`` — a new version always starts ``draft``."""

    async def test_new_version_is_draft_and_numbered_after_the_last(
        self, repos: _Repos
    ) -> None:
        tenant_id = uuid4()
        program = Program(
            tenant_id=tenant_id,
            unit_id=uuid4(),
            program_code="P-001",
            name="Program",
            service_group="language",
        )
        repos.programs.rows[program.id] = program
        existing = ProgramVersion(
            tenant_id=tenant_id,
            unit_id=program.unit_id,
            program_id=program.id,
            version_no=4,
            status=ProgramVersionStatus.PUBLISHED,
        )
        repos.versions.rows[existing.id] = existing
        session = FakeAsyncSession()
        ctx = _ctx(tenant_id)

        created = await app.create_program_version(
            session,
            ctx,
            program_id=program.id,
            learning_objectives="Objectives",
            curriculum=Curriculum(components=[]),
        )

        assert created.version_no == 5
        assert created.status is ProgramVersionStatus.DRAFT
        assert repos.versions.created == [created]

    async def test_unknown_program_is_rejected(self, repos: _Repos) -> None:
        session = FakeAsyncSession()
        with pytest.raises(ValidationError):
            await app.create_program_version(
                session,
                _ctx(uuid4()),
                program_id=uuid4(),
                learning_objectives="x",
                curriculum=Curriculum(components=[]),
            )