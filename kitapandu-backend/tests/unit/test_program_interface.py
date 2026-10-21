"""Unit tests for ``ksma.program.interface`` — the HTTP routing seam.

Seam
----
``ksma.program.interface`` (``src/ksma/program/interface/__init__.py``). The
router is mounted on a throwaway in-process ``FastAPI`` app with:

- ``require_tenant_context`` overridden by a canned ``TenantContext``;
- ``ksma.program.interface.session_scope`` replaced by an in-memory async
  context manager yielding a recording double;
- the repositories and the application services replaced by recorders.

Public surface pinned here — the listing pagination contract from
**docs/specs/003-program-and-class.md**:

- ``GET /programs`` (:190, :191) — ``limit`` lower bound is 1 with default 50;
  ``offset`` lower bound *and* default are 0 (page 1 of a listing starts at 0).
- ``POST /programs/{id}/versions/{version_id}/publish`` (:311) — the
  ``make_current`` query parameter defaults to ``True``, so a bare publish
  request makes the version the program's current one.

Assertions are on the HTTP response (status code + JSON body) and on the
values handed to the repository/application layer, not on the ``Query(...)``
objects. No DB, no Docker, no network (Starlette ``TestClient`` is in-process).

What is NOT asserted here (lives in the integration tier):

- RLS policies, real pagination at scale, Alembic round-trips.
- The business rules behind each endpoint (see ``test_program_application.py``).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksma.program import interface as itf
from ksma.program.domain import Program, ProgramStatus, ProgramVersion, ProgramVersionStatus
from tests.unit.conftest_helpers import tenant_context

PROGRAM_ID = UUID("11111111-1111-1111-1111-111111111111")
VERSION_ID = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """Enough of ``AsyncSession`` for a routed handler that fakes its writes."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append((str(statement), params))
        return None


class _FakeProgramRepository:
    """Recorder standing in for ``ProgramRepository``."""

    def __init__(self) -> None:
        self.rows: dict[UUID, Program] = {}
        self.list_calls: list[dict[str, Any]] = []

    def __call__(self, session: Any, tenant_id: UUID) -> _FakeProgramRepository:
        return self

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
        self.list_calls.append(
            {"unit_id": unit_id, "status": status, "limit": limit, "offset": offset}
        )
        rows = [p for p in self.rows.values() if p.unit_id == unit_id] if unit_id else list(
            self.rows.values()
        )
        if status is not None:
            rows = [p for p in rows if p.status is status]
        return rows[offset : offset + limit]


class _FakeProgramVersionRepository:
    """Returns a publishable ``ProgramVersion`` for the re-read after publish."""

    def __init__(self) -> None:
        self.rows: dict[UUID, ProgramVersion] = {}

    def __call__(self, session: Any, tenant_id: UUID) -> _FakeProgramVersionRepository:
        return self

    async def get(self, id: UUID) -> ProgramVersion | None:
        return self.rows.get(id)

    async def list_for_program(self, program_id: UUID) -> list[ProgramVersion]:
        return [v for v in self.rows.values() if v.program_id == program_id]


class _FakePublishProgramVersion:
    """Recorder standing in for ``publish_program_version``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mount the program router on an in-process app with every seam faked."""
    programs = _FakeProgramRepository()
    versions = _FakeProgramVersionRepository()
    publish = _FakePublishProgramVersion()

    monkeypatch.setattr(itf, "ProgramRepository", programs)
    monkeypatch.setattr(itf, "ProgramVersionRepository", versions)
    monkeypatch.setattr(itf, "publish_program_version", publish)

    @asynccontextmanager
    async def _scope() -> Any:
        yield _FakeSession()

    monkeypatch.setattr(itf, "session_scope", _scope)

    app = FastAPI()
    app.include_router(itf.router)
    app.dependency_overrides[itf.require_tenant_context] = lambda: tenant_context(
        unit_id=uuid4()
    )
    return {"client": TestClient(app), "programs": programs, "versions": versions, "publish": publish}


@pytest.fixture
def client(harness: dict[str, Any]) -> TestClient:
    return harness["client"]


def _program(*, unit_id: UUID, code: str = "P-001") -> Program:
    return Program(
        tenant_id=uuid4(),
        unit_id=unit_id,
        program_code=code,
        name="Kriya Bengali A1",
        service_group="language",
        status=ProgramStatus.ACTIVE,
    )


def _version(*, program_id: UUID) -> ProgramVersion:
    return ProgramVersion(
        tenant_id=uuid4(),
        unit_id=uuid4(),
        program_id=program_id,
        version_no=1,
        learning_objectives="Greet and introduce oneself",
        status=ProgramVersionStatus.PUBLISHED,
    )


# ===========================================================================
# GET /programs — pagination bounds (:190, :191)
# ===========================================================================


