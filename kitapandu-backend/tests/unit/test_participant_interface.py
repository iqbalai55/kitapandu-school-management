"""Unit tests for ``ksma.participant.interface`` — the HTTP routing seam.

Seam
----
``ksma.participant.interface`` (``src/ksma/participant/interface/__init__.py``).
The router is mounted on a throwaway ``FastAPI`` app with:

- ``require_tenant_context`` (i.e. ``shared.auth.dependencies.tenant_context_dep``)
  overridden by a canned ``TenantContext``;
- ``ksma.participant.interface.session_scope`` replaced by an in-memory async
  context manager yielding a recording double;
- the application services (``search_participants``, ``request_account_link``,
  ``revoke_account_link``, ``revoke_relation``) replaced by recorders.

Public surface pinned here — the listing pagination contract and the link /
relation request shapes from **docs/specs/002-participant.md**:

- ``GET /participants`` (:188, :189) — ``limit`` lower bound is 1 with default
  50; ``offset`` lower bound *and* default are 0 (page 1 of a list starts at 0).
- ``GET /participants`` (:225) — ``next_offset`` is ``offset + len(items)`` on a
  full page and ``None`` on a partial page (the client stops paginating).
- ``POST /participants/{id}/account-link`` (:313) — an explicit
  ``body.participant_id`` overrides the path participant; the path participant
  is the fallback.
- ``POST /participants/{id}/account-link/{link_id}/revoke`` (:360) and
  ``POST /participants/{id}/relations/{relation_id}/revoke`` (:434) — the
  ``reason`` query parameter has a lower bound of 1 character.

Assertions are on the HTTP response (status code + JSON body) and on the
values handed to the application layer, not on the ``Query(...)`` objects.
No DB, no Docker, no network (Starlette ``TestClient`` is in-process).

What is NOT asserted here (lives in the integration tier):
- RLS policies, real pagination at scale, Alembic round-trips.
- The business rules behind each endpoint (see ``test_participant_application.py``).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksma.participant import interface as itf
from ksma.participant.application import SearchResult
from ksma.participant.domain import (
    DuplicateSuspect,
    LinkStatus,
    Participant,
    ParticipantAccountLink,
    ParticipantStatus,
    RelationKind,
    RelationStatus,
    ParticipantRelation,
)
from tests.unit.conftest_helpers import tenant_context

PATH_PARTICIPANT_ID = UUID("11111111-1111-1111-1111-111111111111")
BODY_PARTICIPANT_ID = UUID("22222222-2222-2222-2222-222222222222")
ACCOUNT_ID = UUID("33333333-3333-3333-3333-333333333333")
RELATED_PARTY_ID = UUID("44444444-4444-4444-4444-444444444444")
LINK_ID = UUID("55555555-5555-5555-5555-555555555555")
RELATION_ID = UUID("66666666-6666-6666-6666-666666666666")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append((str(statement), params))
        return None


def _participant(*, full_name: str = "Alice Wibowo") -> Participant:
    return Participant(
        id=PATH_PARTICIPANT_ID,
        tenant_id=uuid4(),
        participant_no="P-0001",
        full_name=full_name,
        birth_date=date(2014, 5, 17),
        contact={"email": "alice@example.test"},
        identity_reference=None,
        status=ParticipantStatus.ACTIVE,
    )


class _FakeSearch:
    """Recorder standing in for ``search_participants``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: list[Participant] = []
        self.suspects: list[DuplicateSuspect] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> SearchResult:
        self.calls.append(kwargs)
        return SearchResult(
            participants=list(self.results), duplicate_suspects=list(self.suspects)
        )


