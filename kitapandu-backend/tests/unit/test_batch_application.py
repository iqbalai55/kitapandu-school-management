"""Unit tests for ``ksma.batch.application`` — the capacity-race seam.

Seam
----
``ksma.batch.application`` (module-level ``__init__.py``). Public surface pinned
here, one test per domain invariant from ``docs/specs/003-program-and-class.md``
§Testing Decisions and ``docs/specs/000-umbrella-platform.md``:

- ``create_batch`` (:90) — capacity positivity boundary (V05 pre-condition)
- ``change_batch_capacity`` (:154/:158/:165/:166/:172) — Q46 lowering below
  active occupancy is rejected unless ``force=True`` *and* a reason is given
  (V05 capacity-limit enforcement + V07 serialisation via ``lock_for_update``).
- ``create_application`` (:211) — completeness default-vs-preserved.
- ``decide_application`` (:265/:270) — REG-A04 approve/reject boundary.
- ``place_enrollment`` (:335/:359) — V05 capacity race (count == capacity is
  full) + V06 overlap-by-exclusion-constraint translation.
- ``transfer_enrollment`` (:516) — same V05 semantics on the target batch.
- ``assign_teacher`` (:589/:599) — exactly-one-of ``batch_id`` / ``session_id``
  + component-code default.

The persistence layer is a dict-backed fake repository toolkit defined in this
file (mirroring the T18 ``_FakeSession`` shape) wired in via ``monkeypatch`` on
the ``ksma.batch.application`` namespace, so the assertions are on the
*post-condition* of each application service (recorded repository writes,
typed ``Enrollment.status`` / ``BatchStatus`` values, recorded outbox event
types) rather than on internal SQL. No DB, no FastAPI app, no network.

What is NOT asserted here (lives in the integration tier):

- RLS policies, the real Postgres exclusion constraint, Alembic round-trips.
- The HTTP envelope / route dispatch (see ``test_batch_interface.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from ksma.batch import application as app
from ksma.batch.domain import (
    Application,
    ApplicationStatus,
    AssignmentKind,
    Batch,
    BatchStatus,
    Completeness,
    DeliveryMode,
    Enrollment,
    EnrollmentChange,
    EnrollmentStatus,
    TeachingAssignment,
)
from ksma.program.domain import ProgramVersionStatus
from ksma.shared.errors import (
    CapacityExceededError,
    StateConflictError,
    ValidationError,
)

class _PublishedVersion:
    """Minimal stand-in for a published ``ProgramVersion`` row."""

    status = ProgramVersionStatus.PUBLISHED


UTC = timezone.utc
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake repositories (dict-backed; record every write)
# ---------------------------------------------------------------------------


class _FakeBatchRepository:
    """Stands in for ``BatchRepository``; records the V07 lock + V05 count."""

    batches: dict[UUID, Batch] = {}
    active_counts: dict[UUID, int] = {}
    locks: list[UUID] = []
    status_writes: list[tuple[UUID, BatchStatus, str]] = []
    capacity_writes: list[tuple[UUID, int, str]] = []
    list_calls: list[dict[str, Any]] = []

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, b: Batch) -> None:
        self.batches[b.id] = b

    async def get(self, id: UUID) -> Batch | None:
        return self.batches.get(id)

    async def lock_for_update(self, id: UUID) -> Batch | None:
        self.locks.append(id)
        return self.batches.get(id)

    async def set_status(self, id: UUID, *, status: BatchStatus, actor: str) -> None:
        self.status_writes.append((id, status, actor))
        if id in self.batches:
            self.batches[id].status = status

    async def set_capacity(self, id: UUID, *, capacity: int, actor: str) -> None:
        self.capacity_writes.append((id, capacity, actor))
        if id in self.batches:
            self.batches[id].capacity = capacity

    async def count_active_enrollments(self, batch_id: UUID, *, at: Any | None = None) -> int:
        return self.active_counts.get(batch_id, 0)

    async def list(self, **kwargs: Any) -> list[Batch]:
        self.list_calls.append(kwargs)
        return list(self.batches.values())


class _FakeApplicationRepository:
    applications: dict[UUID, Application] = {}
    created: list[Application] = []
    status_writes: list[tuple[UUID, ApplicationStatus, dict[str, Any] | None, str]] = []
    list_calls: list[dict[str, Any]] = []

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, a: Application) -> None:
        self.created.append(a)
        self.applications[a.id] = a

    async def get(self, id: UUID) -> Application | None:
        return self.applications.get(id)

    async def update_status(
        self,
        id: UUID,
        *,
        status: ApplicationStatus,
        decision: dict[str, Any] | None = None,
        actor: str,
    ) -> None:
        self.status_writes.append((id, status, decision, actor))
        if id in self.applications:
            self.applications[id].status = status
            self.applications[id].decision = decision

    async def list(self, **kwargs: Any) -> list[Application]:
        self.list_calls.append(kwargs)
        return list(self.applications.values())


class _FakeEnrollmentRepository:
    enrollments: dict[UUID, Enrollment] = {}
    created: list[Enrollment] = []
    status_writes: list[tuple[UUID, EnrollmentStatus, Any, str]] = []
    create_error: BaseException | None = None
    list_calls: list[dict[str, Any]] = []

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, e: Enrollment) -> None:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(e)
        self.enrollments[e.id] = e

    async def get(self, id: UUID) -> Enrollment | None:
        return self.enrollments.get(id)

    async def update_status(
        self,
        id: UUID,
        *,
        status: EnrollmentStatus,
        effective_until: Any | None = None,
        actor: str,
    ) -> None:
        self.status_writes.append((id, status, effective_until, actor))
        if id in self.enrollments:
            self.enrollments[id].status = status
            self.enrollments[id].effective_until = effective_until

    async def list(self, **kwargs: Any) -> list[Enrollment]:
        self.list_calls.append(kwargs)
        return list(self.enrollments.values())


class _FakeEnrollmentChangeRepository:
    created: list[EnrollmentChange] = []

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, c: EnrollmentChange) -> None:
        self.created.append(c)


class _FakeTeachingAssignmentRepository:
    created: list[TeachingAssignment] = []

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def create(self, a: TeachingAssignment) -> None:
        self.created.append(a)


class _FakeProgramVersionRepository:
    versions: dict[UUID, Any] = {}

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def get(self, id: UUID) -> Any | None:
        return self.versions.get(id)


class _FakeSession:
    """Minimal ``AsyncSession`` double: records outbox ``enqueue`` INSERTs."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        self.executed.append((sql, params))
        return None


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch):
    """Reset + install the fake repositories for every test in this module."""
    _FakeBatchRepository.batches = {}
    _FakeBatchRepository.active_counts = {}
    _FakeBatchRepository.locks = []
    _FakeBatchRepository.status_writes = []
    _FakeBatchRepository.capacity_writes = []
    _FakeBatchRepository.list_calls = []
    _FakeApplicationRepository.applications = {}
    _FakeApplicationRepository.created = []
    _FakeApplicationRepository.status_writes = []
    _FakeApplicationRepository.list_calls = []
    _FakeEnrollmentRepository.enrollments = {}
    _FakeEnrollmentRepository.created = []
    _FakeEnrollmentRepository.status_writes = []
    _FakeEnrollmentRepository.create_error = None
    _FakeEnrollmentRepository.list_calls = []
    _FakeEnrollmentChangeRepository.created = []
    _FakeTeachingAssignmentRepository.created = []
    _FakeProgramVersionRepository.versions = {}

    for name, fake in (
        ("BatchRepository", _FakeBatchRepository),
        ("ApplicationRepository", _FakeApplicationRepository),
        ("EnrollmentRepository", _FakeEnrollmentRepository),
        ("EnrollmentChangeRepository", _FakeEnrollmentChangeRepository),
        ("TeachingAssignmentRepository", _FakeTeachingAssignmentRepository),
        ("ProgramVersionRepository", _FakeProgramVersionRepository),
    ):
        monkeypatch.setattr(app, name, fake)
    return SimpleCtx()


