"""Unit tests for ``ksma.session.interface`` — the HTTP routing seam.

Seam
----
``ksma.session.interface`` (``src/ksma/session/interface/__init__.py``). The
router is mounted on a throwaway in-process ``FastAPI`` app with:

- ``require_tenant_context`` overridden by a canned ``TenantContext``;
- ``ksma.session.interface.session_scope`` replaced by an in-memory async
  context manager yielding a recording double;
- the application services replaced by recorders.

Public surface pinned here, from **docs/specs/004-session.md** §Testing
Decisions:

- ``POST /sessions/{id}/revisions`` (:208) — the endpoint creates the revision
  *effective*: ``make_effective=True`` is hard-coded, so a revision posted over
  HTTP always publishes itself, becomes the effective revision and moves the
  session back to ``scheduled``. A client cannot stage a draft revision.
- ``POST /sessions/{id}/close`` and ``POST /sessions/{id}/corrections`` — the
  body/response contract: a completion carries its ``completion_no`` and, on a
  correction, a ``supersedes_id`` chain; an empty ``correction_reason`` never
  reaches the application layer.
- ``POST /sessions/{id}/attendance`` — the typed ``AttendanceCode`` enum is
  enforced at the edge; an unknown code is a 422, not a 500.

Assertions are on the HTTP response (status code + JSON body) and on the
values handed to the application layer. No DB, no Docker, no network
(Starlette ``TestClient`` is in-process).

What is NOT asserted here (lives in the integration tier):

- RLS policies, real idempotency-key persistence across processes.
- The business rules behind each endpoint (see ``test_session_application.py``).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksma.session import interface as itf
from ksma.session.domain import (
    Attendance,
    AttendanceCode,
    CompletionDecision,
    SessionCompletion,
    SessionRevision,
)
from tests.unit.conftest_helpers import tenant_context

SESSION_ID = UUID("11111111-1111-1111-1111-111111111111")
ENROLLMENT_ID = UUID("22222222-2222-2222-2222-222222222222")
REVISION_ID = UUID("33333333-3333-3333-3333-333333333333")

STARTS_AT = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
ENDS_AT = datetime(2026, 1, 5, 10, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append((str(statement), params))
        return None


class _FakeAddRevision:
    """Recorder standing in for ``add_revision``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.revision_no = 1

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> SessionRevision:
        self.calls.append(kwargs)
        return SessionRevision(
            id=REVISION_ID,
            tenant_id=uuid4(),
            session_id=kwargs["session_id"],
            revision_no=self.revision_no,
            starts_at=kwargs["starts_at"],
            ends_at=kwargs["ends_at"],
            timezone=kwargs["timezone_str"],
            change_reason=kwargs["change_reason"],
            published_at=datetime.now(UTC),
        )


class _FakeCloseSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> SessionCompletion:
        self.calls.append(kwargs)
        return SessionCompletion(
            id=uuid4(),
            tenant_id=uuid4(),
            session_id=kwargs["session_id"],
            completion_no=1,
            outcome=CompletionDecision.COMPLETED,
        )


class _FakeCorrectSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.supersedes_id: UUID | None = None

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> SessionCompletion:
        self.calls.append(kwargs)
        return SessionCompletion(
            id=uuid4(),
            tenant_id=uuid4(),
            session_id=kwargs["session_id"],
            completion_no=2,
            outcome=CompletionDecision.COMPLETED,
            supersedes_id=self.supersedes_id,
            correction_reason=kwargs.get("correction_reason"),
        )


