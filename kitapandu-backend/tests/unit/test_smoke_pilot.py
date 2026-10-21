"""Unit tests for ``ksma.scripts.smoke_pilot``.

Seam: ``ksma.scripts.smoke_pilot`` — module constants, the
``SmokeRecorder`` artefact, the HTTP helpers (``_login``,
``_auth_headers``), and every ``step_*`` driver function. The smoke
pilot is designed to walk a running dev server; these unit tests pin
the recorder contract so a regression in recording logic surfaces
without requiring a live server.

Public surface under test:
- module constants ``BASE_URL``, ``SLUG_VANETA``
- ``SmokeRecorder`` (record + summary)
- ``_auth_headers(token)``
- ``_login(client, *, auth_subject, tenant_id, password=...)``
- ``step_01_login`` … ``step_09_audit``
- ``_async_main`` (orchestrator + failure-branch skip recorder)

Pure-Python; no DB / Docker / network. ``httpx.MockTransport`` intercepts
HTTP at the transport layer; the DB-bound ``_lookup`` is monkey-patched
out where ``step_01_login`` requires it.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest

from ksma.scripts import smoke_pilot as smoke_pilot_mod
from ksma.scripts.smoke_pilot import (
    BASE_URL,
    SLUG_VANETA,
    SmokeRecorder,
    _async_main,
    _auth_headers,
    _login,
    step_01_login,
    step_03_participants,
    step_04_batches,
    step_05_06_invoices,
    step_07_session,
    step_08_portal_inbox,
    step_09_audit,
)

# ---------------------------------------------------------------------------
# Settings fixture — seed required env vars so Settings() validates in tests
# that exercise _async_main (which calls get_settings()).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _seed_settings_env(monkeypatch):
    monkeypatch.setenv(
        "KITAPANDU_POSTGRES_DSN", "postgresql+asyncpg://x:x@localhost/x"
    )
    monkeypatch.setenv("KITAPANDU_POSTGRES_DSN_SYNC", "postgresql://x:x@localhost/x")
    monkeypatch.setenv("KITAPANDU_JWT_SECRET", "test-secret-not-used")
    monkeypatch.setenv("KITAPANDU_MINIO_ACCESS_KEY", "test-access-key")
    monkeypatch.setenv("KITAPANDU_MINIO_SECRET_KEY", "test-secret-key")
    # Defensive — ensure any cached Settings instance is dropped so the
    # patched env vars take effect.
    try:
        from ksma.settings import get_settings  # noqa: PLC0415

        get_settings.cache_clear()
    except Exception:
        pass
    yield
    try:
        from ksma.settings import get_settings  # noqa: PLC0415

        get_settings.cache_clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------


def mock_client(handler) -> httpx.AsyncClient:
    """Construct an ``httpx.AsyncClient`` backed by an ``httpx.MockTransport``.

    The returned client is *not yet entered*. For tests that call a function
    which owns the client's lifecycle (every ``step_*`` does), the test
    patches ``httpx.AsyncClient`` with ``return_value=mock_client(handler)``
    so the function-under-test enters the client itself.

    For tests that *do* own the client lifecycle (e.g. ``_login``), use
    ``async with mock_client(handler) as client:``.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )


# ---------------------------------------------------------------------------
# JWT construction helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    """Base64url-encode ``data`` and strip ``=`` padding (raw JWT segment)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _fake_jwt(
    *,
    payload: dict[str, Any] | None = None,
    header: dict[str, Any] | None = None,
    parts: int = 3,
) -> str:
    """Build a fake JWT-shaped string.

    - ``parts == 3`` returns ``"header.payload.sig"`` (conventional shape).
    - ``parts == 2`` returns ``"header.payload"`` (kills the ``>=`` mutation
      on ``len(parts) >= 2`` in ``_login``).
    - ``parts == 1`` returns only the payload segment; decoding is skipped.
    """
    header = header if header is not None else {"alg": "none", "typ": "JWT"}
    payload = payload if payload is not None else {"sub": "test"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    if parts == 1:
        return payload_b64
    if parts == 2:
        return f"{header_b64}.{payload_b64}"
    return f"{header_b64}.{payload_b64}.sig"


def _raw_claims(
    claims: dict[str, Any], *, force_length_mod4: int | None = None
) -> str:
    """Build a raw base64url claims segment.

    With ``force_length_mod4``, append "A" characters until the length is
    congruent to the desired value modulo 4. This exercises the various
    padding-arithmetic branches (``% 4`` returns 0, 1, 2, 3).
    """
    payload_bytes = json.dumps(claims, separators=(",", ":")).encode()
    encoded = _b64url(payload_bytes)
    if force_length_mod4 is None:
        return encoded
    pad = "A"
    while len(encoded) % 4 != force_length_mod4:
        encoded += pad
    return encoded


def _token_with_raw(raw_claims_segment: str, *, parts: int = 3) -> str:
    """Wrap a hand-crafted raw claims segment into a full JWT shape."""
    if parts == 1:
        return raw_claims_segment
    if parts == 2:
        return f"header.{raw_claims_segment}"
    return f"header.{raw_claims_segment}.sig"


def _ok_json(body: Any) -> httpx.Response:
    """Build a 200 response with a non-zero ``elapsed``.

    ``MockTransport`` does not auto-populate ``Response.elapsed``; the
    smoke-pilot orchestrator (``_async_main``) reads it on the health
    check, so we set it manually here.
    """
    r = httpx.Response(200, json=body)
    r._elapsed = datetime.timedelta(milliseconds=10)
    return r


# Pre-built tenant + ids fixtures


TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")


def _login_jwt(tenant_id: UUID = TENANT_ID, *, role: str = "AC") -> str:
    """Build a JWT that the smoke pilot considers 'good': current tenant matches
    and at least one membership.
    """
    return _fake_jwt(
        payload={
            "tz_current_tenant": str(tenant_id),
            "tz_memberships": [{"tenant_id": str(tenant_id), "role": role}],
        }
    )


def _login_token(role: str = "AC") -> str:
    """Build a syntactically valid 3-part JWT whose decoded claims
    contain a current-tenant matching ``TENANT_ID``. Used as a stable
    ``access_token`` string for the role-specific login steps, where
    the step's decoder produces an empty-claims fallback rather than
    failing the test on a malformed raw segment.
    """
    return _login_jwt(tenant_id=TENANT_ID, role=role)


# Login tokens — must be valid 3-part JWTs (header.payload.sig) so the
# step helpers' _login() decoder produces an empty claims dict rather
# than raising binascii.Error on a malformed raw segment.
AC_TOKEN = _login_token(role="AC")
FI_TOKEN = _login_token(role="FI")
IN_TOKEN = _login_token(role="IN")
ST_TOKEN = _login_token(role="ST")
AU_TOKEN = _login_token(role="AU")


def _settings_stub():
    """A minimal stub object — only ``postgres_dsn`` is read by ``_lookup``,
    which we patch out anyway in these tests.
    """

    class _Settings:
        postgres_dsn = "postgresql://stub@localhost/db"

    return _Settings()


def _make_ids(
    *, tenant_id: UUID | None = None, extra: dict | None = None
) -> dict[str, Any]:
    """Build a synthetic ``ids`` dict (the artefact returned by ``_lookup``)."""
    base = {
        "tenant_id": tenant_id or TENANT_ID,
        "batch_id": uuid4(),
        "batch_capacity": 10,
        "enrollments": [],
        "session_id": uuid4(),
        "inv1_id": uuid4(),
        "inv2_id": uuid4(),
        "payment_id": None,
        "expected_allocation_amount": 1_000_000,
        "accounts": {
            "ac": uuid4(),
            "fi": uuid4(),
            "in": uuid4(),
            "st": uuid4(),
            "au": uuid4(),
        },
    }
    if extra:
        base.update(extra)
    return base


def patched_lookup(return_value: dict[str, Any]):
    """Return an async function suitable for monkey-patching ``_lookup``."""

    async def _lookup(_settings):
        return return_value

    return _lookup


def _use_mock_client(handler):
    """Patch ``ksma.scripts.smoke_pilot.httpx.AsyncClient`` to return a
    pre-built ``MockTransport``-backed client. The step functions construct
    their own client via ``async with httpx.AsyncClient(...)``; this patch
    makes that construction return our mock client.
    """
    return patch.object(
        smoke_pilot_mod.httpx, "AsyncClient", return_value=mock_client(handler)
    )


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_base_url_defaults_to_localhost_8000(self) -> None:
        os.environ.pop("KSMA_SMOKE_BASE_URL", None)
        # Module-level binding is captured at import; just assert what it is.
        assert BASE_URL == "http://localhost:8000"

    def test_slug_vaneta_constant(self) -> None:
        assert SLUG_VANETA == "vaneta"


# ---------------------------------------------------------------------------
# SmokeRecorder — record() + summary()
# ---------------------------------------------------------------------------


class TestSmokeRecorderRecord:
    """Mutants in ``record`` (line 65):

    - ``delete not`` on ``if not ok and self.failing_step is None:``
    - ``and -> or`` on the same condition

    A passing record MUST NOT set ``failing_step``; a passing record
    after a failing one MUST NOT overwrite ``failing_step``.
    """

    def test_failing_step_is_none_on_construction(self) -> None:
        rec = SmokeRecorder()
        assert rec.failing_step is None
        assert rec.steps == []

    def test_pass_record_appends_tuple_and_does_not_set_failing(self) -> None:
        rec = SmokeRecorder()
        rec.record(1, "alpha", True)
        assert rec.steps == [(1, "alpha", True, "")]
        assert rec.failing_step is None

    def test_fail_record_sets_failing_step(self) -> None:
        rec = SmokeRecorder()
        rec.record(1, "alpha", False, "boom")
        assert rec.steps == [(1, "alpha", False, "boom")]
        assert rec.failing_step == 1

    def test_first_failing_step_is_remembered_after_subsequent_pass(
        self,
    ) -> None:
        """A passing record after a failing one MUST NOT overwrite
        ``failing_step``. The mutated ``or`` would always overwrite.
        """
        rec = SmokeRecorder()
        rec.record(1, "alpha", False, "boom")
        rec.record(2, "beta", True)
        rec.record(3, "gamma", True)
        assert rec.failing_step == 1
        assert rec.steps == [
            (1, "alpha", False, "boom"),
            (2, "beta", True, ""),
            (3, "gamma", True, ""),
        ]

    def test_only_first_failing_step_is_remembered_across_multiple_failures(
        self,
    ) -> None:
        rec = SmokeRecorder()
        rec.record(1, "alpha", False, "boom")
        rec.record(2, "beta", False, "also bad")
        rec.record(3, "gamma", False, "still bad")
        assert rec.failing_step == 1

    @pytest.mark.parametrize(
        "records,expected_failing",
        [
            ([(1, "a", True)], None),
            ([(1, "a", True), (2, "b", True), (3, "c", True)], None),
            ([(1, "a", False, "x")], 1),
            ([(1, "a", True), (2, "b", False, "x")], 2),
            ([(1, "a", False, "x"), (2, "b", True), (3, "c", False, "y")], 1),
        ],
    )
    def test_recorder_state_table(self, records, expected_failing) -> None:
        rec = SmokeRecorder()
        for n, name, ok, *rest in records:
            detail = rest[0] if rest else ""
            rec.record(n, name, ok, detail)
        assert rec.failing_step == expected_failing


class TestSmokeRecorderSummary:
    def test_summary_prints_pass_when_no_failures(self, capsys) -> None:
        rec = SmokeRecorder()
        rec.record(1, "a", True)
        rec.record(2, "b", True)
        rec.summary()
        captured = capsys.readouterr()
        assert "PILOT SMOKE PASS" in captured.out
        assert "PILOT SMOKE FAIL" not in captured.out

    def test_summary_prints_fail_step_when_any_failed(self, capsys) -> None:
        rec = SmokeRecorder()
        rec.record(1, "a", True)
        rec.record(2, "b", False, "boom")
        rec.summary()
        captured = capsys.readouterr()
        assert "PILOT SMOKE FAIL: step=2" in captured.out
        assert "PILOT SMOKE PASS" not in captured.out

    def test_status_string_includes_failure_details(self, capsys) -> None:
        rec = SmokeRecorder()
        rec.record(1, "alpha", True)
        rec.record(2, "beta", False, "kaboom")
        captured = capsys.readouterr()
        assert "[step 1: alpha] pass" in captured.out
        assert "[step 2: beta] fail: kaboom" in captured.out


# ---------------------------------------------------------------------------
# _auth_headers
# ---------------------------------------------------------------------------


class TestAuthHeaders:
    def test_bearer_header_format(self) -> None:
        assert _auth_headers("abc.def.ghi") == {
            "Authorization": "Bearer abc.def.ghi"
        }

    def test_empty_token_still_yields_bearer_prefix(self) -> None:
        assert _auth_headers("") == {"Authorization": "Bearer "}


# ---------------------------------------------------------------------------
# _login — JWT decoder operators
# ---------------------------------------------------------------------------


class TestLoginJwtDecoder:
    """Mutants in ``_login`` (lines 265, 266, 268):

    - ``len(parts) >= 2`` -> ``len(parts) > 2``
    - ``parts[1]`` -> ``parts[0]`` (header instead of payload)
    - padding-math mutations (``+`` -> ``-``, ``*`` -> ``/``, ``delete -``)
    """

    async def test_three_part_jwt_decodes_payload(self) -> None:
        claims = {
            "sub": "ac.vaneta",
            "tz_current_tenant": str(TENANT_ID),
            "tz_memberships": [{"tenant_id": str(TENANT_ID)}],
        }
        token = _fake_jwt(payload=claims, parts=3)

        async with mock_client(
            lambda req: _ok_json({"access_token": token})
        ) as client:
            access, decoded = await _login(
                client, auth_subject="ac.vaneta", tenant_id=TENANT_ID
            )

        assert access == token
        assert decoded["sub"] == "ac.vaneta"
        assert decoded["tz_current_tenant"] == str(TENANT_ID)
        assert decoded["tz_memberships"] == [
            {"tenant_id": str(TENANT_ID)}
        ]

    async def test_two_part_jwt_decodes_payload(self) -> None:
        """A 2-part token MUST still be decoded. Killing the ``>= 2`` mutation
        which becomes ``> 2`` and skips the decoder for the conventional
        2-part shape.
        """
        claims = {"sub": "two-part"}
        token = _fake_jwt(payload=claims, parts=2)

        async with mock_client(
            lambda req: _ok_json({"access_token": token})
        ) as client:
            access, decoded = await _login(
                client, auth_subject="ac.vaneta", tenant_id=TENANT_ID
            )

        assert access == token
        assert decoded.get("sub") == "two-part"

    async def test_one_part_jwt_returns_empty_claims(self) -> None:
        """A 1-part payload alone skips the decoder branch and yields
        ``claims = {}``. (Both original and ``> 2`` mutant agree here; the
        2-part case above distinguishes them.)
        """
        async with mock_client(
            lambda req: _ok_json({"access_token": "single-segment"})
        ) as client:
            access, decoded = await _login(
                client, auth_subject="ac.vaneta", tenant_id=TENANT_ID
            )

        assert access == "single-segment"
        assert decoded == {}

    async def test_three_part_jwt_uses_payload_not_header(self) -> None:
        """``_login`` MUST decode ``parts[1]`` (the payload), NOT
        ``parts[0]`` (the header). Killing the ``parts[1]`` -> ``parts[0]``
        mutation.
        """
        claims = {"unique_payload_marker": "I-AM-PAYLOAD"}
        header = {"unique_header_marker": "I-AM-HEADER"}
        token = _fake_jwt(payload=claims, header=header, parts=3)

        async with mock_client(
            lambda req: _ok_json({"access_token": token})
        ) as client:
            _access, decoded = await _login(
                client, auth_subject="ac.vaneta", tenant_id=TENANT_ID
            )

        assert decoded.get("unique_payload_marker") == "I-AM-PAYLOAD"
        assert "unique_header_marker" not in decoded

    async def test_padding_logic_works_for_modulo_3_raw(self) -> None:
        """``_login`` uses ``padded = raw + "=" * (-len(raw) % 4)``.

        A 2-byte JSON payload (``"42"``) encodes to a raw segment of
        length 3, which is congruent to 3 modulo 4 and therefore requires
        1 padding character. The mutants
        ``+ -> -`` and ``* -> /`` both raise TypeError at this line; any
        ``_login`` test that decodes a non-trivial token therefore kills
        those mutations.

        The ``delete -`` mutation in this codebase is equivalent under
        Python's lenient ``urlsafe_b64decode`` (the function silently
        auto-pads to a multiple of 4), so it is documented here as
        behaviour without being killed.
        """
        raw = base64.urlsafe_b64encode(b"42").rstrip(b"=").decode()
        assert len(raw) == 3 and len(raw) % 4 == 3
        token = _token_with_raw(raw, parts=3)

        async with mock_client(
            lambda req: _ok_json({"access_token": token})
        ) as client:
            _access, decoded = await _login(
                client, auth_subject="ac.vaneta", tenant_id=TENANT_ID
            )

        assert decoded == 42  # JSON parses "42" as the integer 42

    async def test_malformed_json_payload_yields_empty_claims(self) -> None:
        """If the payload is not valid JSON the decoder returns ``{}``."""
        raw = _b64url(b"not json {{{")
        token = _token_with_raw(raw, parts=3)

        async with mock_client(
            lambda req: _ok_json({"access_token": token})
        ) as client:
            _access, decoded = await _login(
                client, auth_subject="ac.vaneta", tenant_id=TENANT_ID
            )

        assert decoded == {}


# ---------------------------------------------------------------------------
# step_01_login
# ---------------------------------------------------------------------------


class TestStep01Login:
    """Mutants in ``step_01_login`` (lines 287-340):

    - step-number 1 -> 0 (lines 298, 309, 313, 316)
    - step-number 2 -> 0 (line 338)
    - ``if not access`` -> ``if access`` (line 312)
    - ``or -> and`` on memberships (line 319)
    - ``str(tenant_id) != current_tenant`` (line 321)
    - ``delete not`` on ``if not memberships`` (line 329)
    """

    async def test_happy_path_records_steps_1_and_2_pass(self) -> None:
        rec = SmokeRecorder()
        token = _login_jwt(TENANT_ID)

        with _use_mock_client(
            lambda req: _ok_json({"access_token": token})
        ), patch(
            "ksma.scripts.smoke_pilot._lookup",
            new=patched_lookup(_make_ids(tenant_id=TENANT_ID)),
        ):
            ids = await step_01_login(rec, _settings_stub())

        assert ids is not None
        assert rec.steps == [
            (1, "login as ac@vaneta.id", True, ""),
            (2, "JWT carries Vaneta DEMO membership", True, ""),
        ]
        assert ids["ac_token"] == token

    async def test_lookup_failure_records_step_1_with_db_error(self) -> None:
        rec = SmokeRecorder()

        async def boom(_settings):
            raise RuntimeError("kaboom db")

        with patch("ksma.scripts.smoke_pilot._lookup", new=boom):
            ids = await step_01_login(rec, _settings_stub())

        assert ids is None
        assert len(rec.steps) == 1
        n, _name, ok, detail = rec.steps[0]
        assert n == 1
        assert ok is False
        assert "DB error" in detail

    async def test_login_http_error_records_step_1_with_login_failed(self) -> None:
        rec = SmokeRecorder()

        with _use_mock_client(
            lambda req: httpx.Response(500, text="server dead")
        ), patch(
            "ksma.scripts.smoke_pilot._lookup",
            new=patched_lookup(_make_ids(tenant_id=TENANT_ID)),
        ):
            ids = await step_01_login(rec, _settings_stub())

        assert ids is None
        assert len(rec.steps) == 1
        n, _name, ok, detail = rec.steps[0]
        assert n == 1
        assert ok is False
        assert "login failed" in detail

    async def test_empty_access_token_records_login_failed(self) -> None:
        """When the server returns a body without ``access_token`` the code
        raises ``KeyError`` (caught by ``except Exception``) and records a
        failure with the ``login failed`` detail string.
        """
        rec = SmokeRecorder()

        with _use_mock_client(
            lambda req: _ok_json({"no_token_here": "x"})
        ), patch(
            "ksma.scripts.smoke_pilot._lookup",
            new=patched_lookup(_make_ids(tenant_id=TENANT_ID)),
        ):
            ids = await step_01_login(rec, _settings_stub())

        assert ids is None
        assert len(rec.steps) == 1
        n, _name, ok, detail = rec.steps[0]
        assert n == 1
        assert ok is False
        assert "login failed" in detail

    async def test_jwt_tenant_mismatch_records_step_2_failure(self) -> None:
        rec = SmokeRecorder()
        # JWT claims a different tenant than _lookup returned.
        token = _login_jwt(OTHER_TENANT)

        with _use_mock_client(
            lambda req: _ok_json({"access_token": token})
        ), patch(
            "ksma.scripts.smoke_pilot._lookup",
            new=patched_lookup(_make_ids(tenant_id=TENANT_ID)),
        ):
            ids = await step_01_login(rec, _settings_stub())

        assert ids is None
        assert len(rec.steps) == 2
        assert rec.steps[0] == (1, "login as ac@vaneta.id", True, "")
        n, _name, ok, detail = rec.steps[1]
        assert n == 2
        assert ok is False
        assert "tz_current_tenant" in detail
        assert str(OTHER_TENANT) in detail

    async def test_jwt_with_empty_memberships_records_step_2_failure(self) -> None:
        rec = SmokeRecorder()
        # JWT has the right tenant BUT no memberships.
        token = _fake_jwt(
            payload={
                "tz_current_tenant": str(TENANT_ID),
                "tz_memberships": [],
            }
        )

        with _use_mock_client(
            lambda req: _ok_json({"access_token": token})
        ), patch(
            "ksma.scripts.smoke_pilot._lookup",
            new=patched_lookup(_make_ids(tenant_id=TENANT_ID)),
        ):
            ids = await step_01_login(rec, _settings_stub())

        assert ids is None
        assert len(rec.steps) == 2
        assert rec.steps[0] == (1, "login as ac@vaneta.id", True, "")
        n, _name, ok, detail = rec.steps[1]
        assert n == 2
        assert ok is False
        assert "tz_memberships is empty" in detail


# ---------------------------------------------------------------------------
# step_03_participants
# ---------------------------------------------------------------------------


class TestStep03Participants:
    """Mutants in step_03 (lines 349-388):

    - step-number 3 -> 0
    - ``response.status_code != 200`` (line 361)
    - ``or -> and`` on ``items = body.get("items") or []`` (line 370)
    - ``or -> and`` on ``suspects = body.get(...)`` (line 381)
    - ``dup_ok = True`` -> ``dup_ok = False`` (line 373)
    - ``dup_ok = False`` -> ``dup_ok = True`` (line 383)
    - ``delete not`` on ``if not suspects`` (line 382)
    """

    async def test_no_ac_token_records_failure(self) -> None:
        rec = SmokeRecorder()
        await step_03_participants(rec, _make_ids())
        assert len(rec.steps) == 1
        n, _name, ok, detail = rec.steps[0]
        assert n == 3
        assert ok is False
        assert "no AC token" in detail

    async def test_http_error_status_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["ac_token"] = AC_TOKEN

        with _use_mock_client(
            lambda req: httpx.Response(403, text="forbidden")
        ):
            await step_03_participants(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 3
        assert ok is False
        assert "status=403" in detail

    async def test_items_missing_p0006_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["ac_token"] = AC_TOKEN

        body = {
            "items": [{"participant_no": "P-0001"}],
            "duplicate_suspects": [],
        }

        with _use_mock_client(lambda req: _ok_json(body)):
            await step_03_participants(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 3
        assert ok is False
        assert "P-0001+P-0006" in detail
        assert "P-0006" in detail

    async def test_items_complete_but_suspects_empty_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["ac_token"] = AC_TOKEN

        body = {
            "items": [
                {"participant_no": "P-0001"},
                {"participant_no": "P-0006"},
            ],
            "duplicate_suspects": [],
        }

        with _use_mock_client(lambda req: _ok_json(body)):
            await step_03_participants(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 3
        assert ok is False
        assert "duplicate_suspects is empty" in detail

    async def test_happy_path_records_pass(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["ac_token"] = AC_TOKEN

        body = {
            "items": [
                {"participant_no": "P-0001"},
                {"participant_no": "P-0006"},
            ],
            "duplicate_suspects": [{"participant_no": "P-0001"}],
        }

        with _use_mock_client(lambda req: _ok_json(body)):
            await step_03_participants(rec, ids)

        assert rec.steps == [
            (3, "participants duplicate-suspect (Sari)", True, ""),
        ]
        assert rec.failing_step is None


# ---------------------------------------------------------------------------
# step_04_batches
# ---------------------------------------------------------------------------


class TestStep04Batches:
    """``step_04_batches`` GETs ``/api/v1/batches`` and asserts
    ``EN-A1-Okt2026`` has capacity 10 + active_enrollments 2.
    """

    async def test_no_ac_token_records_failure(self) -> None:
        rec = SmokeRecorder()
        await step_04_batches(rec, _make_ids())
        n, _name, ok, detail = rec.steps[0]
        assert n == 4
        assert ok is False
        assert "no AC token" in detail

    async def test_batch_not_found_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["ac_token"] = AC_TOKEN

        with _use_mock_client(lambda req: _ok_json([{"batch_code": "OTHER"}])):
            await step_04_batches(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 4
        assert ok is False
        assert "EN-A1-Okt2026 not in" in detail

    @pytest.mark.parametrize(
        "body, expected_detail_substr",
        [
            (
                [{"batch_code": "EN-A1-Okt2026", "capacity": 5,
                  "active_enrollments": 2}],
                "capacity=5",
            ),
            (
                [{"batch_code": "EN-A1-Okt2026", "capacity": 10,
                  "active_enrollments": 0}],
                "active_enrollments=0",
            ),
        ],
    )
    async def test_invariant_violation_records_failure(
        self, body, expected_detail_substr
    ) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["ac_token"] = AC_TOKEN

        with _use_mock_client(lambda req: _ok_json(body)):
            await step_04_batches(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 4
        assert ok is False
        assert expected_detail_substr in detail

    async def test_http_error_status_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["ac_token"] = AC_TOKEN

        with _use_mock_client(lambda req: httpx.Response(500, text="boom")):
            await step_04_batches(rec, ids)

        _n, _name, ok, detail = rec.steps[0]
        assert ok is False
        assert "status=500" in detail

    async def test_happy_path_records_pass(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["ac_token"] = AC_TOKEN

        body = [{
            "batch_code": "EN-A1-Okt2026",
            "capacity": 10,
            "active_enrollments": 2,
        }]

        with _use_mock_client(lambda req: _ok_json(body)):
            await step_04_batches(rec, ids)

        assert rec.steps == [
            (4, "batch EN-A1-Okt2026 capacity/occupied", True, ""),
        ]
        assert rec.failing_step is None


# ---------------------------------------------------------------------------
# step_05_06_invoices
# ---------------------------------------------------------------------------


class TestStep0506Invoices:
    """``step_05_06_invoices`` logs in as FI then GETs
    ``/api/v1/invoices/{id}/status`` for both invoices, asserting paid /
    unpaid respectively.
    """

    async def test_fi_login_failure_records_steps_5_and_6_failed(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        with _use_mock_client(
            lambda req: httpx.Response(401, text="no auth")
        ):
            await step_05_06_invoices(rec, ids)

        assert len(rec.steps) == 2
        s5, s6 = rec.steps
        assert s5[0] == 5 and s5[2] is False and "login" in s5[3]
        assert s6[0] == 6 and s6[2] is False
        assert rec.failing_step == 5

    async def test_first_invoice_payment_status_mismatch_fails(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            return _ok_json({
                "payment_status": "unpaid",  # wrong
                "overdue_amount": 0,
                "active_allocation_total": 0,
            })

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 5
        assert ok is False
        assert "payment_status" in detail

    async def test_first_invoice_overdue_amount_nonzero_fails(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["expected_allocation_amount"] = 1_000_000

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            return _ok_json({
                "payment_status": "paid",
                "overdue_amount": 50_000,
                "active_allocation_total": 1_000_000,
            })

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 5
        assert ok is False
        assert "overdue_amount" in detail

    async def test_first_invoice_allocation_total_mismatch_fails(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["expected_allocation_amount"] = 1_000_000

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            return _ok_json({
                "payment_status": "paid",
                "overdue_amount": 0,
                "active_allocation_total": 999,
            })

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 5
        assert ok is False
        assert "active_allocation_total" in detail

    async def test_first_invoice_happy_path_records_step_5_pass(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["expected_allocation_amount"] = 1_000_000

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            if "/invoices/" in req.url.path and req.url.path.endswith(
                "/status"
            ):
                inv_id = req.url.path.split("/invoices/")[1].split("/")[0]
                if str(inv_id) == str(ids["inv1_id"]):
                    return _ok_json({
                        "payment_status": "paid",
                        "overdue_amount": 0,
                        "active_allocation_total": 1_000_000,
                    })
                return _ok_json({
                    "payment_status": "unpaid",
                    "overdue_amount": 0,
                    "active_allocation_total": 0,
                })
            return httpx.Response(404)

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        assert len(rec.steps) == 2
        s5, s6 = rec.steps
        assert s5[0] == 5 and s5[2] is True
        assert s6[0] == 6 and s6[2] is True
        assert rec.failing_step is None


# ---------------------------------------------------------------------------
# step_07_session
# ---------------------------------------------------------------------------


class TestStep07Session:
    """``step_07_session`` logs in as IN and asserts the first session has an
    ``effective_revision_id`` plus a non-empty attendance list.
    """

    async def test_in_login_failure_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        with _use_mock_client(
            lambda req: httpx.Response(500, text="boom")
        ):
            await step_07_session(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 7
        assert ok is False
        assert "login" in detail

    async def test_session_get_fails_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": IN_TOKEN})
            if req.url.path.endswith("/attendance"):
                return _ok_json([{"participant_no": "P-0001"}])
            return httpx.Response(500, text="boom")

        with _use_mock_client(handler):
            await step_07_session(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 7
        assert ok is False
        assert "status=500" in detail

    async def test_missing_effective_revision_id_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": IN_TOKEN})
            if req.url.path.endswith("/attendance"):
                return _ok_json([{"participant_no": "P-0001"}])
            return _ok_json({"session_no": 1, "starts_at": "2026-01-01"})

        with _use_mock_client(handler):
            await step_07_session(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 7
        assert ok is False
        assert "effective_revision_id missing" in detail

    async def test_empty_attendance_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": IN_TOKEN})
            if req.url.path.endswith("/attendance"):
                return _ok_json([])
            return _ok_json({"effective_revision_id": str(uuid4())})

        with _use_mock_client(handler):
            await step_07_session(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 7
        assert ok is False
        assert "attendance rows=0" in detail

    async def test_attendance_http_error_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": IN_TOKEN})
            if req.url.path.endswith("/attendance"):
                return httpx.Response(500, text="boom")
            return _ok_json({"effective_revision_id": str(uuid4())})

        with _use_mock_client(handler):
            await step_07_session(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 7
        assert ok is False
        assert "attendance" in detail

    async def test_happy_path_records_pass(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": IN_TOKEN})
            if req.url.path.endswith("/attendance"):
                return _ok_json([{"participant_no": "P-0001"}])
            return _ok_json({"effective_revision_id": str(uuid4())})

        with _use_mock_client(handler):
            await step_07_session(rec, ids)

        assert rec.steps == [
            (7, "session first effective+attendance", True, ""),
        ]
        assert rec.failing_step is None


# ---------------------------------------------------------------------------
# step_08_portal_inbox
# ---------------------------------------------------------------------------


class TestStep08PortalInbox:
    async def test_st_login_failure_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        with _use_mock_client(
            lambda req: httpx.Response(500, text="boom")
        ):
            await step_08_portal_inbox(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 8
        assert ok is False
        assert "login" in detail

    async def test_empty_inbox_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": ST_TOKEN})
            return _ok_json([])

        with _use_mock_client(handler):
            await step_08_portal_inbox(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 8
        assert ok is False
        assert "inbox is empty" in detail

    async def test_http_error_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": ST_TOKEN})
            return httpx.Response(500, text="boom")

        with _use_mock_client(handler):
            await step_08_portal_inbox(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 8
        assert ok is False
        assert "status=500" in detail

    async def test_happy_path_records_pass(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": ST_TOKEN})
            return _ok_json(
                [{"id": str(uuid4()), "kind": "PaymentVerified"}]
            )

        with _use_mock_client(handler):
            await step_08_portal_inbox(rec, ids)

        assert rec.steps == [
            (8, "portal/inbox for st@vaneta.id", True, ""),
        ]
        assert rec.failing_step is None


# ---------------------------------------------------------------------------
# step_09_audit
# ---------------------------------------------------------------------------


class TestStep09Audit:
    async def test_au_login_failure_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        with _use_mock_client(
            lambda req: httpx.Response(500, text="boom")
        ):
            await step_09_audit(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 9
        assert ok is False
        assert "login" in detail

    async def test_empty_audit_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": AU_TOKEN})
            return _ok_json([])

        with _use_mock_client(handler):
            await step_09_audit(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 9
        assert ok is False
        assert "audit list is empty" in detail

    async def test_http_error_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": AU_TOKEN})
            return httpx.Response(500, text="boom")

        with _use_mock_client(handler):
            await step_09_audit(rec, ids)

        n, _name, ok, detail = rec.steps[0]
        assert n == 9
        assert ok is False
        assert "status=500" in detail

    async def test_happy_path_records_pass(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": AU_TOKEN})
            return _ok_json(
                [{"action_id": "FIN-A05", "object_kind": "FIN"}]
            )

        with _use_mock_client(handler):
            await step_09_audit(rec, ids)

        assert rec.steps == [
            (9, "audit FIN seeded allocation", True, ""),
        ]
        assert rec.failing_step is None


# ---------------------------------------------------------------------------
# _async_main — orchestrator
# ---------------------------------------------------------------------------


class TestAsyncMain:
    """Mutants in ``_async_main`` (lines 691-728):

    - line 691 ``h.status_code != 200``
    - line 697 ``* -> /`` on elapsed scaling
    - line 713 ``+ -> -`` on the step-name concatenation
    - line 715 ``False -> True`` on the skip-record
    - line 717 ``return 1`` -> ``return 0``
    - line 727 ``- -> +`` on elapsed subtraction
    - line 728 ``0 if ... else 1`` flips
    """

    async def test_health_check_non_200_exits_2_no_steps(self) -> None:
        async def fake_step_01(*args, **kwargs):
            raise AssertionError(
                "step_01_login must NOT be invoked when health=500"
            )

        with _use_mock_client(
            lambda req: httpx.Response(500, text="server sad")
        ), patch(
            "ksma.scripts.smoke_pilot.step_01_login", side_effect=fake_step_01
        ):
            result = await _async_main()

        assert result == 2

    async def test_failure_branch_records_steps_3_to_9_as_skipped(self) -> None:
        captured: dict[str, Any] = {}

        async def fake_step_01(*args, **kwargs):
            captured["called"] = True

        # Capture the SmokeRecorder instance _async_main creates.
        original_SmokeRecorder = smoke_pilot_mod.SmokeRecorder
        captured_recorders: list[SmokeRecorder] = []

        def recording_Recorder():
            r = original_SmokeRecorder()
            captured_recorders.append(r)
            return r

        with _use_mock_client(
            lambda req: _ok_json({"status": "ok"})
        ), patch(
            "ksma.scripts.smoke_pilot.step_01_login", side_effect=fake_step_01
        ), patch(
            "ksma.scripts.smoke_pilot.SmokeRecorder", side_effect=recording_Recorder
        ):
            result = await _async_main()

        assert result == 1
        assert captured["called"] is True
        assert len(captured_recorders) == 1
        rec = captured_recorders[0]
        step_nums = [n for (n, _, _, _) in rec.steps]
        assert step_nums == [3, 4, 5, 6, 7, 8, 9]
        labels = [name for (_n, name, _ok, _detail) in rec.steps]
        for label in labels:
            assert label.endswith(" (skipped, no seed)"), label
        for (_n, _name, ok, detail) in rec.steps:
            assert ok is False
            assert detail == "skipped due to earlier failure"
        assert rec.failing_step == 3


# ===========================================================================
# T19 — kill the 22 SURVIVED mutations in the smoke pilot driver
#
# Survivor map (``.metrics/mutator.txt`` @ integration tip 59e7172):
#
#   :87  True -> False           ``pool_pre_ping=True`` on the lookup engine
#   :121 0 -> 1                  account key is the role prefix, not the suffix
#   :220 or -> and / 0 -> 1      ``int(total or 0)`` COALESCE over the SUM
#   :268 delete -                base64 padding sign — provably equivalent
#                                (pinned by ``test_padding_sign_is_unobservable``)
#   :313 1 -> 0 / False -> True  empty ``access_token`` records step 1 FAILED
#   :359 False -> True           step_03 transport-error record
#   :376 False -> True           ``dup_ok = False`` when P-0006 is absent
#   :404 False -> True           step_04 transport-error record
#   :476 False -> True           step_05/06 invoice transport-error record
#   :484 False -> True           step_05/06 invoice non-200 record
#   :498 0 -> 1                  ``overdue_amount`` trips at 1, not at 0
#   :512 0 -> 1                  ``active_allocation_total`` default is 0
#   :549 False -> True           step_07 session transport-error record
#   :575 False -> True           step_07 attendance transport-error record
#   :616 False -> True           step_08 inbox transport-error record
#   :654 False -> True           step_09 audit transport-error record
#   :697 * -> /                  health-check elapsed is scaled to milliseconds
#   :727 - -> +                  elapsed is ``now - started``
#   :728 0 -> 1 / 1 -> 0         exit code tracks ``rec.failing_step``
# ===========================================================================


# ---------------------------------------------------------------------------
# In-memory asyncpg double for the DB-bound ``_lookup``
#
# ``_lookup`` is the only DB seam in the module. Rather than stand up a
# real engine (integration test), we hand ``create_async_engine`` a fake
# that replays the ten ``execute`` calls in order and records the
# constructor kwargs — that is where the ``pool_pre_ping=True`` contract
# lives.
# ---------------------------------------------------------------------------


class _FakeResult:
    """The subset of SQLAlchemy's ``Result`` that ``_lookup`` consumes."""

    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeBegin:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return False


class _FakeConn:
    def __init__(self, results):
        self._results = list(results)
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return self._results.pop(0) if self._results else _FakeResult([])


class _FakeEngine:
    def __init__(self, results):
        self.conn = _FakeConn(results)
        self.disposed = False

    def begin(self):
        return _FakeBegin(self.conn)

    async def dispose(self):
        self.disposed = True


# Canonical UUIDs for the seeded Vaneta DEMO graph.
DB_TENANT_ID = UUID("aaaaaaaa-0000-4000-8000-000000000001")
DB_AC_ACCOUNT = UUID("aaaaaaaa-0000-4000-8000-000000000002")
DB_FI_ACCOUNT = UUID("aaaaaaaa-0000-4000-8000-000000000003")
DB_BATCH_ID = UUID("aaaaaaaa-0000-4000-8000-000000000004")
DB_ENROLLMENT_ID = UUID("aaaaaaaa-0000-4000-8000-000000000005")
DB_PARTICIPANT_ID = UUID("aaaaaaaa-0000-4000-8000-000000000006")
DB_SESSION_ID = UUID("aaaaaaaa-0000-4000-8000-000000000007")
DB_INV1_ID = UUID("aaaaaaaa-0000-4000-8000-000000000008")
DB_INV2_ID = UUID("aaaaaaaa-0000-4000-8000-000000000009")
DB_PAYMENT_ID = UUID("aaaaaaaa-0000-4000-8000-00000000000a")


def _lookup_results(*, alloc_total):
    """The ten ``execute`` results ``_lookup`` consumes, in call order."""
    ns = SimpleNamespace
    return [
        _FakeResult([ns(id=DB_TENANT_ID)]),  # 1. tenant
        _FakeResult([]),  # 2. SET LOCAL app.bypass_rls
        _FakeResult(  # 3. accounts (one per role)
            [
                ns(id=DB_AC_ACCOUNT, auth_subject=f"ac.{SLUG_VANETA}"),
                ns(id=DB_FI_ACCOUNT, auth_subject=f"fi.{SLUG_VANETA}"),
            ]
        ),
        _FakeResult([ns(id=DB_BATCH_ID, capacity=10)]),  # 4. batch
        _FakeResult(  # 5. active enrollments
            [ns(id=DB_ENROLLMENT_ID, participant_id=DB_PARTICIPANT_ID)]
        ),
        _FakeResult([ns(id=DB_SESSION_ID)]),  # 6. first session
        _FakeResult([ns(id=DB_INV1_ID)]),  # 7. INV-VANETA-0001
        _FakeResult([ns(id=DB_INV2_ID)]),  # 8. INV-VANETA-0002
        _FakeResult(  # 9. verified payment for invoice 1
            [ns(id=DB_PAYMENT_ID, recognized_amount=1_000_000)]
        ),
        _FakeResult([ns(total=alloc_total)]),  # 10. allocation SUM
    ]


def _install_fake_engine(monkeypatch, *, alloc_total):
    """Patch ``create_async_engine``; return the recorded constructor calls."""
    calls: list[tuple[str, dict[str, Any], _FakeEngine]] = []

    def _create(dsn, **kwargs):
        engine = _FakeEngine(_lookup_results(alloc_total=alloc_total))
        calls.append((dsn, kwargs, engine))
        return engine

    monkeypatch.setattr(smoke_pilot_mod, "create_async_engine", _create)
    return calls


class TestLookupDatabaseMapping:
    """``_lookup`` turns DB rows into the ``ids`` artefact every step consumes."""

    @pytest.mark.parametrize(
        "alloc_total, expected_amount",
        [
            (None, 0),  # COALESCE: NULL SUM -> 0
            (1_000_000, 1_000_000),  # real SUM survives the `or` short-circuit
        ],
    )
    async def test_lookup_maps_rows_into_ids(
        self, monkeypatch, alloc_total, expected_amount
    ) -> None:
        calls = _install_fake_engine(monkeypatch, alloc_total=alloc_total)

        ids = await smoke_pilot_mod._lookup(_settings_stub())

        assert len(calls) == 1
        dsn, kwargs, engine = calls[0]
        # ``pool_pre_ping=True`` — a stale pooled connection would surface
        # as a phantom "dev server unreachable" during the smoke run.
        assert dsn == "postgresql://stub@localhost/db"
        assert kwargs["pool_pre_ping"] is True
        assert engine.disposed is True

        assert ids == {
            "tenant_id": DB_TENANT_ID,
            "batch_id": DB_BATCH_ID,
            "batch_capacity": 10,
            "enrollments": [(DB_ENROLLMENT_ID, DB_PARTICIPANT_ID)],
            "session_id": DB_SESSION_ID,
            "inv1_id": DB_INV1_ID,
            "inv2_id": DB_INV2_ID,
            "payment_id": DB_PAYMENT_ID,
            "expected_allocation_amount": expected_amount,
            # Keys are the role prefix ("ac"), NOT the slug suffix ("vaneta").
            "accounts": {"ac": DB_AC_ACCOUNT, "fi": DB_FI_ACCOUNT},
        }

    async def test_lookup_reads_past_rls_for_the_id_graph(
        self, monkeypatch
    ) -> None:
        """The lookup must disable RLS before the account/enrollment reads."""
        calls = _install_fake_engine(monkeypatch, alloc_total=0)
        await smoke_pilot_mod._lookup(_settings_stub())

        statements = calls[0][2].conn.statements
        bypass = [s for s in statements if "bypass_rls" in s]
        assert len(bypass) == 1
        assert statements.index(bypass[0]) == 1  # immediately after tenant lookup

    async def test_lookup_optional_verified_payment_absent_is_none(
        self, monkeypatch
    ) -> None:
        """No verified payment row -> ``payment_id`` is None, not a crash."""

        def _create(dsn, **kwargs):
            results = _lookup_results(alloc_total=0)
            results[8] = _FakeResult([])  # no verified_payment row
            engine = _FakeEngine(results)
            calls.append(engine)
            return engine

        calls: list[_FakeEngine] = []
        monkeypatch.setattr(smoke_pilot_mod, "create_async_engine", _create)

        ids = await smoke_pilot_mod._lookup(_settings_stub())
        assert ids["payment_id"] is None
        assert ids["expected_allocation_amount"] == 0


# ---------------------------------------------------------------------------
# step_01_login — empty access_token
# ---------------------------------------------------------------------------


class TestStep01EmptyToken:
    """``if not access:`` must record step **1** as FAILED, not as 0 or pass."""

    async def test_empty_access_token_records_step_1_failed(self) -> None:
        rec = SmokeRecorder()

        with patch(
            "ksma.scripts.smoke_pilot._lookup",
            new=patched_lookup(_make_ids(tenant_id=TENANT_ID)),
        ), _use_mock_client(lambda req: _ok_json({"access_token": ""})):
            result = await step_01_login(rec, _settings_stub())

        assert result is None
        assert rec.steps == [
            (1, "login as ac@vaneta.id", False, "no access_token returned")
        ]
        assert rec.failing_step == 1


# ---------------------------------------------------------------------------
# Transport-error records — one per step helper
# ---------------------------------------------------------------------------


def _connection_refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


class TestTransportErrorRecords:
    """Every ``except Exception`` branch must record ``ok=False``.

    The ``False -> True`` mutants on those ``rec.record`` calls are killed
    by asserting the full recorded tuple, not merely the detail string.
    """

    async def test_step_03_transport_error_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids(extra={"ac_token": AC_TOKEN})

        with _use_mock_client(_connection_refused):
            await step_03_participants(rec, ids)

        assert rec.steps == [
            (3, "participants duplicate-suspect", False, "http: connection refused")
        ]
        assert rec.failing_step == 3

    async def test_step_04_transport_error_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids(extra={"ac_token": AC_TOKEN})

        with _use_mock_client(_connection_refused):
            await step_04_batches(rec, ids)

        assert rec.steps == [
            (4, "batch EN-A1-Okt2026 capacity/occupied", False, "http: connection refused")
        ]
        assert rec.failing_step == 4

    async def test_step_05_06_transport_error_records_both_failed(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            raise httpx.ConnectError("invoice service down")

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        assert rec.steps == [
            (
                5,
                f"invoice {ids['inv1_id']} status=paid",
                False,
                "http: invoice service down",
            ),
            (
                6,
                f"invoice {ids['inv2_id']} status=unpaid",
                False,
                "http: invoice service down",
            ),
        ]
        assert rec.failing_step == 5

    async def test_step_07_session_transport_error_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": IN_TOKEN})
            raise httpx.ConnectError("session service down")

        with _use_mock_client(handler):
            await step_07_session(rec, ids)

        assert rec.steps == [
            (
                7,
                "session first effective+attendance",
                False,
                "http: session service down",
            )
        ]
        assert rec.failing_step == 7

    async def test_step_07_attendance_transport_error_records_failure(self) -> None:
        """The attendance GET has its own ``except`` — it must also fail."""
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": IN_TOKEN})
            if req.url.path.endswith("/attendance"):
                raise httpx.ConnectError("attendance service down")
            return _ok_json({"effective_revision_id": "rev-1"})

        with _use_mock_client(handler):
            await step_07_session(rec, ids)

        assert rec.steps == [
            (
                7,
                "session first effective+attendance",
                False,
                "http: attendance service down",
            )
        ]
        assert rec.failing_step == 7

    async def test_step_08_transport_error_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": ST_TOKEN})
            raise httpx.ConnectError("portal service down")

        with _use_mock_client(handler):
            await step_08_portal_inbox(rec, ids)

        assert rec.steps == [
            (8, "portal/inbox for st@vaneta.id", False, "http: portal service down")
        ]
        assert rec.failing_step == 8

    async def test_step_09_transport_error_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": AU_TOKEN})
            raise httpx.ConnectError("audit service down")

        with _use_mock_client(handler):
            await step_09_audit(rec, ids)

        assert rec.steps == [
            (9, "audit FIN seeded allocation", False, "http: audit service down")
        ]
        assert rec.failing_step == 9


# ---------------------------------------------------------------------------
# Boundary assertions (the mutation-sensitive comparisons)
# ---------------------------------------------------------------------------


class TestStep03DuplicateSuspectBoundary:
    """``dup_ok = False`` (line 376) is only observable when the response
    still carries a non-empty ``duplicate_suspects`` list — otherwise the
    ``if not suspects`` guard repairs the mutant.
    """

    async def test_missing_p0006_with_suspects_present_records_failure(self) -> None:
        rec = SmokeRecorder()
        ids = _make_ids(extra={"ac_token": AC_TOKEN})

        body = {
            "items": [{"participant_no": "P-0001"}],
            "duplicate_suspects": [{"participant_no": "P-0001"}],
        }

        with _use_mock_client(lambda req: _ok_json(body)):
            await step_03_participants(rec, ids)

        assert rec.steps == [
            (
                3,
                "participants duplicate-suspect (Sari)",
                False,
                "items had ['P-0001'], expected P-0001+P-0006",
            )
        ]
        assert rec.failing_step == 3


class TestStep0506Boundaries:
    """``overdue_amount`` and ``active_allocation_total`` sit on the exact
    boundaries 0 and 1 — the ``0 -> 1`` mutants survive any probe that only
    uses "obviously wrong" values such as 999 or 50_000.
    """

    async def test_overdue_amount_of_one_rupiah_fails(self) -> None:
        """``overdue_amount == 1`` is overdue; the mutant's ``!= 1`` passes."""
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            return _ok_json({
                "payment_status": "paid",
                "overdue_amount": 1,
                "active_allocation_total": 1_000_000,
            })

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        assert rec.steps[0] == (
            5,
            f"invoice {ids['inv1_id']} status=paid",
            False,
            "overdue_amount=1",
        )
        assert rec.failing_step == 5

    async def test_missing_allocation_total_defaults_to_zero(self) -> None:
        """An absent ``active_allocation_total`` means 0, not 1."""
        rec = SmokeRecorder()
        ids = _make_ids()
        ids["expected_allocation_amount"] = 1

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            return _ok_json({
                "payment_status": "paid",
                "overdue_amount": 0,
            })

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        assert rec.steps[0] == (
            5,
            f"invoice {ids['inv1_id']} status=paid",
            False,
            "active_allocation_total=0 != expected 1",
        )
        assert rec.failing_step == 5

    async def test_missing_overdue_amount_defaults_to_zero(self) -> None:
        """An absent ``overdue_amount`` is 0, not 1 — the projection simply
        omits the key for a clean invoice.
        """
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            if req.url.path.endswith(str(ids["inv1_id"]) + "/status"):
                return _ok_json({
                    "payment_status": "paid",
                    "active_allocation_total": 1_000_000,
                })
            return _ok_json({"payment_status": "unpaid"})

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        assert rec.steps == [
            (5, f"invoice {ids['inv1_id']} status=paid", True, ""),
            (6, f"invoice {ids['inv2_id']} status=unpaid", True, ""),
        ]
        assert rec.failing_step is None

    async def test_invoice_non_200_records_both_failed(self) -> None:
        """A 500 from the invoice projection is a failure for both invoices."""
        rec = SmokeRecorder()
        ids = _make_ids()

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return _ok_json({"access_token": FI_TOKEN})
            return httpx.Response(500, text="projection boom")

        with _use_mock_client(handler):
            await step_05_06_invoices(rec, ids)

        assert len(rec.steps) == 2
        for n, (step, _name, ok, detail) in enumerate(rec.steps, start=5):
            assert step == n
            assert ok is False
            assert detail == "status=500: projection boom"
        assert rec.failing_step == 5


# ---------------------------------------------------------------------------
# _async_main — elapsed reporting + exit code
# ---------------------------------------------------------------------------


class _FakeClock:
    """Module-local ``time`` double — patched over ``smoke_pilot.time`` only
    so the event loop's own clock is untouched.
    """

    def __init__(self, values):
        self._values = list(values)

    def monotonic(self):
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


async def _run_async_main(*, failing_step=None, clock=None):
    """Drive ``_async_main`` over a healthy server with every step stubbed."""
    ids = _make_ids()
    calls: list[int] = []

    async def fake_step_01(rec, settings):
        calls.append(1)
        return ids

    def make_step(numbers):
        async def _step(rec, ids):
            for n in numbers:
                calls.append(n)
                if n == failing_step:
                    rec.record(n, f"step {n}", False, "boom")
            return None

        return _step

    patches = [patch("ksma.scripts.smoke_pilot.step_01_login", side_effect=fake_step_01)]
    for name, numbers in (
        ("step_03_participants", (3,)),
        ("step_04_batches", (4,)),
        ("step_05_06_invoices", (5, 6)),
        ("step_07_session", (7,)),
        ("step_08_portal_inbox", (8,)),
        ("step_09_audit", (9,)),
    ):
        patches.append(patch(f"ksma.scripts.smoke_pilot.{name}", side_effect=make_step(numbers)))
    ctxs = [_use_mock_client(lambda req: _ok_json({"status": "ok"}))] + patches
    if clock is not None:
        ctxs.append(patch.object(smoke_pilot_mod, "time", clock))

    for ctx in ctxs:
        ctx.start()
    try:
        result = await _async_main()
    finally:
        for ctx in reversed(ctxs):
            ctx.stop()
    return result, calls


class TestAsyncMainElapsedAndExitCode:
    async def test_health_elapsed_reported_in_milliseconds(self, capsys) -> None:
        result, calls = await _run_async_main(clock=_FakeClock([100.0, 102.5]))
        out = capsys.readouterr().out

        assert calls == [1, 3, 4, 5, 6, 7, 8, 9]
        # 10ms elapsed * 1000 -> "10ms" (the ``* -> /`` mutant prints "0ms").
        assert "[smoke] server OK (10ms)" in out
        # 102.5 - 100.0 -> "2.5s" (the ``- -> +`` mutant prints "202.5s").
        assert "[smoke] elapsed 2.5s" in out
        assert "PILOT SMOKE PASS" in out
        assert result == 0

    async def test_exit_code_1_when_a_step_fails(self, capsys) -> None:
        result, _calls = await _run_async_main(
            failing_step=4, clock=_FakeClock([0.0, 1.0])
        )
        out = capsys.readouterr().out

        assert "PILOT SMOKE FAIL: step=4" in out
        assert "[smoke] elapsed 1.0s" in out
        assert result == 1


# ---------------------------------------------------------------------------
# Base64 padding sign — equivalence proof (deferred survivor :268)
# ---------------------------------------------------------------------------


# Raw base64url segments spanning every length 1..9 (hence every residue of
# ``len(raw) % 4``) for the padding-equivalence proof below.
_PADDING_EQUIVALENCE_SAMPLES = [
    alphabet * length
    for length in range(1, 10)
    for alphabet in ("A", "BCDE", "0123")
]


class TestLoginJwtPaddingEquivalence:
    """``padded = raw + "=" * (-len(raw) % 4)`` — the ``delete -`` mutant
    yields ``len(raw) % 4``.

    ``base64.urlsafe_b64decode`` runs in non-strict mode: it *tolerates*
    over-padding (it drops the trailing partial quantum) exactly as it
    tolerates under-padding. Exhaustive search over every raw segment of
    length 1..9 (over three alphabets, plus a full 4-symbol alphabet up to
    length 6) shows both paddings decode to identical bytes for every
    input, so this survivor is equivalent and is deferred to the ADR
    follow-up rather than papered over with a production-code change.
    """

    @pytest.mark.parametrize("raw", sorted(_PADDING_EQUIVALENCE_SAMPLES))
    def test_padding_sign_is_unobservable(self, raw: str) -> None:
        def _decode(padding: int):
            try:
                return base64.urlsafe_b64decode(raw + "=" * padding)
            except Exception as exc:  # invalid base64 either way
                return f"{type(exc).__name__}: {exc}"

        # Segments of length 1 mod 4 are invalid base64 under BOTH paddings
        # (the decoder raises identically); every other length decodes to
        # the same bytes. Either way the padding sign is unobservable.
        assert _decode(-len(raw) % 4) == _decode(len(raw) % 4)