class SimpleCtx:
    tenant_id: UUID = UUID("11111111-1111-1111-1111-111111111111")
    account_id: UUID = UUID("22222222-2222-2222-2222-222222222222")

    @property
    def actor(self) -> str:
        return str(self.account_id)


@pytest.fixture
def ctx() -> Any:
    return SimpleCtx()


def _outbox_events(session: _FakeSession) -> list[str]:
    """Extract the ``event_type`` values from the recorded outbox INSERTs."""
    events: list[str] = []
    for sql, params in session.executed:
        if "INSERT INTO outbox_messages" in sql and params:
            events.append(str(params.get("event_type")))
    return events


# ---------------------------------------------------------------------------
# create_batch — :90 (0 -> 1, <= -> <)
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


def _published(_ctx: Any) -> UUID:
    pv_id = uuid4()
    _FakeProgramVersionRepository.versions[pv_id] = _PublishedVersion()
    return pv_id


@pytest.mark.parametrize(
    ("capacity", "expect_error"),
    [
        pytest.param(0, True, id="capacity_eq_0_rejected"),
        pytest.param(-1, True, id="capacity_negative_rejected"),
        pytest.param(1, False, id="capacity_eq_1_accepted"),
    ],
)
async def test_create_batch_capacity_boundary(
    ctx: Any, session: _FakeSession, capacity: int, expect_error: bool
) -> None:
    """V05: capacity must be positive. Boundary is ``>= 1`` (:90 ``<= 0``)."""
    pv_id = _published(ctx)
    if expect_error:
        with pytest.raises(ValidationError, match="capacity must be positive"):
            await app.create_batch(
                session,
                ctx,
                unit_id=uuid4(),
                batch_code="B-1",
                program_version_id=pv_id,
                capacity=capacity,
                delivery_mode=DeliveryMode.ONSITE,
            )
        return
    b = await app.create_batch(
        session,
        ctx,
        unit_id=uuid4(),
        batch_code="B-1",
        program_version_id=pv_id,
        capacity=capacity,
        delivery_mode=DeliveryMode.ONSITE,
    )
    assert isinstance(b, Batch)
    assert b.capacity == 1
    assert b.status is BatchStatus.PLANNED
    assert _outbox_events(session) == ["BatchCreated"]


