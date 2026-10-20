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
