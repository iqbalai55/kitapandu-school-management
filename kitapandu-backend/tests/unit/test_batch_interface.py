"""Unit tests for ``ksma.batch.interface`` — the HTTP routing seam.

Seam
----
``ksma.batch.interface`` (``src/ksma/batch/interface/__init__.py``). The router
is mounted on a throwaway ``FastAPI`` app with:

- ``require_tenant_context`` (i.e. ``shared.auth.dependencies.tenant_context_dep``)
  overridden by a canned ``TenantContext``;
- ``ksma.batch.interface.session_scope`` replaced by an in-memory async
  context manager yielding a recording double;
- the repositories and the ``assign_teacher`` application service replaced by
  recorders.

Public surface pinned here — the pagination contract and the assignment
route's batch resolution, which is what the ``docs/specs/003`` listing
endpoints must guarantee:

- ``GET /batches`` (:207 / :208) — ``limit`` lower bound is 1 and ``offset``
  lower bound and default are 0 (page 1 of a list starts at 0).
- ``GET /applications`` (:392 / :393) — same pagination contract.
- ``GET /enrollments`` (:534) — same ``limit`` lower bound.
- ``POST /batches/{batch_id}/assignments`` (:579) — an explicit
  ``body.batch_id`` overrides the path batch; the path batch is the fallback.

Assertions are on the HTTP response (status code + JSON body) and on the
values handed to the repository layer, not on the ``Query(...)`` objects.
No DB, no Docker, no network (Starlette ``TestClient`` is in-process).

What is NOT asserted here (lives in the integration tier):

- RLS policies, real pagination at scale, Alembic round-trips.
- The business rules behind each endpoint (see ``test_batch_application.py``).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksma.batch import interface as itf
from ksma.batch.domain import (
    Application,
    ApplicationStatus,
    AssignmentKind,
    AssignmentStatus,
    Batch,
    BatchStatus,
    DeliveryMode,
    Enrollment,
    EnrollmentStatus,
    TeachingAssignment,
)
from tests.unit.conftest_helpers import tenant_context

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT_ID = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append((str(statement), params))
        return None


class _FakeBatchRepository:
    rows: list[Batch] = []
    calls: list[dict[str, Any]] = []

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def list(self, **kwargs: Any) -> list[Batch]:
        self.calls.append(kwargs)
        return list(self.rows)

    async def count_active_enrollments(self, batch_id: UUID, *, at: Any | None = None) -> int:
        return 2


class _FakeApplicationRepository:
    rows: list[Application] = []
    calls: list[dict[str, Any]] = []

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def list(self, **kwargs: Any) -> list[Application]:
        self.calls.append(kwargs)
        return list(self.rows)


class _FakeEnrollmentRepository:
    rows: list[Enrollment] = []
    calls: list[dict[str, Any]] = []

    def __init__(self, session: Any, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def list(self, **kwargs: Any) -> list[Enrollment]:
        self.calls.append(kwargs)
        return list(self.rows)


class _FakeAssignTeacher:
    calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> TeachingAssignment:
        self.calls.append(kwargs)
        return TeachingAssignment(
            id=uuid4(),
            membership_id=kwargs["membership_id"],
            batch_id=kwargs["batch_id"],
            session_id=kwargs["session_id"],
            assignment_kind=kwargs["assignment_kind"],
            component_codes=list(kwargs["component_codes"] or []),
            valid_from=kwargs["valid_from"],
            valid_until=kwargs["valid_until"],
            status=AssignmentStatus.ACTIVE,
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    _FakeBatchRepository.rows = [
        Batch(
            id=uuid4(),
            unit_id=uuid4(),
            batch_code="B-1",
            program_version_id=uuid4(),
            capacity=20,
            delivery_mode=DeliveryMode.ONSITE,
            status=BatchStatus.OPEN,
        )
    ]
    _FakeBatchRepository.calls = []
    _FakeApplicationRepository.rows = [
        Application(id=uuid4(), participant_id=uuid4(), program_id=uuid4(),
                    status=ApplicationStatus.SUBMITTED)
    ]
    _FakeApplicationRepository.calls = []
    _FakeEnrollmentRepository.rows = [
        Enrollment(id=uuid4(), participant_id=uuid4(), batch_id=uuid4(),
                   status=EnrollmentStatus.ACTIVE, effective_from=None)
    ]
    _FakeEnrollmentRepository.calls = []
    _FakeAssignTeacher.calls = []

    monkeypatch.setattr(itf, "BatchRepository", _FakeBatchRepository)
    monkeypatch.setattr(itf, "ApplicationRepository", _FakeApplicationRepository)
    monkeypatch.setattr(itf, "EnrollmentRepository", _FakeEnrollmentRepository)
    monkeypatch.setattr(itf, "assign_teacher", _FakeAssignTeacher())

    @asynccontextmanager
    async def _scope():
        yield _FakeSession()

    monkeypatch.setattr(itf, "session_scope", _scope)

    app = FastAPI()
    app.include_router(itf.router)
    ctx = tenant_context(unit_id=uuid4())
    app.dependency_overrides[itf.require_tenant_context] = lambda: ctx
    return TestClient(app)


# ---------------------------------------------------------------------------
# Pagination contracts — :207 / :208 / :392 / :393 / :534
# ---------------------------------------------------------------------------


_PAGED_LISTS = [
    pytest.param("/api/v1/batches", "batches", id="GET_batches"),
    pytest.param("/api/v1/applications", "applications", id="GET_applications"),
    pytest.param("/api/v1/enrollments", "enrollments", id="GET_enrollments"),
]


@pytest.mark.parametrize(("path", "repo_attr"), _PAGED_LISTS)
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="defaults_accepted"),
        pytest.param({"limit": "1"}, id="limit_at_lower_bound_accepted"),
        pytest.param({"offset": "0"}, id="offset_at_lower_bound_accepted"),
        pytest.param({"limit": "1", "offset": "0"}, id="first_page_accepted"),
    ],
)
def test_list_endpoint_accepts_pagination_lower_bounds(
    client: TestClient, path: str, repo_attr: str, params: dict[str, str]
) -> None:
    """``limit >= 1`` and ``offset >= 0`` — the lower bounds must be accepted."""
    r = client.get(path, params=params)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


@pytest.mark.parametrize(("path", "repo_attr"), _PAGED_LISTS)
def test_list_endpoint_rejects_limit_zero(client: TestClient, path: str, repo_attr: str) -> None:
    """``limit`` is a *page size*: 0 is meaningless and must be 422 (:207/:392/:534)."""
    r = client.get(path, params={"limit": 0})
    assert r.status_code == 422
    assert "limit" in r.text


@pytest.mark.parametrize(
    ("path", "repo_attr"),
    [
        pytest.param("/api/v1/batches", "batches", id="GET_batches"),
        pytest.param("/api/v1/applications", "applications", id="GET_applications"),
    ],
)
def test_list_endpoint_rejects_negative_offset(
    client: TestClient, path: str, repo_attr: str
) -> None:
    """``offset`` lower bound is 0; -1 must be 422 (:208/:393)."""
    r = client.get(path, params={"offset": -1})
    assert r.status_code == 422
    assert "offset" in r.text


def test_get_batches_paginates_with_zero_offset_by_default(client: TestClient) -> None:
    """``offset`` *defaults* to 0 — the first page starts at row 0 (:208)."""
    client.get("/api/v1/batches")
    assert _FakeBatchRepository.calls[-1]["offset"] == 0
    assert _FakeBatchRepository.calls[-1]["limit"] == 50


def test_get_applications_paginates_with_zero_offset_by_default(client: TestClient) -> None:
    client.get("/api/v1/applications")
    assert _FakeApplicationRepository.calls[-1]["offset"] == 0
    assert _FakeApplicationRepository.calls[-1]["limit"] == 50


def test_get_enrollments_defaults_to_first_page(client: TestClient) -> None:
    client.get("/api/v1/enrollments")
    assert _FakeEnrollmentRepository.calls[-1]["limit"] == 50


def test_get_batches_forwards_pagination_to_repository(client: TestClient) -> None:
    client.get("/api/v1/batches", params={"limit": 7, "offset": 3})
    assert _FakeBatchRepository.calls[-1]["limit"] == 7
    assert _FakeBatchRepository.calls[-1]["offset"] == 3


def test_get_batches_serialises_typed_response_model(client: TestClient) -> None:
    """``response_model=list[BatchOut]`` — status is the enum *value* string."""
    r = client.get("/api/v1/batches")
    body = r.json()
    assert len(body) == 1
    assert body[0]["status"] == BatchStatus.OPEN.value
    assert body[0]["delivery_mode"] == DeliveryMode.ONSITE.value
    assert body[0]["active_enrollments"] == 2
    assert body[0]["batch_code"] == "B-1"


def test_get_enrollments_serialises_typed_response_model(client: TestClient) -> None:
    r = client.get("/api/v1/enrollments")
    body = r.json()
    assert len(body) == 1
    assert body[0]["status"] == EnrollmentStatus.ACTIVE.value
    assert body[0]["participant_id"] == str(_FakeEnrollmentRepository.rows[0].participant_id)


# ---------------------------------------------------------------------------
# POST /batches/{batch_id}/assignments — :579
# ---------------------------------------------------------------------------


def _assignment_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "unit_id": str(uuid4()),
        "membership_id": str(uuid4()),
        "assignment_kind": AssignmentKind.INSTRUCTOR.value,
        "component_codes": ["mod-1"],
        "valid_from": "2026-10-20T12:00:00",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("body_batch", "expected_source"),
    [
        pytest.param("body", "body_batch_id", id="body_batch_id_wins_over_path"),
        pytest.param(None, "path_batch_id", id="path_batch_id_is_fallback"),
    ],
)
def test_post_teaching_assignment_resolves_batch_id(
    client: TestClient, body_batch: str | None, expected_source: str
) -> None:
    """REG-A05: ``body.batch_id or path.batch_id`` (:579).

    ``or -> and`` makes an explicitly supplied body batch resolve to the *path*
    batch instead, so both directions must be pinned.
    """
    path_batch = uuid4()
    overrides: dict[str, Any] = {}
    if body_batch is not None:
        overrides["batch_id"] = str(uuid4())
    r = client.post(
        f"/api/v1/batches/{path_batch}/assignments", json=_assignment_payload(**overrides)
    )
    assert r.status_code == 201, r.text
    sent = _FakeAssignTeacher.calls[-1]["batch_id"]
    if expected_source == "body_batch_id":
        assert sent == UUID(overrides["batch_id"])
        assert sent != path_batch
    else:
        assert sent == path_batch


def test_post_teaching_assignment_response_model(client: TestClient) -> None:
    path_batch = uuid4()
    r = client.post(
        f"/api/v1/batches/{path_batch}/assignments", json=_assignment_payload()
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch_id"] == str(path_batch)
    assert body["status"] == AssignmentStatus.ACTIVE.value
    assert body["assignment_kind"] == AssignmentKind.INSTRUCTOR.value