class _FakeRecordAttendance:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> Attendance:
        self.calls.append(kwargs)
        return Attendance(
            id=uuid4(),
            tenant_id=uuid4(),
            session_id=kwargs["session_id"],
            enrollment_id=kwargs["enrollment_id"],
            attendance_code=kwargs["attendance_code"],
            note=kwargs.get("note"),
            recorded_by=str(ctx.account_id),
            recorded_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mount the session router on an in-process app with every seam faked."""
    add_revision = _FakeAddRevision()
    close_session = _FakeCloseSession()
    correct_session = _FakeCorrectSession()
    record_attendance = _FakeRecordAttendance()

    monkeypatch.setattr(itf, "add_revision", add_revision)
    monkeypatch.setattr(itf, "close_session", close_session)
    monkeypatch.setattr(itf, "correct_session", correct_session)
    monkeypatch.setattr(itf, "record_attendance", record_attendance)

    @asynccontextmanager
    async def _scope() -> Any:
        yield _FakeSession()

    monkeypatch.setattr(itf, "session_scope", _scope)

    app = FastAPI()
    app.include_router(itf.router)
    app.dependency_overrides[itf.require_tenant_context] = lambda: tenant_context(
        unit_id=uuid4()
    )
    return {
        "client": TestClient(app),
        "add_revision": add_revision,
        "close_session": close_session,
        "correct_session": correct_session,
        "record_attendance": record_attendance,
    }


@pytest.fixture
def client(harness: dict[str, Any]) -> TestClient:
    return harness["client"]


def _revision_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "starts_at": STARTS_AT.isoformat(),
        "ends_at": ENDS_AT.isoformat(),
        "timezone": "Asia/Jakarta",
        "change_reason": "room change",
        "location": {"room": "R-201"},
    }
    body.update(overrides)
    return body


# ===========================================================================
# POST /sessions/{id}/revisions — the effective-revision contract
# ===========================================================================


class TestAddRevisionIsAlwaysEffective:
    """``POST /api/v1/sessions/{session_id}/revisions`` (:208)."""

    def test_make_effective_is_hard_coded_true(
        self, harness: dict[str, Any]
    ) -> None:
        """Kills :208 ``make_effective=True -> make_effective=False``.

        A revision posted over HTTP is *the* new effective revision: it
        publishes itself, becomes ``effective_revision_id`` and moves the
        session back to ``scheduled``. With ``make_effective=False`` the row
        would be written as a never-published draft that the session ignores —
        the client's reschedule would silently have no effect.
        """
        response = harness["client"].post(
            f"/api/v1/sessions/{SESSION_ID}/revisions", json=_revision_body()
        )

        assert response.status_code == 201
        assert harness["add_revision"].calls == [
            {
                "session_id": SESSION_ID,
                "starts_at": STARTS_AT,
                "ends_at": ENDS_AT,
                "timezone_str": "Asia/Jakarta",
                "change_reason": "room change",
                "location": {"room": "R-201"},
                "make_effective": True,
            }
        ]

    def test_response_carries_the_revision_number(
        self, harness: dict[str, Any]
    ) -> None:
        """The revision is returned with its monotonic ``revision_no``."""
        harness["add_revision"].revision_no = 7
        response = harness["client"].post(
            f"/api/v1/sessions/{SESSION_ID}/revisions", json=_revision_body()
        )

        body = response.json()
        assert body["revision_no"] == 7
        assert body["id"] == str(REVISION_ID)
        assert body["session_id"] == str(SESSION_ID)
        assert body["change_reason"] == "room change"
        assert body["published_at"] is not None

    def test_empty_change_reason_is_rejected_at_the_edge(self, client: TestClient) -> None:
        """``change_reason`` has a lower bound of 1 character."""
        response = client.post(
            f"/api/v1/sessions/{SESSION_ID}/revisions", json=_revision_body(change_reason="")
        )
        assert response.status_code == 422


# ===========================================================================
# POST /sessions/{id}/close — closure over HTTP
# ===========================================================================


class TestCloseSessionEndpoint:
    """``POST /api/v1/sessions/{session_id}/close``."""

    def test_completion_number_is_returned(self, harness: dict[str, Any]) -> None:
        response = harness["client"].post(f"/api/v1/sessions/{SESSION_ID}/close")

        assert response.status_code == 200
        assert harness["close_session"].calls == [{"session_id": SESSION_ID, "policy_id": None}]
        body = response.json()
        assert body["session_id"] == str(SESSION_ID)
        assert body["completion_no"] == 1
        assert body["outcome"] == "completed"
        assert body["supersedes_id"] is None


# ===========================================================================
# POST /sessions/{id}/corrections — the correction chain over HTTP
# ===========================================================================


class TestCorrectSessionEndpoint:
    """``POST /api/v1/sessions/{session_id}/corrections`` — supersedes chain."""

    def test_correction_supersedes_the_prior_completion(
        self, harness: dict[str, Any]
    ) -> None:
        prior = uuid4()
        harness["correct_session"].supersedes_id = prior

        response = harness["client"].post(
            f"/api/v1/sessions/{SESSION_ID}/corrections",
            json={"correction_reason": "attendance mis-keyed", "attendance_changes": []},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["supersedes_id"] == str(prior)
        assert body["completion_no"] == 2
        assert body["correction_reason"] == "attendance mis-keyed"
        assert harness["correct_session"].calls == [
            {
                "session_id": SESSION_ID,
                "correction_reason": "attendance mis-keyed",
                "attendance_changes": [],
            }
        ]

    def test_empty_correction_reason_is_rejected_at_the_edge(
        self, client: TestClient
    ) -> None:
        """A correction with no reason is a 422 before any write happens."""
        response = client.post(
            f"/api/v1/sessions/{SESSION_ID}/corrections",
            json={"correction_reason": "", "attendance_changes": []},
        )
        assert response.status_code == 422

    def test_absent_correction_reason_is_rejected(self, client: TestClient) -> None:
        """``correction_reason`` is required, not merely non-empty."""
        response = client.post(
            f"/api/v1/sessions/{SESSION_ID}/corrections", json={"attendance_changes": []}
        )
        assert response.status_code == 422

    def test_attendance_changes_are_forwarded_verbatim(
        self, harness: dict[str, Any]
    ) -> None:
        changes = [
            {"enrollment_id": str(ENROLLMENT_ID), "attendance_code": "present"},
            {"enrollment_id": str(uuid4()), "attendance_code": "absent"},
        ]
        harness["client"].post(
            f"/api/v1/sessions/{SESSION_ID}/corrections",
            json={"correction_reason": "late arrival", "attendance_changes": changes},
        )
        assert harness["correct_session"].calls[0]["attendance_changes"] == changes


# ===========================================================================
# POST /sessions/{id}/attendance — the typed attendance-code enum
# ===========================================================================


class TestAttendanceEndpoint:
    """``POST /api/v1/sessions/{session_id}/attendance``."""

    @pytest.mark.parametrize("code", [code.value for code in AttendanceCode])
    def test_every_attendance_code_is_accepted(
        self, harness: dict[str, Any], code: str
    ) -> None:
        """The typed enum round-trips: every member reaches the application."""
        response = harness["client"].post(
            f"/api/v1/sessions/{SESSION_ID}/attendance",
            json={"enrollment_id": str(ENROLLMENT_ID), "attendance_code": code},
        )

        assert response.status_code == 201
        assert harness["record_attendance"].calls[-1]["attendance_code"] is AttendanceCode(code)
        assert response.json()["attendance_code"] == code

    def test_unknown_code_is_rejected(self, client: TestClient) -> None:
        """Out-of-enum codes never reach the application layer."""
        response = client.post(
            f"/api/v1/sessions/{SESSION_ID}/attendance",
            json={"enrollment_id": str(ENROLLMENT_ID), "attendance_code": "maybe"},
        )
        assert response.status_code == 422