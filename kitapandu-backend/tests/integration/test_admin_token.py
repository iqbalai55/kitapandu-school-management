"""Integration test for the ADR-0007 admin-token gate on ``/api/v1/admin/*``.

Boots a real uvicorn process (not an in-process ASGI shortcut) so we exercise
the full stack: env var -> ``Settings`` -> ``create_app`` -> middleware ->
router. The process runs against the per-test migrated database from the
``migrated_db`` fixture and with ``KITAPANDU_ADMIN_TOKEN`` set, i.e. the exact
shape of a staging deployment.

Note on route shape: ``/api/v1/admin/tenants`` only declares ``POST`` today
(PLT-A01 create tenant). So:
- without the header -> 401 (middleware rejects before the router is reached),
- with the header    -> the router answers; a ``GET`` is a 405 method-not-allowed
  (proof the request was forwarded) and a ``POST`` is a 201 created.
Either way the assertion that matters is: **401 iff the token is missing/wrong**.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"

ADMIN_TOKEN = "foo"  # value used for KITAPANDU_ADMIN_TOKEN in this test
ADMIN_TENANTS_URL = "/api/v1/admin/tenants"


# --- helpers -----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _server_env() -> dict[str, str]:
    """Env for the uvicorn subprocess, pointed at the per-test database."""
    env = os.environ.copy()
    # ``migrated_db`` has already exported the test DB DSNs into os.environ.
    assert env.get("KITAPANDU_POSTGRES_DSN"), "migrated_db must run first"
    # Settings has required fields; fill any the caller did not export.
    env.setdefault("KITAPANDU_JWT_SECRET", "integration-test-secret")
    env.setdefault("KITAPANDU_MINIO_ACCESS_KEY", "minioadmin")
    env.setdefault("KITAPANDU_MINIO_SECRET_KEY", "minioadmin")
    env["KITAPANDU_ADMIN_TOKEN"] = ADMIN_TOKEN
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep + existing if existing else ''}"
    return env


async def _wait_until_healthy(
    base_url: str, proc: asyncio.subprocess.Process, *, deadline_seconds: float = 30.0
) -> None:
    deadline = time.monotonic() + deadline_seconds
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                raise RuntimeError(f"uvicorn died early (rc={proc.returncode})")
            try:
                r = await client.get(f"{base_url}/health")
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise TimeoutError(f"uvicorn did not become healthy within {deadline_seconds}s")


@pytest_asyncio.fixture
async def admin_api(migrated_db: AsyncEngine) -> AsyncIterator[str]:
    """Yield the base URL of a live uvicorn with the admin token configured."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "ksma.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
        cwd=str(PROJECT_ROOT),
        env=_server_env(),
        # KSMA_DEBUG_UVICORN=1 lets the server's tracebacks reach the test output.
        stdout=None if os.environ.get("KSMA_DEBUG_UVICORN") else asyncio.subprocess.DEVNULL,
        stderr=None if os.environ.get("KSMA_DEBUG_UVICORN") else asyncio.subprocess.STDOUT,
    )
    try:
        await _wait_until_healthy(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=10)
        if proc.returncode is None:  # pragma: no cover - defensive
            proc.kill()


@pytest_asyncio.fixture
async def client(admin_api: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=admin_api, timeout=10.0) as c:
        yield c


# --- tests -------------------------------------------------------------------


async def test_admin_route_without_header_is_401(client: httpx.AsyncClient) -> None:
    """No Authorization header => 401 with the fixed envelope."""
    r = await client.get(ADMIN_TENANTS_URL)
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["code"] == "ADMIN_TOKEN_REQUIRED"
    assert "correlation_id" in body


async def test_admin_post_without_header_is_401(client: httpx.AsyncClient) -> None:
    """PLT-A01 create tenant is gated too — nothing is created."""
    r = await client.post(ADMIN_TENANTS_URL, json={"display_name": "Sneaky Tenant"})
    assert r.status_code == 401, r.text
    assert r.json()["code"] == "ADMIN_TOKEN_REQUIRED"