# ---------------------------------------------------------------------------
# change_batch_capacity — :154/:158/:165/:166/:172
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new_capacity", "expect_error"),
    [
        pytest.param(0, True, id="new_capacity_eq_0_rejected"),
        pytest.param(-5, True, id="new_capacity_negative_rejected"),
        pytest.param(1, False, id="new_capacity_eq_1_accepted"),
    ],
)
async def test_change_batch_capacity_positive_boundary(
    ctx: Any, session: _FakeSession, new_capacity: int, expect_error: bool
) -> None:
    """V05 pre-condition: a batch capacity may never reach zero (:158)."""
    batch_id = uuid4()
    _FakeBatchRepository.batches[batch_id] = Batch(id=batch_id, capacity=10)
    coro = app.change_batch_capacity(
        session, ctx, batch_id=batch_id, new_capacity=new_capacity
    )
    if expect_error:
        with pytest.raises(ValidationError, match="capacity must be positive"):
            await coro
        return
    await coro


async def test_change_batch_capacity_not_found(ctx: Any, session: _FakeSession) -> None:
    with pytest.raises(ValidationError, match="Batch not found"):
        await app.change_batch_capacity(
            session, ctx, batch_id=uuid4(), new_capacity=5
        )


async def test_change_batch_capacity_equal_to_active_is_allowed(
    ctx: Any, session: _FakeSession
) -> None:
    """Q46/V05: the reject boundary is *strictly below* occupancy (:165 ``<``).

    ``new_capacity == active_count`` fills the batch exactly and must be
    accepted; only ``new_capacity < active_count`` needs ``force``.
    """
    batch_id = uuid4()
    _FakeBatchRepository.batches[batch_id] = Batch(id=batch_id, capacity=10)
    _FakeBatchRepository.active_counts[batch_id] = 7

    await app.change_batch_capacity(session, ctx, batch_id=batch_id, new_capacity=7)

    assert _FakeBatchRepository.capacity_writes == [(batch_id, 7, ctx.actor)]
    assert "BatchCapacityChanged" in _outbox_events(session)
    # No exception was audited: lowering *to* occupancy is not an exception.
    assert "BatchCapacityExceptionRecorded" not in _outbox_events(session)


async def test_change_batch_capacity_below_active_rejected_without_force(
    ctx: Any, session: _FakeSession
) -> None:
    """Q46/V05 + :166 ``not force`` — default ``force=False`` must reject.

    Also kills :154 (``False -> True``): with the default flipped to ``True``
    the call would fall through to the ``reason`` check instead of raising
    ``CapacityExceededError``.
    """
    batch_id = uuid4()
    _FakeBatchRepository.batches[batch_id] = Batch(id=batch_id, capacity=10)
    _FakeBatchRepository.active_counts[batch_id] = 7

    with pytest.raises(CapacityExceededError) as excinfo:
        await app.change_batch_capacity(
            session, ctx, batch_id=batch_id, new_capacity=6
        )

    assert excinfo.value.details == {"new_capacity": 6, "active": 7}
    assert _FakeBatchRepository.capacity_writes == []
    assert _FakeBatchRepository.status_writes == []


async def test_change_batch_capacity_force_without_reason_rejected(
    ctx: Any, session: _FakeSession
) -> None:
    """Q46 audit trail: ``force=True`` without a reason is a hard error (:172)."""
    batch_id = uuid4()
    _FakeBatchRepository.batches[batch_id] = Batch(id=batch_id, capacity=10)
    _FakeBatchRepository.active_counts[batch_id] = 7

    with pytest.raises(ValidationError, match="reason is required when force=true"):
        await app.change_batch_capacity(
            session, ctx, batch_id=batch_id, new_capacity=6, force=True
        )

    assert _FakeBatchRepository.capacity_writes == []


async def test_change_batch_capacity_force_with_reason_records_exception(
    ctx: Any, session: _FakeSession
) -> None:
    """Q46 positive path: force + reason writes capacity *and* audits it."""
    batch_id = uuid4()
    _FakeBatchRepository.batches[batch_id] = Batch(id=batch_id, capacity=10)
    _FakeBatchRepository.active_counts[batch_id] = 7

    await app.change_batch_capacity(
        session,
        ctx,
        batch_id=batch_id,
        new_capacity=3,
        force=True,
        reason="cohort consolidation",
    )

    assert _FakeBatchRepository.capacity_writes == [(batch_id, 3, ctx.actor)]
    events = _outbox_events(session)
    assert "BatchCapacityExceptionRecorded" in events
    assert "BatchCapacityChanged" in events


