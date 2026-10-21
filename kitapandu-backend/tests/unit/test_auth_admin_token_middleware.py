"""Unit tests for ``ksma.shared.auth.admin_token_middleware`` (ADR-0007).

Seam: the Starlette middleware that gates ``/api/v1/admin/*``:
- ``AdminTokenMiddleware.dispatch`` — path-prefix gate + downstream pass-through.
- ``get_policy(app)`` — reads the policy stashed on ``app.state`` at startup,
  falling back to dev mode.
- ``init_admin_token_policy(app, token)`` — one-shot startup wiring.
- ``unauthorized_response()`` — the fixed 401 envelope (no token echo).

Behaviour asserted: a correctly-authenticated admin request reaches the
endpoint, an unauthenticated one is rejected with 401, and non-admin paths are
untouched. Starlette middleware is exercised through a real ``TestClient``
against a tiny ASGI app — no database, no external service.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ksma.shared.auth.admin_token import (
    ADMIN_PATH_PREFIX,
    AdminTokenPolicy,
)
from ksma.shared.auth.admin_token_middleware import (
    DEFAULT_POLICY,
    AdminTokenMiddleware,
    get_policy,
    init_admin_token_policy,
    unauthorized_response,
)

TOKEN = "s3cr3t-admin-token"


async def _ok(request: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "path": request.url.path})


def _build_app(
    token: str | None, *, wire: bool = True, extra_paths: list[str] | None = None
) -> Starlette:
    routes: list[Route] = [Route(ADMIN_PATH_PREFIX + "ping", _ok)]
    routes.extend(Route(p, _ok) for p in (extra_paths or []))
    app = Starlette(routes=routes)
    app.add_middleware(AdminTokenMiddleware)
    if wire:
        init_admin_token_policy(app, token)
    return app


class TestPolicyWiring:
    def test_init_stashes_a_configured_policy(self) -> None:
        app = _build_app(TOKEN)
        policy = get_policy(app)
        assert policy.is_configured is True and policy.token == TOKEN

    def test_init_without_token_stores_dev_policy(self) -> None:
        app = _build_app(None)
        assert get_policy(app) == DEFAULT_POLICY

    def test_get_policy_falls_back_when_state_is_missing(self) -> None:
        app = _build_app(TOKEN, wire=False)
        assert get_policy(app) == DEFAULT_POLICY

    def test_get_policy_ignores_a_foreign_state_value(self) -> None:
        app = _build_app(TOKEN, wire=False)
        app.state.admin_token_policy = "not-a-policy"
        assert get_policy(app) == DEFAULT_POLICY


class TestUnauthorizedEnvelope:
    def test_shape_and_no_token_echo(self) -> None:
        response = unauthorized_response()
        assert response.status_code == 401
        body = json.loads(bytes(response.body))
        assert body["code"] == "ADMIN_TOKEN_REQUIRED"
        assert "correlation_id" in body
        assert TOKEN not in json.dumps(body)
        assert "Bearer" not in body["message"]


class TestMiddlewareDispatch:
    def test_authorised_admin_request_reaches_the_endpoint(self) -> None:
        """Kills the ``delete not`` mutant on line 68.

        A *matching* bearer credential must pass through to the route; the
        mutant turns ``if not is_authorized(...)`` into ``if
        is_authorized(...)`` and 401s every legitimate admin caller.
        """
        app = _build_app(TOKEN)
        with TestClient(app) as client:
            response = client.get(
                ADMIN_PATH_PREFIX + "ping", headers={"Authorization": f"Bearer {TOKEN}"}
            )
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "path": ADMIN_PATH_PREFIX + "ping"}

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="no_header"),
            pytest.param({"Authorization": "Bearer nope"}, id="wrong_token"),
            pytest.param({"Authorization": TOKEN}, id="missing_scheme"),
            pytest.param({"Authorization": "Bearer "}, id="empty_credential"),
        ],
    )
    def test_unauthorised_admin_request_is_401(self, headers: dict[str, str]) -> None:
        app = _build_app(TOKEN)
        with TestClient(app) as client:
            response = client.get(ADMIN_PATH_PREFIX + "ping", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "ADMIN_TOKEN_REQUIRED"

    def test_dev_mode_lets_the_admin_surface_through(self) -> None:
        app = _build_app(None)
        with TestClient(app) as client:
            response = client.get(ADMIN_PATH_PREFIX + "ping")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/api/v1/public/ping", id="non_admin_v1"),
            pytest.param("/api/v2/admin/ping", id="other_major_version"),
            pytest.param("/api/v1/administration/ping", id="prefix_lookalike"),
            pytest.param("/healthz", id="health"),
        ],
    )
    def test_paths_outside_the_prefix_are_untouched(self, path: str) -> None:
        """The gate is prefix-scoped: a wrong header elsewhere must not 401."""
        app = _build_app(TOKEN, extra_paths=[path])

        with TestClient(app) as client:
            response = client.get(path, headers={"Authorization": "Bearer nope"})
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

    def test_admin_prefix_without_trailing_segment_still_gated(self) -> None:
        """``/api/v1/admin/`` itself is inside the prefix."""
        app = _build_app(TOKEN, extra_paths=["/api/v1/admin/"])

        with TestClient(app) as client:
            assert client.get("/api/v1/admin/").status_code == 401
            assert client.get(
                "/api/v1/admin/", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code == 200

    def test_middleware_is_stateful_across_requests(self) -> None:
        """A bad request must not poison a later good one."""
        app = _build_app(TOKEN)
        with TestClient(app) as client:
            assert client.get(ADMIN_PATH_PREFIX + "ping").status_code == 401
            good = client.get(
                ADMIN_PATH_PREFIX + "ping", headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert good.status_code == 200
            assert client.get(
                ADMIN_PATH_PREFIX + "ping", headers={"Authorization": "Bearer bad"}
            ).status_code == 401


class TestHalfConfiguredPolicy:
    def test_tokenless_but_configured_policy_lets_requests_through(self) -> None:
        """End-to-end pin of the ``or -> and`` boundary from line 64 of
        ``admin_token.py``: the middleware must not 500 on a policy whose
        ``is_configured`` flag is set but whose token is ``None``."""
        app = _build_app(TOKEN, wire=False)
        app.state.admin_token_policy = AdminTokenPolicy(token=None)
        with TestClient(app) as client:
            response = client.get(ADMIN_PATH_PREFIX + "ping")
        assert response.status_code == 200, response.text