class TestListPaginationBounds:
    """``GET /api/v1/programs`` — the listing window (:190, :191)."""

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({}, id="defaults"),
            pytest.param({"limit": "1"}, id="limit_at_lower_bound"),
            pytest.param({"limit": "200"}, id="limit_at_upper_bound"),
            pytest.param({"limit": "50"}, id="limit_at_default"),
            pytest.param({"offset": "0"}, id="offset_at_lower_bound"),
            pytest.param({"offset": "7"}, id="offset_above_bound"),
        ],
    )
    def test_valid_windows_are_accepted(
        self, client: TestClient, params: dict[str, str]
    ) -> None:
        assert client.get("/api/v1/programs", params=params).status_code == 200

    def test_limit_zero_is_rejected(self, client: TestClient) -> None:
        """Kills :190 ``ge=1 -> ge=0``.

        ``limit=0`` asks for an empty page; accepting it would hand the client
        a page-1 request with no lower bound at all, and every page after it a
        silently empty response.
        """
        assert client.get("/api/v1/programs", params={"limit": "0"}).status_code == 422

    def test_limit_below_the_lower_bound_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/v1/programs", params={"limit": "-1"}).status_code == 422

    def test_limit_above_the_upper_bound_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/v1/programs", params={"limit": "201"}).status_code == 422

    def test_negative_offset_is_rejected(self, client: TestClient) -> None:
        """Kills :191 ``ge=0 -> ge=1`` (lower half of the mutation pair).

        ``offset`` is a 0-based index; a negative one is meaningless to the
        ``LIMIT/OFFSET`` clause it feeds.
        """
        assert client.get("/api/v1/programs", params={"offset": "-1"}).status_code == 422

    def test_offset_at_the_lower_bound_is_accepted(self, client: TestClient) -> None:
        """Boundary pair for :191 ``ge=0 -> ge=1`` — the on-boundary value 0.

        Page 1 of a listing starts at offset 0, so 0 must NOT be rejected.
        """
        response = client.get("/api/v1/programs", params={"offset": "0"})
        assert response.status_code == 200

    def test_offset_defaults_to_zero(self, harness: dict[str, Any]) -> None:
        """Kills :191 ``default=0 -> default=1``.

        Page 1 of a listing starts at offset 0; defaulting to 1 would skip the
        first row of every default request.
        """
        harness["client"].get("/api/v1/programs")
        assert harness["programs"].list_calls[-1]["offset"] == 0

    def test_limit_defaults_to_fifty(self, harness: dict[str, Any]) -> None:
        """Boundary pair for :190 — the declared default window is 50 rows."""
        harness["client"].get("/api/v1/programs")
        assert harness["programs"].list_calls[-1]["limit"] == 50

    def test_supplied_window_reaches_the_repository(self, harness: dict[str, Any]) -> None:
        harness["client"].get("/api/v1/programs", params={"limit": "5", "offset": "10"})
        call = harness["programs"].list_calls[-1]
        assert call["limit"] == 5
        assert call["offset"] == 10


# ===========================================================================
# GET /programs — the response projection
# ===========================================================================


class TestListResponse:
    """The listing serialises the program rows the repository returned."""

    def test_rows_are_projected_into_the_response(self, harness: dict[str, Any]) -> None:
        unit_id = uuid4()
        program = _program(unit_id=unit_id, code="P-042")
        harness["programs"].rows[program.id] = program

        response = harness["client"].get(
            "/api/v1/programs", params={"unit_id": str(unit_id)}
        )

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["id"] == str(program.id)
        assert body[0]["program_code"] == "P-042"
        assert body[0]["status"] == "active"
        assert harness["programs"].list_calls[-1]["unit_id"] == unit_id

    def test_empty_window_returns_an_empty_list(self, client: TestClient) -> None:
        response = client.get("/api/v1/programs", params={"offset": "999"})
        assert response.status_code == 200
        assert response.json() == []


# ===========================================================================
# POST /programs/{id}/versions/{version_id}/publish — make_current default
# ===========================================================================


class TestPublishMakeCurrentDefault:
    """``POST /programs/{program_id}/versions/{version_id}/publish`` (:311)."""

    def test_make_current_defaults_to_true(self, harness: dict[str, Any]) -> None:
        """Kills :311 ``Query(default=True) -> Query(default=False)``.

        A bare publish request (no ``make_current`` in the query string) must
        make the published version the program's current one — that is the
        default the endpoint advertises.
        """
        version = _version(program_id=PROGRAM_ID)
        harness["versions"].rows[version.id] = version

        response = harness["client"].post(
            f"/api/v1/programs/{PROGRAM_ID}/versions/{version.id}/publish"
        )

        assert response.status_code == 200
        assert harness["publish"].calls == [
            {"version_id": version.id, "make_current": True}
        ]

    def test_make_current_false_is_forwarded(self, harness: dict[str, Any]) -> None:
        """Boundary pair: the explicit opt-out reaches the application layer."""
        version = _version(program_id=PROGRAM_ID)
        harness["versions"].rows[version.id] = version

        response = harness["client"].post(
            f"/api/v1/programs/{PROGRAM_ID}/versions/{version.id}/publish",
            params={"make_current": "false"},
        )

        assert response.status_code == 200
        assert harness["publish"].calls == [
            {"version_id": version.id, "make_current": False}
        ]

    def test_response_reports_the_published_status(self, harness: dict[str, Any]) -> None:
        """The handler re-reads the version and returns its post-publish state."""
        version = _version(program_id=PROGRAM_ID)
        harness["versions"].rows[version.id] = version

        response = harness["client"].post(
            f"/api/v1/programs/{PROGRAM_ID}/versions/{version.id}/publish"
        )

        body = response.json()
        assert body["id"] == str(version.id)
        assert body["program_id"] == str(PROGRAM_ID)
        assert body["version_no"] == 1
        assert body["status"] == "published"