async def test_change_batch_capacity_locks_batch_before_counting(
    ctx: Any, session: _FakeSession
) -> None:
    """V07: the capacity change takes ``SELECT ... FOR UPDATE`` on the batch."""
    batch_id = uuid4()
    _FakeBatchRepository.batches[batch_id] = Batch(id=batch_id, capacity=10)

    await app.change_batch_capacity(session, ctx, batch_id=batch_id, new_capacity=4)

    assert _FakeBatchRepository.locks == [batch_id]


# ---------------------------------------------------------------------------
# create_application — :211 (``completeness or Completeness()``)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "expect_items"),
    [
        pytest.param(None, [], id="completeness_none_defaults_to_empty"),
        pytest.param(
            [{"item": "ktp", "status": "verified"}],
            [{"item": "ktp", "status": "verified"}],
            id="completeness_supplied_is_preserved",
        ),
    ],
)
async def test_create_application_completeness_default_vs_supplied(
    ctx: Any,
    session: _FakeSession,
    supplied: list[dict[str, Any]] | None,
    expect_items: list[dict[str, Any]],
) -> None:
    """§3.17: an explicitly supplied completeness must survive ``or`` (:211)."""
    a = await app.create_application(
        session,
        ctx,
        unit_id=uuid4(),
        participant_id=uuid4(),
        program_id=uuid4(),
        completeness=Completeness(items=list(supplied)) if supplied is not None else None,
    )

    assert a.completeness.items == expect_items
    assert a.status is ApplicationStatus.DRAFT
    assert _FakeApplicationRepository.created == [a]
    assert _outbox_events(session) == ["ApplicationCreated"]


# ---------------------------------------------------------------------------
# submit_application + decide_application — :265 / :270
# ---------------------------------------------------------------------------


async def test_submit_application_requires_draft(ctx: Any, session: _FakeSession) -> None:
    """REG-A09 lifecycle: draft -> submitted exactly once."""
    a = Application(status=ApplicationStatus.DRAFT)
    _FakeApplicationRepository.applications[a.id] = a

    await app.submit_application(session, ctx, a.id)

    assert _FakeApplicationRepository.status_writes == [
        (a.id, ApplicationStatus.SUBMITTED, None, ctx.actor)
    ]
    with pytest.raises(StateConflictError, match="Cannot submit application in status submitted"):
        await app.submit_application(session, ctx, a.id)


async def test_submit_application_not_found(ctx: Any, session: _FakeSession) -> None:
    with pytest.raises(ValidationError, match="Application not found"):
        await app.submit_application(session, ctx, uuid4())


@pytest.mark.parametrize(
    ("decision", "expect_status", "expect_event"),
    [
        pytest.param(
            "approve",
            ApplicationStatus.APPROVED,
            "ApplicationApproved",
            id="approve_maps_to_APPROVED",
        ),
        pytest.param(
            "reject",
            ApplicationStatus.REJECTED,
            "ApplicationRejected",
            id="reject_maps_to_REJECTED",
        ),
    ],
)
async def test_decide_application_approve_reject_boundary(
    ctx: Any,
    session: _FakeSession,
    decision: str,
    expect_status: ApplicationStatus,
    expect_event: str,
) -> None:
    """REG-A04: ``approve`` -> APPROVED, ``reject`` -> REJECTED (:265)."""
    a = Application(status=ApplicationStatus.SUBMITTED)
    _FakeApplicationRepository.applications[a.id] = a

    out = await app.decide_application(
        session, ctx, application_id=a.id, decision=decision, reason="panel quorum"
    )

    assert out.status is expect_status
    assert out.decision == {
        "actor": ctx.actor,
        "decision": decision,
        "reason": "panel quorum",
    }
    assert _FakeApplicationRepository.status_writes == [
        (a.id, expect_status, out.decision, ctx.actor)
    ]
    assert _outbox_events(session) == [expect_event]


@pytest.mark.parametrize(
    ("decision", "reason", "expect_reason"),
    [
        pytest.param("approve", None, "", id="missing_reason_coerced_to_empty_string"),
        pytest.param("approve", "ok", "ok", id="supplied_reason_persisted"),
    ],
)
async def test_decide_application_reason_defaults_to_empty_string(
    ctx: Any,
    session: _FakeSession,
    decision: str,
    reason: str | None,
    expect_reason: str,
) -> None:
    """REG-A04 decision payload: absent reason serialises as ``""`` (:270)."""
    a = Application(status=ApplicationStatus.UNDER_REVIEW)
    _FakeApplicationRepository.applications[a.id] = a

    out = await app.decide_application(
        session, ctx, application_id=a.id, decision=decision, reason=reason
    )

    assert out.decision["reason"] == expect_reason


