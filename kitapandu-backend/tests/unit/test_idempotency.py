"""Unit tests for the pure-Python idempotency helpers.

Seam: ``ksma.shared.idempotency`` for the value-object layer.
We don't exercise the DB-bound ``check_or_register`` here — that needs
a real Postgres session and lives in integration tests.
"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ksma.shared.errors import IdempotencyKeyConflictError
from ksma.shared.idempotency import (
    IdempotencyContext,
    fingerprint_idempotency_key,
    hash_request_body,
)


class TestFingerprintKey:
    def test_same_key_same_fingerprint(self) -> None:
        a = fingerprint_idempotency_key("abc-123")
        b = fingerprint_idempotency_key("abc-123")
        assert a == b

    def test_different_keys_different_fingerprints(self) -> None:
        a = fingerprint_idempotency_key("abc-123")
        c = fingerprint_idempotency_key("def-456")
        assert a != c

    def test_fingerprint_is_hex_sha256(self) -> None:
        fp = fingerprint_idempotency_key("anything")
        assert len(fp) == 64
        assert all(ch in "0123456789abcdef" for ch in fp)


class TestHashRequestBody:
    def test_dict_canonicalization_is_key_order_independent(self) -> None:
        a = {"a": 1, "b": 2, "c": 3}
        b = {"c": 3, "a": 1, "b": 2}
        assert hash_request_body(a) == hash_request_body(b)

    def test_string_input(self) -> None:
        h = hash_request_body('{"amount": 100}')
        assert len(h) == 64

    def test_bytes_input(self) -> None:
        h = hash_request_body(b'{"amount": 100}')
        assert len(h) == 64

    def test_dict_and_string_of_same_payload_produce_same_hash(self) -> None:
        # The dict is serialised with sort_keys=True before hashing, so it
        # should match the canonical string form.
        payload = {"b": 1, "a": 2}
        string_form = json.dumps(payload, sort_keys=True, default=str)
        assert hash_request_body(payload) == hash_request_body(string_form)

    def test_nested_dict_is_canonicalised(self) -> None:
        a = {"x": {"z": 1, "y": 2}}
        b = {"x": {"y": 2, "z": 1}}
        assert hash_request_body(a) == hash_request_body(b)


class TestIdempotencyContext:
    def test_construction(self) -> None:
        ctx = IdempotencyContext(
            raw_key="key-1",
            fingerprint="fp",
            request_hash="rh",
            action_id="FIN-A05",
            subject_account_id=uuid4(),
            tenant_id=uuid4(),
        )
        assert ctx.raw_key == "key-1"
        assert ctx.action_id == "FIN-A05"
        assert ctx.fingerprint == "fp"
        assert ctx.request_hash == "rh"

    def test_immutable(self) -> None:
        ctx = IdempotencyContext(
            raw_key="k", fingerprint="f", request_hash="r",
            action_id="REG-A02",
            subject_account_id=uuid4(), tenant_id=uuid4(),
        )
        with pytest.raises(FrozenInstanceError):
            ctx.action_id = "FIN-A05"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Issue #37 — same-key/different-body must raise IdempotencyKeyConflictError.
#
# ``check_or_register`` is DB-bound, but the lookup/branch logic lives in
# Python. We exercise it with an in-memory fake ``AsyncSession`` that mimics
# the SELECT... return shape — enough to drive the conflict branch.
# ---------------------------------------------------------------------------


class _FakeRow:
    """Stand-in for the row returned by `SELECT ... FROM idempotency_record`."""

    def __init__(self, *, id, status, request_hash, result_ref=None):
        self.id = id
        self.status = status
        self.request_hash = request_hash
        self.result_ref = result_ref


class _FakeExecuteResult:
    """Mimics SQLAlchemy 2.x ``Result.first()`` / ``Result.all()``."""

    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeAsyncSession:
    """Minimal AsyncSession double — records execute calls, returns pre-set rows."""

    def __init__(self, *, lookup_rows=()):
        self._lookup_rows = list(lookup_rows)
        self.execute_calls: list[tuple] = []

    async def execute(self, statement, params=None):
        # Record every execute call so tests can pin the SQL + params.
        self.execute_calls.append((str(statement), params))
        # The first execute call is the SELECT in check_or_register. Return
        # our pre-set row(s).
        return _FakeExecuteResult(self._lookup_rows)


@pytest.mark.asyncio
async def test_check_or_register_raises_on_same_key_different_body() -> None:
    """R21: same Idempotency-Key + different request body MUST raise."""
    from ksma.shared.idempotency import check_or_register  # noqa: PLC0415

    existing_id = uuid4()
    existing = _FakeRow(
        id=existing_id,
        status="succeeded",
        request_hash="deadbeef" * 8,  # 64-hex
        result_ref="invoice:abc",
    )
    session = _FakeAsyncSession(lookup_rows=[existing])

    ctx = IdempotencyContext(
        raw_key="abc-123",
        fingerprint="some-fingerprint",
        # Different from existing.request_hash — the conflict branch.
        request_hash="cafef00d" * 8,
        action_id="FIN-A05",
        subject_account_id=uuid4(),
        tenant_id=uuid4(),
    )

    with pytest.raises(IdempotencyKeyConflictError) as exc_info:
        await check_or_register(session, ctx=ctx)

    # Pin the *type* (R21), the *code* (wire contract) and the *details*
    # (operator + debugging surface for client-side tracing).
    err = exc_info.value
    assert err.code == "IDEMPOTENCY_KEY_CONFLICT"
    assert err.http_status == 422
    assert err.details == {"action_id": "FIN-A05"}


@pytest.mark.asyncio
async def test_check_or_register_returns_succeeded_record_when_body_matches() -> None:
    """Same key + same body + status='succeeded' returns the stored record."""
    from ksma.shared.idempotency import check_or_register  # noqa: PLC0415

    existing_id = uuid4()
    request_hash = "abcd" * 16
    existing = _FakeRow(
        id=existing_id,
        status="succeeded",
        request_hash=request_hash,
        result_ref="invoice:abc",
    )
    session = _FakeAsyncSession(lookup_rows=[existing])

    ctx = IdempotencyContext(
        raw_key="abc-123",
        fingerprint="some-fingerprint",
        request_hash=request_hash,  # SAME body as the stored record
        action_id="FIN-A05",
        subject_account_id=uuid4(),
        tenant_id=uuid4(),
    )

    record = await check_or_register(session, ctx=ctx)
    assert record is not None
    assert record.id == existing_id
    assert record.status == "succeeded"
    assert record.result_ref == "invoice:abc"


@pytest.mark.asyncio
async def test_check_or_register_inserts_when_no_prior_record() -> None:
    """First request with a fresh key inserts an 'in_progress' row."""
    from ksma.shared.idempotency import check_or_register  # noqa: PLC0415

    session = _FakeAsyncSession(lookup_rows=[])  # no prior record

    ctx = IdempotencyContext(
        raw_key="fresh-key",
        fingerprint="some-fingerprint",
        request_hash="ffff" * 16,
        action_id="REG-A02",
        subject_account_id=uuid4(),
        tenant_id=uuid4(),
    )

    record = await check_or_register(session, ctx=ctx)
    # No prior record → caller is expected to proceed; no record returned.
    assert record is None
    # The SELECT must be followed by an INSERT.
    assert len(session.execute_calls) == 2
    insert_sql, insert_params = session.execute_calls[1]
    # INSERT statement shape — pin the OUTBOX_TABLE / idempotency_record name
    # so an accidental table-name flip fails here.
    assert "INSERT INTO idempotency_record" in insert_sql
    assert "'in_progress'" in insert_sql
    assert insert_params["act"] == "REG-A02"
    assert insert_params["rh"] == "ffff" * 16


# ---------------------------------------------------------------------------
# Issue #37 — replay after grant revocation returns 403.
#
# This is enforced at the FastAPI dep layer (``require_action``), which
# calls ``evaluate(grants=...)`` and translates ``allowed=False`` to a 403.
# The grant side is covered in ``test_rbac_action_registry`` (see
# ``test_revoked_grant_denies``); the wire-shape assertion lives here so a
# future refactor that breaks the dep translation is caught in the same
# ticket cluster.
# ---------------------------------------------------------------------------


def test_replay_after_grant_revocation_yields_allowed_false() -> None:
    """If the actor's grant has been revoked, a replay returns allowed=False.

    The FastAPI dep ``require_action`` raises ``HTTPException(403)`` on
    ``decision.allowed is False`` — see ``ksma.shared.auth.dependencies``.
    Pin the resolver decision here so any change to ``evaluate()`` that
    lets a revoked grant slip through is caught.
    """
    from datetime import datetime  # noqa: PLC0415

    from ksma.shared.rbac import (  # noqa: PLC0415
        ActionCode,
        GrantDecision,
        GrantRow,
        RoleCode,
        Scope,
        Target,
        evaluate,
    )

    fixed_now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    revoked_grant = GrantRow(
        id=uuid4(),
        role_code=RoleCode.FI,
        action_ids=frozenset({ActionCode.FIN_A05}),
        scope=Scope(),
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_until=None,
        status="revoked",
        policy_id=uuid4(),
    )
    decision = evaluate(
        actor=None,
        action=ActionCode.FIN_A05,
        target=Target(),
        now=fixed_now,
        grants=(revoked_grant,),
    )
    assert isinstance(decision, GrantDecision)
    assert decision.allowed is False
    assert decision.grant_id is None
    # The reason string is a wire-level diagnostic. We don't pin the exact
    # text (too brittle), but it MUST be present and non-empty.
    assert decision.reason

# ---------------------------------------------------------------------------
# Ticket #27 — spec #16 user story #12: the retry contract.
#   * a ``failed`` record is *cleared* so the caller may retry (R21)
#   * a fresh record expires 24h after registration (default ``ttl_seconds``)
# ---------------------------------------------------------------------------


def _ctx(*, request_hash: str, action_id: str = "FIN-A05") -> IdempotencyContext:
    return IdempotencyContext(
        raw_key="abc-123",
        fingerprint="some-fingerprint",
        request_hash=request_hash,
        action_id=action_id,
        subject_account_id=uuid4(),
        tenant_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_check_or_register_returns_in_progress_for_concurrent_request() -> None:
    """A concurrent in-flight request must be told to retry, not re-registered."""
    from ksma.shared.idempotency import check_or_register  # noqa: PLC0415

    existing_id = uuid4()
    request_hash = "abcd" * 16
    session = _FakeAsyncSession(
        lookup_rows=[
            _FakeRow(
                id=existing_id,
                status="in_progress",
                request_hash=request_hash,
                result_ref=None,
            )
        ]
    )

    record = await check_or_register(session, ctx=_ctx(request_hash=request_hash))

    assert record is not None
    assert record.id == existing_id
    assert record.status == "in_progress"
    assert record.result_ref is None
    # No DELETE / re-INSERT: the concurrent row is left alone.
    assert len(session.execute_calls) == 1


@pytest.mark.asyncio
async def test_check_or_register_clears_failed_record_and_retries() -> None:
    """A ``failed`` record is deleted so the request proceeds; the DELETE is
    what makes the retry idempotent-safe (kills ``status == 'in_progress'``
    flipped to ``!=``, which would short-circuit and replay a bogus record).
    """
    from ksma.shared.idempotency import check_or_register  # noqa: PLC0415

    existing_id = uuid4()
    request_hash = "abcd" * 16
    session = _FakeAsyncSession(
        lookup_rows=[
            _FakeRow(
                id=existing_id, status="failed", request_hash=request_hash, result_ref=None
            )
        ]
    )

    record = await check_or_register(session, ctx=_ctx(request_hash=request_hash))

    assert record is None  # caller proceeds with the real work
    assert len(session.execute_calls) == 3
    delete_sql, delete_params = session.execute_calls[1]
    assert "DELETE FROM idempotency_record" in delete_sql
    assert delete_params == {"id": str(existing_id)}
    insert_sql, insert_params = session.execute_calls[2]
    assert "INSERT INTO idempotency_record" in insert_sql
    assert insert_params["rh"] == request_hash
    assert insert_params["act"] == "FIN-A05"


@pytest.mark.asyncio
async def test_check_or_register_default_ttl_is_24_hours() -> None:
    """Default ``ttl_seconds`` is ``24 * 3600`` and ``expires_at`` is in the
    FUTURE (kills ``* -> /`` and ``+ -> -`` on the expiry arithmetic)."""
    from ksma.shared.idempotency import check_or_register  # noqa: PLC0415

    session = _FakeAsyncSession(lookup_rows=[])
    before = datetime.now(UTC)

    assert await check_or_register(session, ctx=_ctx(request_hash="f" * 64)) is None

    after = datetime.now(UTC)
    assert len(session.execute_calls) == 2
    insert_sql, insert_params = session.execute_calls[1]
    assert "INSERT INTO idempotency_record" in insert_sql

    expires_at = insert_params["exp"]
    assert expires_at.tzinfo is not None
    assert before + timedelta(seconds=24 * 3600) <= expires_at
    assert expires_at <= after + timedelta(seconds=24 * 3600)


@pytest.mark.asyncio
async def test_check_or_register_honours_explicit_ttl_seconds() -> None:
    """An explicit ``ttl_seconds`` overrides the 24h default."""
    from ksma.shared.idempotency import check_or_register  # noqa: PLC0415

    session = _FakeAsyncSession(lookup_rows=[])
    before = datetime.now(UTC)

    assert (
        await check_or_register(session, ctx=_ctx(request_hash="f" * 64), ttl_seconds=60)
        is None
    )

    after = datetime.now(UTC)
    _, insert_params = session.execute_calls[1]
    expires_at = insert_params["exp"]
    assert before + timedelta(seconds=60) <= expires_at
    assert expires_at <= after + timedelta(seconds=60)