class _FakeRequestAccountLink:
    """Recorder standing in for ``request_account_link``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> ParticipantAccountLink:
        self.calls.append(kwargs)
        return ParticipantAccountLink(
            id=LINK_ID,
            tenant_id=uuid4(),
            account_id=kwargs["account_id"],
            participant_id=kwargs.get("participant_id"),
            related_party_id=kwargs.get("related_party_id"),
            status=LinkStatus.PENDING,
        )


class _FakeRevokeAccountLink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _FakeRevokeRelation:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _FakeRevokeRelationService:
    """Returns a typed relation so the 204 route can complete its handler."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session: Any, ctx: Any, **kwargs: Any) -> ParticipantRelation:
        self.calls.append(kwargs)
        return ParticipantRelation(
            id=RELATION_ID,
            tenant_id=uuid4(),
            participant_id=PATH_PARTICIPANT_ID,
            related_party_id=RELATED_PARTY_ID,
            relation_kind=RelationKind.PAYER,
            status=RelationStatus.PENDING,
        )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mount the participant router on an in-process app with all seams faked."""
    search = _FakeSearch()
    request_link = _FakeRequestAccountLink()
    revoke_link = _FakeRevokeAccountLink()
    revoke_relation = _FakeRevokeRelationService()

    monkeypatch.setattr(itf, "search_participants", search)
    monkeypatch.setattr(itf, "request_account_link", request_link)
    monkeypatch.setattr(itf, "revoke_account_link", revoke_link)
    monkeypatch.setattr(itf, "revoke_relation", revoke_relation)

    @asynccontextmanager
    async def _scope():
        yield _FakeSession()

    monkeypatch.setattr(itf, "session_scope", _scope)

    app = FastAPI()
    app.include_router(itf.router)
    app.dependency_overrides[itf.require_tenant_context] = lambda: tenant_context(
        unit_id=uuid4()
    )
    return {
        "client": TestClient(app),
        "search": search,
        "request_link": request_link,
        "revoke_link": revoke_link,
        "revoke_relation": revoke_relation,
    }


@pytest.fixture
def client(harness: dict[str, Any]) -> TestClient:
    return harness["client"]


# ===========================================================================
# GET /participants — pagination bounds (:188, :189)
# ===========================================================================


class TestListPaginationBounds:
    """``GET /api/v1/participants`` — the listing window (:188, :189)."""

    @pytest.mark.parametrize(
        ("params", "expected_status"),
        [
            pytest.param({}, 200, id="defaults_accepted"),
            pytest.param({"limit": "1"}, 200, id="limit_at_lower_bound_accepted"),
            pytest.param({"limit": "200"}, 200, id="limit_at_upper_bound_accepted"),
            pytest.param({"offset": "0"}, 200, id="offset_at_lower_bound_accepted"),
            pytest.param({"offset": "7"}, 200, id="offset_above_bound_accepted"),
        ],
    )
    def test_valid_windows_are_accepted(
        self, client: TestClient, params: dict[str, str], expected_status: int
    ) -> None:
        assert client.get("/api/v1/participants", params=params).status_code == expected_status

    def test_limit_zero_is_rejected(self, client: TestClient) -> None:
        """Kills :188 ``ge=1 -> ge=0``.

        ``limit=0`` asks for an empty page; accepting it would hand the client
        an unbounded page-1 request with no lower bound at all.
        """
        response = client.get("/api/v1/participants", params={"limit": "0"})
        assert response.status_code == 422

    def test_limit_below_the_lower_bound_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/v1/participants", params={"limit": "-1"}).status_code == 422

    def test_negative_offset_is_rejected(self, client: TestClient) -> None:
        """Kills :189 ``ge=0 -> ge=1``.

        ``offset`` is a 0-based index; a negative one is meaningless to the
        ``LIMIT/OFFSET`` clause it feeds.
        """
        assert client.get("/api/v1/participants", params={"offset": "-1"}).status_code == 422

    def test_offset_at_the_lower_bound_is_accepted(self, client: TestClient) -> None:
        """Boundary pair for :189 ``ge=0`` — the on-boundary value 0."""
        response = client.get("/api/v1/participants", params={"offset": "0"})
        assert response.status_code == 200

    def test_offset_defaults_to_zero(self, harness: dict[str, Any]) -> None:
        """Kills :189 ``default=0 -> default=1``.

        Page 1 of a listing starts at offset 0; defaulting to 1 would skip the
        first row of every default request.
        """
        response = harness["client"].get("/api/v1/participants")
        assert response.status_code == 200
        assert harness["search"].calls == [
            {"query": None, "status": None, "limit": 50, "offset": 0}
        ]

    def test_limit_defaults_to_fifty(self, harness: dict[str, Any]) -> None:
        response = harness["client"].get("/api/v1/participants")
        assert response.status_code == 200
        assert harness["search"].calls[0]["limit"] == 50

    def test_explicit_window_is_forwarded_to_the_application_layer(
        self, harness: dict[str, Any]
    ) -> None:
        harness["client"].get("/api/v1/participants", params={"limit": "10", "offset": "30"})
        assert harness["search"].calls[0] == {
            "query": None,
            "status": None,
            "limit": 10,
            "offset": 30,
        }


# ===========================================================================
# GET /participants — next_offset (:225)
# ===========================================================================


class TestListNextOffset:
    """``SearchResponse.next_offset`` — the pagination cursor (:225)."""

    def test_full_page_reports_the_next_offset(self, harness: dict[str, Any]) -> None:
        """Kills :225 ``+ -> -``.

        A full page means "there may be more", so the cursor must advance by
        the page size; subtracting would walk backwards through the result set.
        """
        harness["search"].results = [_participant(), _participant(full_name="Budi")]

        response = harness["client"].get("/api/v1/participants", params={"limit": "2"})

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 2
        assert body["next_offset"] == 2

    def test_full_page_from_a_non_zero_offset_advances_from_that_offset(
        self, harness: dict[str, Any]
    ) -> None:
        harness["search"].results = [_participant(), _participant(full_name="Budi")]

        response = harness["client"].get(
            "/api/v1/participants", params={"limit": "2", "offset": "10"}
        )

        assert response.json()["next_offset"] == 12

    @pytest.mark.parametrize(
        ("limit", "item_count", "expected_next_offset"),
        [
            pytest.param("2", 1, None, id="partial_page_stops_pagination"),
            pytest.param("2", 0, None, id="empty_page_stops_pagination"),
            pytest.param("50", 2, None, id="short_first_page_stops_pagination"),
            pytest.param("2", 2, 2, id="exactly_full_page_continues"),
        ],
    )
    def test_next_offset_is_none_unless_the_page_is_exactly_full(
        self,
        harness: dict[str, Any],
        limit: str,
        item_count: int,
        expected_next_offset: int | None,
    ) -> None:
        """Kills :225 ``== -> !=`` plus the off-boundary side.

        The cursor is only offered when the page is exactly ``limit`` long;
        a short page means the caller has reached the end of the result set.
        """
        harness["search"].results = [
            _participant(full_name=f"P{i}") for i in range(item_count)
        ]

        response = harness["client"].get("/api/v1/participants", params={"limit": limit})

        assert response.status_code == 200
        assert response.json()["next_offset"] == expected_next_offset

    def test_response_shape_includes_typed_items_and_suspects(
        self, harness: dict[str, Any]
    ) -> None:
        harness["search"].results = [_participant()]
        harness["search"].suspects = [
            DuplicateSuspect(
                participant_id=PATH_PARTICIPANT_ID,
                matched_participant_ids=(BODY_PARTICIPANT_ID,),
                reason="same_name",
            )
        ]

        body = harness["client"].get("/api/v1/participants").json()

        assert body["items"][0]["participant_no"] == "P-0001"
        assert body["items"][0]["full_name"] == "Alice Wibowo"
        assert body["items"][0]["birth_date"] == "2014-05-17"
        assert body["items"][0]["status"] == "active"
        assert body["items"][0]["has_support_needs"] is False
        assert body["duplicate_suspects"] == [
            {
                "participant_id": str(PATH_PARTICIPANT_ID),
                "matched_participant_ids": [str(BODY_PARTICIPANT_ID)],
                "reason": "same_name",
            }
        ]


# ===========================================================================
# POST /participants/{id}/account-link — body vs path resolution (:313)
# ===========================================================================


class TestAccountLinkParticipantResolution:
    """``POST /participants/{participant_id}/account-link`` (:313)."""

    def test_body_participant_id_overrides_the_path(self, harness: dict[str, Any]) -> None:
        """Kills :313 ``or -> and``.

        An explicit body target wins: it is the resolution the caller asked
        for (e.g. linking a payer party discovered during intake).
        """
        response = harness["client"].post(
            f"/api/v1/participants/{PATH_PARTICIPANT_ID}/account-link",
            json={
                "account_id": str(ACCOUNT_ID),
                "participant_id": str(BODY_PARTICIPANT_ID),
                "evidence": {"document_ref": "doc-1"},
            },
        )

        assert response.status_code == 201
        assert harness["request_link"].calls[0]["participant_id"] == BODY_PARTICIPANT_ID

    def test_path_participant_id_is_the_fallback(self, harness: dict[str, Any]) -> None:
        """Boundary pair for :313 — the ``or`` fallback side."""
        response = harness["client"].post(
            f"/api/v1/participants/{PATH_PARTICIPANT_ID}/account-link",
            json={
                "account_id": str(ACCOUNT_ID),
                "evidence": {"document_ref": "doc-1"},
            },
        )

        assert response.status_code == 201
        assert harness["request_link"].calls[0]["participant_id"] == PATH_PARTICIPANT_ID

    def test_related_party_link_forwards_both_ids(self, harness: dict[str, Any]) -> None:
        response = harness["client"].post(
            f"/api/v1/participants/{PATH_PARTICIPANT_ID}/account-link",
            json={
                "account_id": str(ACCOUNT_ID),
                "related_party_id": str(RELATED_PARTY_ID),
                "evidence": {"document_ref": "doc-1"},
            },
        )

        assert response.status_code == 201
        call = harness["request_link"].calls[0]
        assert call["participant_id"] == PATH_PARTICIPANT_ID
        assert call["related_party_id"] == RELATED_PARTY_ID
        assert response.json()["related_party_id"] == str(RELATED_PARTY_ID)


# ===========================================================================
# Revoke routes — reason lower bound (:360, :434)
# ===========================================================================


class TestRevokeReasonBounds:
    """``reason`` is a mandatory, non-empty audit string on both revoke routes."""

    @pytest.mark.parametrize(
        ("route", "recorder", "path"),
        [
            pytest.param(
                "/api/v1/participants/{pid}/account-link/{lid}/revoke",
                "revoke_link",
                f"/api/v1/participants/{PATH_PARTICIPANT_ID}/account-link/{LINK_ID}/revoke",
                id="account_link",
            ),
            pytest.param(
                "/api/v1/participants/{pid}/relations/{rid}/revoke",
                "revoke_relation",
                f"/api/v1/participants/{PATH_PARTICIPANT_ID}/relations/{RELATION_ID}/revoke",
                id="relation",
            ),
        ],
    )
    def test_blank_reason_is_rejected(
        self, harness: dict[str, Any], route: str, recorder: str, path: str
    ) -> None:
        """Kills :360 / :434 ``min_length=1 -> min_length=0``.

        A revocation with an empty reason has no audit value — the whole point
        of the required parameter — so it must be refused at the edge.
        """
        response = harness["client"].post(f"{path}?reason=")

        assert response.status_code == 422
        assert harness[recorder].calls == []

    @pytest.mark.parametrize(
        ("route", "recorder", "path"),
        [
            pytest.param(
                "/api/v1/participants/{pid}/account-link/{lid}/revoke",
                "revoke_link",
                f"/api/v1/participants/{PATH_PARTICIPANT_ID}/account-link/{LINK_ID}/revoke",
                id="account_link",
            ),
            pytest.param(
                "/api/v1/participants/{pid}/relations/{rid}/revoke",
                "revoke_relation",
                f"/api/v1/participants/{PATH_PARTICIPANT_ID}/relations/{RELATION_ID}/revoke",
                id="relation",
            ),
        ],
    )
    def test_missing_reason_is_rejected(
        self, harness: dict[str, Any], route: str, recorder: str, path: str
    ) -> None:
        assert harness["client"].post(path).status_code == 422
        assert harness[recorder].calls == []

    @pytest.mark.parametrize(
        ("recorder", "path"),
        [
            pytest.param(
                "revoke_link",
                f"/api/v1/participants/{PATH_PARTICIPANT_ID}/account-link/{LINK_ID}/revoke",
                id="account_link",
            ),
            pytest.param(
                "revoke_relation",
                f"/api/v1/participants/{PATH_PARTICIPANT_ID}/relations/{RELATION_ID}/revoke",
                id="relation",
            ),
        ],
    )
    def test_single_character_reason_is_accepted(
        self, harness: dict[str, Any], recorder: str, path: str
    ) -> None:
        """Boundary pair — the on-boundary value (length 1) must pass."""
        response = harness["client"].post(f"{path}?reason=r")

        assert response.status_code == 204
        assert harness[recorder].calls[0]["reason"] == "r"

    @pytest.mark.parametrize(
        ("recorder", "path", "id_kwarg"),
        [
            pytest.param(
                "revoke_link",
                f"/api/v1/participants/{PATH_PARTICIPANT_ID}/account-link/{LINK_ID}/revoke",
                "link_id",
                id="account_link",
            ),
            pytest.param(
                "revoke_relation",
                f"/api/v1/participants/{PATH_PARTICIPANT_ID}/relations/{RELATION_ID}/revoke",
                "relation_id",
                id="relation",
            ),
        ],
    )
    def test_reason_is_forwarded_to_the_application_layer(
        self,
        harness: dict[str, Any],
        recorder: str,
        path: str,
        id_kwarg: str,
    ) -> None:
        response = harness["client"].post(f"{path}?reason=guardian%20withdrew")

        assert response.status_code == 204
        assert harness[recorder].calls[0] == {
            id_kwarg: LINK_ID if id_kwarg == "link_id" else RELATION_ID,
            "reason": "guardian withdrew",
        }