async def test_decide_application_rejects_unknown_decision(
    ctx: Any, session: _FakeSession
) -> None:
    with pytest.raises(ValidationError, match="decision must be"):
        await app.decide_application(
            session, ctx, application_id=uuid4(), decision="maybe"
        )


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(ApplicationStatus.DRAFT, id="draft"),
        pytest.param(ApplicationStatus.APPROVED, id="approved"),
        pytest.param(ApplicationStatus.WITHDRAWN, id="withdrawn"),
    ],
)
async def test_decide_application_only_from_submitted_or_under_review(
    ctx: Any, session: _FakeSession, status: ApplicationStatus
) -> None:
    a = Application(status=status)
    _FakeApplicationRepository.applications[a.id] = a
    with pytest.raises(StateConflictError, match="must be submitted or under_review"):
        await app.decide_application(
            session, ctx, application_id=a.id, decision="approve"
        )


# ---------------------------------------------------------------------------
# place_enrollment — :335 (>= -> >) and :359 (or -> and)
# ---------------------------------------------------------------------------


def _open_batch(capacity: int = 10, status: BatchStatus = BatchStatus.OPEN) -> tuple[UUID, Batch]:
    batch_id = uuid4()
    b = Batch(id=batch_id, capacity=capacity, status=status)
    _FakeBatchRepository.batches[batch_id] = b
    return batch_id, b


@pytest.mark.parametrize(
    ("active", "capacity", "expect_error"),
    [
        pytest.param(10, 10, True, id="active_eq_capacity_is_full"),
        pytest.param(11, 10, True, id="active_gt_capacity_is_full"),
        pytest.param(9, 10, False, id="active_eq_capacity_minus_one_fits"),
    ],
)
async def test_place_enrollment_capacity_boundary(
    ctx: Any,
    session: _FakeSession,
    active: int,
    capacity: int,
    expect_error: bool,
) -> None:
    """V05: the batch is full exactly at ``active == capacity`` (:335 ``>=``)."""
    batch_id, _b = _open_batch(capacity=capacity)
    _FakeBatchRepository.active_counts[batch_id] = active

    coro = app.place_enrollment(
        session,
        ctx,
        participant_id=uuid4(),
        batch_id=batch_id,
        effective_from=NOW,
    )
    if expect_error:
        with pytest.raises(CapacityExceededError) as excinfo:
            await coro
        assert excinfo.value.details == {"capacity": capacity, "active": active}
        assert _FakeEnrollmentRepository.created == []
        return

    e = await coro
    assert isinstance(e, Enrollment)
    assert e.status is EnrollmentStatus.PLANNED
    assert e.batch_id == batch_id
    assert _FakeEnrollmentRepository.created == [e]


async def test_place_enrollment_locks_batch_and_promotes_to_running(
    ctx: Any, session: _FakeSession
) -> None:
    """V07 serialisation + OPEN -> RUNNING kick-off on the first placement."""
    batch_id, _b = _open_batch(capacity=5, status=BatchStatus.OPEN)
    _FakeBatchRepository.active_counts[batch_id] = 1

    e = await app.place_enrollment(
        session, ctx, participant_id=uuid4(), batch_id=batch_id, effective_from=NOW
    )

    assert _FakeBatchRepository.locks == [batch_id]
    assert _FakeBatchRepository.status_writes == [(batch_id, BatchStatus.RUNNING, "system")]
    assert _outbox_events(session) == ["ParticipantEnrolled"]
    assert _FakeEnrollmentRepository.created[0].id == e.id


async def test_place_enrollment_on_running_batch_does_not_rewrite_status(
    ctx: Any, session: _FakeSession
) -> None:
    batch_id, _b = _open_batch(capacity=5, status=BatchStatus.RUNNING)
    _FakeBatchRepository.active_counts[batch_id] = 1

    await app.place_enrollment(
        session, ctx, participant_id=uuid4(), batch_id=batch_id, effective_from=NOW
    )

    assert _FakeBatchRepository.status_writes == []


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(BatchStatus.PLANNED, id="planned"),
        pytest.param(BatchStatus.CLOSED, id="closed"),
        pytest.param(BatchStatus.CANCELLED, id="cancelled"),
    ],
)
async def test_place_enrollment_rejects_non_open_batch(
    ctx: Any, session: _FakeSession, status: BatchStatus
) -> None:
    batch_id, _b = _open_batch(capacity=5, status=status)
    with pytest.raises(StateConflictError, match="Cannot place enrollment on a batch"):
        await app.place_enrollment(
            session, ctx, participant_id=uuid4(), batch_id=batch_id, effective_from=NOW
        )
    assert _FakeEnrollmentRepository.created == []


async def test_place_enrollment_missing_batch(ctx: Any, session: _FakeSession) -> None:
    with pytest.raises(ValidationError, match="Batch not found"):
        await app.place_enrollment(
            session, ctx, participant_id=uuid4(), batch_id=uuid4(), effective_from=NOW
        )


def _integrity_error(message: str) -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception(message))