async def test_admin_route_with_wrong_token_is_401(client: httpx.AsyncClient) -> None:
    r = await client.get(ADMIN_TENANTS_URL, headers={"Authorization": "Bearer not-the-token"})
    assert r.status_code == 401, r.text


async def test_admin_route_with_wrong_scheme_is_401(client: httpx.AsyncClient) -> None:
    r = await client.get(ADMIN_TENANTS_URL, headers={"Authorization": f"Token {ADMIN_TOKEN}"})
    assert r.status_code == 401, r.text


async def test_admin_route_with_correct_token_is_forwarded(client: httpx.AsyncClient) -> None:
    """Correct token => the request reaches the router (no 401).

    ``GET /api/v1/admin/tenants`` is POST-only today, so the router answers 405 —
    that is the proof the middleware let the request through.
    """
    r = await client.get(ADMIN_TENANTS_URL, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    assert r.status_code != 401, r.text
    assert r.status_code == 405, r.text


async def test_admin_post_with_correct_token_is_forwarded(client: httpx.AsyncClient) -> None:
    """PLT-A01 with the right token gets past the gate and into the handler.

    NOTE: we assert "not 401" rather than 201 on purpose. The gate is what this
    ticket adds; ``create_tenant`` itself currently blows up *inside the handler*
    with ``NotNullViolationError: null value in column "created_at"`` because
    ``TenantRow.created_at``/``updated_at`` are NOT NULL with no default and
    ``identity.application.create_tenant`` never sets them. That is a
    pre-existing identity-BC bug, unrelated to ADR-0007 — once it is fixed this
    assertion still holds and the status becomes 201.
    """
    r = await client.post(
        ADMIN_TENANTS_URL,
        json={"display_name": "Gate Test Tenant", "default_timezone": "Asia/Jakarta"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert r.status_code != 401, r.text
    assert r.status_code != 403, r.text
    if r.headers.get("content-type", "").startswith("application/json"):
        assert r.json().get("code") != "ADMIN_TOKEN_REQUIRED"
    if r.status_code == 201:  # pragma: no cover - depends on the identity fix
        body = r.json()
        assert body["display_name"] == "Gate Test Tenant"
        assert body["service_status"] == "setup"
        assert body["id"]


async def test_401_payload_does_not_leak_token(client: httpx.AsyncClient) -> None:
    """The 401 body never echoes the presented or the configured token."""
    presented = "super-secret-guess"
    r = await client.get(ADMIN_TENANTS_URL, headers={"Authorization": f"Bearer {presented}"})
    assert r.status_code == 401
    assert presented not in r.text
    assert ADMIN_TOKEN not in r.text

    # Missing-header and wrong-token failures are indistinguishable.
    r2 = await client.get(ADMIN_TENANTS_URL)
    assert r2.status_code == r.status_code
    assert r2.json()["code"] == r.json()["code"]
    assert r2.json()["message"] == r.json()["message"]


async def test_non_admin_routes_are_untouched(client: httpx.AsyncClient) -> None:
    """The gate must not leak onto /health or /docs."""
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200


async def test_dev_mode_open_surface_logs_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset token => WARNING + open surface (no 401), in-process."""
    from ksma.main import create_app
    from ksma.settings import get_settings

    monkeypatch.setenv("KITAPANDU_ADMIN_TOKEN", "")
    get_settings.cache_clear()
    try:
        app = create_app()
        policy = app.state.admin_token_policy
        assert policy.is_configured is False

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(ADMIN_TENANTS_URL)
            # Not 401: the router answers (POST-only route => 405).
            assert r.status_code != 401
            assert r.status_code == 405
    finally:
        monkeypatch.delenv("KITAPANDU_ADMIN_TOKEN", raising=False)
        get_settings.cache_clear()