# NOTE (spec #16, T20): ``CapacityExceededError(..., code=...)`` at
# ``batch/application/__init__.py:362`` passes a keyword the ``DomainError``
# base constructor does not accept, so the translation branch *itself* raises
# ``TypeError`` instead of producing the intended ``ENROLLMENT_OVERLAP``
# ``CapacityExceededError``. That is a production defect, not a test defect —
# ticket #20 may not touch ``src/``. The branch-selection assertions below
# still pin the ``or`` at :359: they prove the translation branch was *taken*
# (TypeError escapes) rather than the raw ``IntegrityError`` being re-raised.
_OVERLAP_MARKER_MESSAGES = [
    pytest.param(
        'duplicate key value violates unique constraint "uq_enrollment_no_overlap"',
        id="named_unique_constraint_only",
    ),
    pytest.param(
        "conflicting key value violates exclusion constraint "
        '"ex_enrollment_no_overlap" SQLSTATE 23P01 (exclusion_violation)',
        id="exclusion_violation_only",
    ),
]


@pytest.mark.parametrize("message", _OVERLAP_MARKER_MESSAGES)
async def test_place_enrollment_overlap_marker_alone_takes_translation_branch(
    ctx: Any, session: _FakeSession, message: str
) -> None:
    """V06/R07: *either* overlap marker alone must select the ``or`` at :359.

    With the mutant (``or -> and``) neither single-marker message satisfies the
    guard, the raw ``IntegrityError`` is re-raised, and this test fails.
    """
    batch_id, _b = _open_batch(capacity=5)
    _FakeBatchRepository.active_counts[batch_id] = 0
    _FakeEnrollmentRepository.create_error = _integrity_error(message)

    with pytest.raises(TypeError) as excinfo:
        await app.place_enrollment(
            session, ctx, participant_id=uuid4(), batch_id=batch_id, effective_from=NOW
        )

    assert "code" in str(excinfo.value)
    assert _FakeBatchRepository.status_writes == []


async def test_place_enrollment_reraises_unrelated_integrity_error(
    ctx: Any, session: _FakeSession
) -> None:
    """A non-overlap IntegrityError is NOT translated — it propagates."""
    batch_id, _b = _open_batch(capacity=5)
    _FakeBatchRepository.active_counts[batch_id] = 0
    _FakeEnrollmentRepository.create_error = _integrity_error(
        'duplicate key value violates unique constraint "uq_enrollment_tenant_id"'
    )

    with pytest.raises(IntegrityError):
        await app.place_enrollment(
            session, ctx, participant_id=uuid4(), batch_id=batch_id, effective_from=NOW
        )


# ---------------------------------------------------------------------------
# transfer_enrollment — :516 (>= -> >)
# ---------------------------------------------------------------------------


def _source(status: EnrollmentStatus = EnrollmentStatus.ACTIVE) -> Enrollment:
    e = Enrollment(
        status=status,
        participant_id=uuid4(),
        application_id=uuid4(),
        effective_from=NOW,
    )
    _FakeEnrollmentRepository.enrollments[e.id] = e
    return e


@pytest.mark.parametrize(
    ("active", "capacity", "expect_error"),
    [
        pytest.param(4, 4, True, id="target_active_eq_capacity_is_full"),
        pytest.param(5, 4, True, id="target_active_gt_capacity_is_full"),
        pytest.param(3, 4, False, id="target_active_eq_capacity_minus_one_fits"),
    ],
)
async def test_transfer_enrollment_target_capacity_boundary(
    ctx: Any,
    session: _FakeSession,
    active: int,
    capacity: int,
    expect_error: bool,
) -> None:
    """REG-A08 / V05: the target batch is full at ``active == capacity`` (:516)."""
    src = _source(EnrollmentStatus.PLANNED)
    target_id, target = _open_batch(capacity=capacity, status=BatchStatus.RUNNING)
    _FakeBatchRepository.active_counts[target_id] = active

    coro = app.transfer_enrollment(
        session,
        ctx,
        source_enrollment_id=src.id,
        target_batch_id=target_id,
        effective_at=LATER,
        reason="participant request",
    )
    if expect_error:
        with pytest.raises(CapacityExceededError, match="Target batch is at capacity"):
            await coro
        # Source must NOT be closed when the transfer is refused.
        assert _FakeEnrollmentRepository.status_writes == []
        assert _FakeEnrollmentRepository.created == []
        return

    new_e = await coro
    assert new_e.batch_id == target_id
    assert new_e.participant_id == src.participant_id
    assert new_e.status is EnrollmentStatus.ACTIVE
    assert new_e.effective_from == LATER
    assert new_e.effective_until is None


async def test_transfer_enrollment_closes_source_and_records_change(
    ctx: Any, session: _FakeSession
) -> None:
    """REG-A08 accounting: source -> TRANSFERRED + one APPLIED TRANSFER change."""
    src = _source(EnrollmentStatus.ACTIVE)
    target_id, _target = _open_batch(capacity=4, status=BatchStatus.OPEN)
    _FakeBatchRepository.active_counts[target_id] = 1

    new_e = await app.transfer_enrollment(
        session,
        ctx,
        source_enrollment_id=src.id,
        target_batch_id=target_id,
        effective_at=LATER,
        reason="schedule clash",
    )

    assert _FakeEnrollmentRepository.status_writes == [
        (src.id, EnrollmentStatus.TRANSFERRED, LATER, ctx.actor)
    ]
    assert _FakeEnrollmentChangeRepository.created[0].change_kind.value == "transfer"
    assert _FakeEnrollmentChangeRepository.created[0].status.value == "applied"
    assert _FakeEnrollmentChangeRepository.created[0].target_enrollment_id == new_e.id
    assert _outbox_events(session) == ["EnrollmentTransferred"]
    # V07: the target row is locked before the count is taken.
    assert _FakeBatchRepository.locks == [target_id]


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(EnrollmentStatus.PAUSED, id="paused"),
        pytest.param(EnrollmentStatus.WITHDRAWN, id="withdrawn"),
        pytest.param(EnrollmentStatus.TRANSFERRED, id="transferred"),
        pytest.param(EnrollmentStatus.COMPLETED, id="completed"),
    ],
)
async def test_transfer_enrollment_rejects_non_live_source(
    ctx: Any, session: _FakeSession, status: EnrollmentStatus
) -> None:
    src = _source(status)
    with pytest.raises(StateConflictError, match="Cannot transfer enrollment in status"):
        await app.transfer_enrollment(
            session,
            ctx,
            source_enrollment_id=src.id,
            target_batch_id=uuid4(),
            effective_at=LATER,
            reason="x",
        )
    assert _FakeBatchRepository.locks == []


async def test_transfer_enrollment_source_not_found(
    ctx: Any, session: _FakeSession
) -> None:
    with pytest.raises(ValidationError, match="Source enrollment not found"):
        await app.transfer_enrollment(
            session,
            ctx,
            source_enrollment_id=uuid4(),
            target_batch_id=uuid4(),
            effective_at=LATER,
            reason="x",
        )


async def test_transfer_enrollment_rejects_non_open_target(
    ctx: Any, session: _FakeSession
) -> None:
    src = _source()
    target_id, _target = _open_batch(capacity=4, status=BatchStatus.CLOSED)
    with pytest.raises(StateConflictError, match="Cannot transfer into a batch in status"):
        await app.transfer_enrollment(
            session,
            ctx,
            source_enrollment_id=src.id,
            target_batch_id=target_id,
            effective_at=LATER,
            reason="x",
        )


# ---------------------------------------------------------------------------
# pause / resume / withdraw lifecycle
# ---------------------------------------------------------------------------


async def test_pause_enrollment_lifecycle(ctx: Any, session: _FakeSession) -> None:
    e = _source(EnrollmentStatus.ACTIVE)
    await app.pause_enrollment(
        session, ctx, enrollment_id=e.id, effective_at=NOW, reason="illness"
    )
    assert _FakeEnrollmentRepository.status_writes == [
        (e.id, EnrollmentStatus.PAUSED, None, ctx.actor)
    ]
    change = _FakeEnrollmentChangeRepository.created[0]
    assert change.change_kind.value == "pause"
    assert change.status.value == "applied"
    assert change.decision == {"actor": ctx.actor, "reason": "illness"}
    assert _outbox_events(session) == ["EnrollmentPaused"]


async def test_resume_enrollment_lifecycle(ctx: Any, session: _FakeSession) -> None:
    e = _source(EnrollmentStatus.PAUSED)
    await app.resume_enrollment(
        session, ctx, enrollment_id=e.id, effective_at=LATER
    )
    assert _FakeEnrollmentRepository.status_writes == [
        (e.id, EnrollmentStatus.ACTIVE, None, ctx.actor)
    ]
    change = _FakeEnrollmentChangeRepository.created[0]
    assert change.change_kind.value == "resume"
    assert change.effective_at == LATER


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(EnrollmentStatus.PAUSED, id="pause_needs_ACTIVE"),
        pytest.param(EnrollmentStatus.PLANNED, id="plan_needs_ACTIVE"),
        pytest.param(EnrollmentStatus.WITHDRAWN, id="withdrawn_needs_ACTIVE"),
    ],
)
async def test_pause_enrollment_requires_active(
    ctx: Any, session: _FakeSession, status: EnrollmentStatus
) -> None:
    e = _source(status)
    with pytest.raises(StateConflictError, match="Cannot pause enrollment in status"):
        await app.pause_enrollment(
            session, ctx, enrollment_id=e.id, effective_at=NOW, reason="illness"
        )


async def test_resume_enrollment_requires_paused(
    ctx: Any, session: _FakeSession
) -> None:
    e = _source(EnrollmentStatus.ACTIVE)
    with pytest.raises(StateConflictError, match="Cannot resume enrollment in status"):
        await app.resume_enrollment(
            session, ctx, enrollment_id=e.id, effective_at=LATER
        )


@pytest.mark.parametrize(
    ("status", "allowed"),
    [
        pytest.param(EnrollmentStatus.PLANNED, True, id="planned_may_withdraw"),
        pytest.param(EnrollmentStatus.ACTIVE, True, id="active_may_withdraw"),
        pytest.param(EnrollmentStatus.PAUSED, False, id="paused_may_not_withdraw"),
        pytest.param(EnrollmentStatus.COMPLETED, False, id="completed_may_not_withdraw"),
    ],
)
async def test_withdraw_enrollment_state_machine(
    ctx: Any, session: _FakeSession, status: EnrollmentStatus, allowed: bool
) -> None:
    e = _source(status)
    coro = app.withdraw_enrollment(
        session, ctx, enrollment_id=e.id, effective_at=LATER, reason="relocation"
    )
    if not allowed:
        with pytest.raises(StateConflictError, match="Cannot withdraw enrollment in status"):
            await coro
        return
    await coro
    assert _FakeEnrollmentRepository.status_writes == [
        (e.id, EnrollmentStatus.WITHDRAWN, LATER, ctx.actor)
    ]
    change = _FakeEnrollmentChangeRepository.created[0]
    assert change.change_kind.value == "withdraw"
    assert _outbox_events(session) == ["EnrollmentWithdrawn"]


# ---------------------------------------------------------------------------
# open_batch lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "allowed"),
    [
        pytest.param(BatchStatus.PLANNED, True, id="planned_may_open"),
        pytest.param(BatchStatus.OPEN, False, id="open_may_not_reopen"),
        pytest.param(BatchStatus.RUNNING, False, id="running_may_not_reopen"),
        pytest.param(BatchStatus.CLOSED, False, id="closed_may_not_reopen"),
        pytest.param(BatchStatus.CANCELLED, False, id="cancelled_may_not_reopen"),
    ],
)
async def test_open_batch_lifecycle(
    ctx: Any, session: _FakeSession, status: BatchStatus, allowed: bool
) -> None:
    batch_id = uuid4()
    _FakeBatchRepository.batches[batch_id] = Batch(id=batch_id, status=status)
    coro = app.open_batch(session, ctx, batch_id)
    if not allowed:
        with pytest.raises(StateConflictError, match="Cannot open batch in status"):
            await coro
        return
    await coro
    assert _FakeBatchRepository.status_writes == [(batch_id, BatchStatus.OPEN, ctx.actor)]
    assert _outbox_events(session) == ["BatchOpened"]


async def test_open_batch_missing(ctx: Any, session: _FakeSession) -> None:
    with pytest.raises(ValidationError, match="Batch not found"):
        await app.open_batch(session, ctx, uuid4())


# ---------------------------------------------------------------------------
# assign_teacher — :589 (== -> !=) and :599 (or -> and)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("batch_id", "session_id", "allowed"),
    [
        pytest.param(None, None, False, id="neither_rejected"),
        pytest.param("BATCH", None, True, id="batch_only_accepted"),
        pytest.param(None, "SESSION", True, id="session_only_accepted"),
        pytest.param("BATCH", "SESSION", False, id="both_rejected"),
    ],
)
async def test_assign_teacher_exactly_one_target(
    ctx: Any,
    session: _FakeSession,
    batch_id: str | None,
    session_id: str | None,
    allowed: bool,
) -> None:
    """:589 — exactly one of ``batch_id`` / ``session_id`` must be supplied."""
    coro = app.assign_teacher(
        session,
        ctx,
        unit_id=uuid4(),
        membership_id=uuid4(),
        assignment_kind=AssignmentKind.INSTRUCTOR,
        batch_id=uuid4() if batch_id else None,
        session_id=uuid4() if session_id else None,
        valid_from=NOW,
    )
    if not allowed:
        with pytest.raises(ValidationError, match="Exactly one of batch_id or session_id"):
            await coro
        assert _FakeTeachingAssignmentRepository.created == []
        return
    a = await coro
    assert isinstance(a, TeachingAssignment)
    assert (a.batch_id is None) != (a.session_id is None)
    assert a.status.value == "active"
    assert _outbox_events(session) == ["TeachingAssignmentCreated"]


@pytest.mark.parametrize(
    ("component_codes", "expect"),
    [
        pytest.param(None, [], id="component_codes_none_becomes_empty_list"),
        pytest.param([], [], id="component_codes_empty_list"),
        pytest.param(["mod-1", "mod-2"], ["mod-1", "mod-2"], id="component_codes_persisted"),
    ],
)
async def test_assign_teacher_component_codes_default_vs_supplied(
    ctx: Any,
    session: _FakeSession,
    component_codes: list[str] | None,
    expect: list[str],
) -> None:
    """:599 — ``component_codes or []`` must keep a supplied list intact."""
    a = await app.assign_teacher(
        session,
        ctx,
        unit_id=uuid4(),
        membership_id=uuid4(),
        assignment_kind=AssignmentKind.ASSESSOR,
        batch_id=uuid4(),
        component_codes=component_codes,
        valid_from=NOW,
    )

    assert a.component_codes == expect
    assert _FakeTeachingAssignmentRepository.created[0].component_codes == expect