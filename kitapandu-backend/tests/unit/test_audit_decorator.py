"""Unit tests for the ``@audited`` decorator + audit row writer.

Seam: ``ksma.shared.audit``. Public surface:
- ``audited(action_id, *, object_kind, object_id_arg, reason_required, change_ref_arg)``
  — the service-layer decorator that writes one ``AuditRecord`` row in the same
  transaction as the wrapped call, or NO row when the wrapped call raises.
- ``write_audit_record(session, *, action_id, object_ref, ctx, correlation_id,
   authorization_ref, change_ref, reason)`` — the underlying INSERT helper used
  by the decorator (and by code paths that need to append audit rows ad-hoc).

Tests assert behaviour at the public Python seam: the decorator's call surface,
the row shape (fields + JSON), and the rollback-on-error contract. They are
pure-Python; the persistence layer is an in-memory ``FakeAsyncSession`` that
captures the INSERT parameter dict and returns a synthetic row id. No DB, no
FastAPI app.

What this file pins (so an operator flip in the decorator fails at least one test):
- success path writes exactly one row with the documented field shape;
- failure path writes NO row;
- ``reason_required=True`` raises ``ValidationError`` when the matching
  ``reason``/``correction_reason``/``revocation_reason`` kwarg is absent or
  empty (and ONLY then);
- row's ``correlation_id`` matches the ``correlation_id`` attribute on the
  ``TenantContext`` instance handed in at call time (``unknown`` when absent);
- ``object_kind`` and ``object_id_arg`` populate ``object_ref`` with
  ``{kind, id}`` (id resolved from the kwarg, else from ``result.id``);
- ``change_ref_arg`` populates the row's ``change_ref`` (dict pass-through;
  non-dict wrapped as ``{"value": v}``);
- the row's ``reason`` column carries the value of the matched reason kwarg.

What is NOT asserted here:
- The Postgres row-level RLS / append-only enforcement (integration test);
- The HTTP envelope in ``Response`` from ``ksma.main`` (covered by ``test_errors``);
- The actual SQL produced by ``text(...)`` — we capture the bound ``params``
  dict, not the literal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from ksma.shared.audit import audited, write_audit_record
from ksma.shared.errors import ValidationError
from ksma.shared.tenancy import ActorKind, TenantContext

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakeRow:
    """Stand-in for the SQLAlchemy ``Row`` returned from ``.first()``."""

    id: UUID


class _FakeExecuteResult:
    """Stand-in for SQLAlchemy's async ``Result`` returned by ``session.execute``."""

    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row
        self.calls: list[tuple[Any, dict[str, Any] | None]] = []

    def first(self) -> _FakeRow | None:
        return self._row


class FakeAsyncSession:
    """Minimal async-session fake that records every ``execute(text, params)`` call.

    The audit code path only needs:
      - ``await session.execute(text(...), params)`` → result-like object
      - ``result.first()`` → row-like object with ``.id``
    """

    def __init__(self, *, row_id: UUID | None = None) -> None:
        self.row_id = row_id if row_id is not None else uuid4()
        self.calls: list[dict[str, Any]] = []
        # Pre-build a result so the very first execute returns our row.
        self._next_result = _FakeExecuteResult(_FakeRow(id=self.row_id))

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeExecuteResult:
        self._next_result.calls.append((statement, params))
        self.calls.append(
            {
                "statement": statement,
                "params": dict(params) if params else {},
            }
        )
        result = self._next_result
        # Subsequent calls return an empty result (no second row in any test path).
        self._next_result = _FakeExecuteResult(None)
        return result


@dataclass(frozen=True)
class _CorrelationCtxStub:
    """Stand-in for ``TenantContext`` that also carries ``correlation_id``.

    Production ``TenantContext`` deliberately does not expose ``correlation_id``
    (the decorator reads it via ``getattr(..., "unknown")``); the tests that
    pin a specific correlation value construct this stub so the decorator's
    ``getattr(ctx, "correlation_id", "unknown")`` resolves to the test value.
    The audit helper accesses ``account_id``, ``membership_id``, ``tenant_id``
    and ``role_codes`` by dot access — no isinstance check — so a structurally
    compatible stub drives the wrapper end-to-end.
    """

    account_id: UUID | None
    membership_id: UUID | None
    tenant_id: UUID
    unit_id: UUID | None
    actor_kind: ActorKind
    authz_revision: int
    role_codes: tuple[str, ...] = ()
    correlation_id: str = "unknown"


def _ctx(**overrides: Any) -> TenantContext:
    """Build a real ``TenantContext`` for tests (no test-only fields)."""
    defaults: dict[str, Any] = dict(
        account_id=uuid4(),
        membership_id=uuid4(),
        tenant_id=uuid4(),
        unit_id=None,
        actor_kind=ActorKind.ACCOUNT,
        authz_revision=1,
        role_codes=("AC",),
    )
    defaults.update(overrides)
    return TenantContext(**defaults)


def _ctx_with_correlation(correlation_id: str, **overrides: Any) -> _CorrelationCtxStub:
    """Build a stub context carrying the given ``correlation_id``."""
    defaults: dict[str, Any] = dict(
        account_id=uuid4(),
        membership_id=uuid4(),
        tenant_id=uuid4(),
        unit_id=None,
        actor_kind=ActorKind.ACCOUNT,
        authz_revision=1,
        role_codes=("AC",),
    )
    defaults.update(overrides)
    return _CorrelationCtxStub(correlation_id=correlation_id, **defaults)


def _last_insert_params(session: FakeAsyncSession) -> dict[str, Any]:
    """Return the parameter dict of the most recent ``execute(text, params)`` call."""
    assert session.calls, "expected at least one session.execute call"
    return session.calls[-1]["params"]


# ---------------------------------------------------------------------------
# audited(...) — success path
# ---------------------------------------------------------------------------


class TestAuditedSuccessPath:
    """Pins: success path writes exactly one row with the documented shape."""

    @pytest.mark.asyncio
    async def test_writes_exactly_one_audit_row_on_success(self) -> None:
        session = FakeAsyncSession()
        ctx = _ctx()

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_):
            return _Result(id=uuid4())

        result = await decide(session, ctx)

        assert isinstance(result, _Result)
        assert len(session.calls) == 1, (
            "success must write exactly one INSERT (no extra SELECTs/probes)"
        )

    @pytest.mark.asyncio
    async def test_writes_documented_field_shape_on_success(self) -> None:
        session = FakeAsyncSession()
        ctx = _ctx()
        result_id = uuid4()

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_):
            return _Result(id=result_id)

        await decide(session, ctx)

        params = _last_insert_params(session)
        # action_id from the decorator's first positional arg.
        assert params["action_id"] == "REG-A04"
        # object_ref is a JSON string of {"kind": ..., "id": ...}.
        obj = json.loads(params["obj"])
        assert obj == {"kind": "application", "id": str(result_id)}
        # actor was derived from ctx.account_id.
        actor = json.loads(params["actor"])
        assert actor == {"kind": "account", "id": str(ctx.account_id)}
        # authorization_ref for an authenticated actor is the membership grant.
        # ``role_code`` carries the first element of ``role_codes`` (or
        # ``"unknown"`` when empty) — same pattern as in
        # ``ksma.operations.application._audit``.
        auth = json.loads(params["auth"])
        assert auth == {
            "kind": "membership",
            "id": str(ctx.membership_id),
            "role_code": ctx.role_codes[0] if ctx.role_codes else "unknown",
        }
        # correlation_id comes from ctx (TenantContext has no correlation_id,
        # so getattr falls back to "unknown" — that is the documented default).
        assert params["cid"] == "unknown"
        # tenant_id from ctx.
        assert params["tid"] == str(ctx.tenant_id)
        # No reason kwarg → reason column is NULL.
        assert params["reason"] is None
        # No change_ref_arg → change_ref is NULL.
        assert params["change"] is None

    @pytest.mark.asyncio
    async def test_object_ref_kind_falls_back_to_unknown_when_object_kind_omitted(self) -> None:
        """``object_kind or "unknown"`` — kills the ``or -> and`` mutation on line 136."""
        session = FakeAsyncSession()

        @audited("REG-A04")  # no object_kind
        async def decide(session_, ctx_):
            return _Result(id=uuid4())

        await decide(session, _ctx())

        obj = json.loads(_last_insert_params(session)["obj"])
        assert obj["kind"] == "unknown", (
            "with object_kind omitted, object_ref.kind defaults to 'unknown'"
        )

    @pytest.mark.asyncio
    async def test_object_ref_id_uses_result_id_when_no_object_id_arg(self) -> None:
        """``elif result is not None and hasattr(result, "id")`` — line 140.

        Kills the ``and -> or`` mutation by asserting that a result *without* an
        ``.id`` attribute (or with ``result is None``) does not produce an
        ``id`` key in ``object_ref``.
        """
        session = FakeAsyncSession()

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_):
            return _NoIdResult()  # no .id attribute

        await decide(session, _ctx())

        obj = json.loads(_last_insert_params(session)["obj"])
        assert "id" not in obj, (
            "without object_id_arg and result without .id, object_ref must NOT have id"
        )

    @pytest.mark.asyncio
    async def test_object_id_arg_overrides_result_id(self) -> None:
        """``if object_id_arg and object_id_arg in kwargs`` — line 138.

        Kills the ``and -> or`` mutation: with ``object_id_arg="application_id"``
        and that kwarg provided, the row's ``object_ref.id`` is the kwarg's
        value, NOT ``result.id``.
        """
        session = FakeAsyncSession()
        kwarg_id = uuid4()
        result_id = uuid4()

        @audited("REG-A04", object_kind="application", object_id_arg="application_id")
        async def decide(session_, ctx_, *, application_id):
            return _Result(id=result_id)

        await decide(session, _ctx(), application_id=kwarg_id)

        obj = json.loads(_last_insert_params(session)["obj"])
        assert obj == {"kind": "application", "id": str(kwarg_id)}
        assert obj["id"] != str(result_id), (
            "object_id_arg must take precedence over result.id"
        )


# ---------------------------------------------------------------------------
# audited(...) — failure path
# ---------------------------------------------------------------------------


class TestAuditedFailurePath:
    """Pins: failure path writes NO row (the same-transaction rollback contract)."""

    @pytest.mark.asyncio
    async def test_writes_no_row_when_wrapped_call_raises(self) -> None:
        session = FakeAsyncSession()

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await decide(session, _ctx())

        assert session.calls == [], (
            "if the wrapped call raises, no AuditRecord row may be written"
        )

    @pytest.mark.asyncio
    async def test_writes_no_row_when_wrapped_call_raises_after_partial_work(self) -> None:
        """A partial-state raise (after some internal side effect) still skips the row."""
        session = FakeAsyncSession()
        marker = {"ran": False}

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_):
            marker["ran"] = True
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await decide(session, _ctx())

        assert marker["ran"] is True  # wrapped function actually executed
        assert session.calls == [], (
            "even when the wrapped body ran partially, an exception must skip the audit write"
        )


# ---------------------------------------------------------------------------
# audited(...) — reason enforcement
# ---------------------------------------------------------------------------


class TestAuditedReasonRequired:
    """Pins the reason_required + reason-arg-name resolution contract."""

    @pytest.mark.asyncio
    async def test_reason_required_raises_without_reason_kwarg(self) -> None:
        session = FakeAsyncSession()

        @audited("REG-A08", object_kind="application", reason_required=True)
        async def revoke(session_, ctx_, *, reason):
            return _Result(id=uuid4())

        with pytest.raises(ValidationError) as excinfo:
            await revoke(session, _ctx())  # no reason

        assert excinfo.value.code == "INVALID_VALUE"
        assert "reason is required" in excinfo.value.message
        assert "REG-A08" in excinfo.value.message
        assert session.calls == [], "no audit row when reason_required is violated"

    @pytest.mark.asyncio
    async def test_reason_required_raises_when_reason_is_empty(self) -> None:
        """`if not kwargs.get(reason_arg):` — kills the ``delete not`` mutation on line 128."""
        session = FakeAsyncSession()

        @audited("REG-A08", reason_required=True)
        async def revoke(session_, ctx_, *, reason):
            return _Result(id=uuid4())

        with pytest.raises(ValidationError):
            await revoke(session, _ctx(), reason="")  # empty reason is still missing

        assert session.calls == [], "no audit row when reason is empty string"

    @pytest.mark.asyncio
    async def test_reason_required_raises_when_reason_is_none(self) -> None:
        session = FakeAsyncSession()

        @audited("REG-A08", reason_required=True)
        async def revoke(session_, ctx_, *, reason):
            return _Result(id=uuid4())

        with pytest.raises(ValidationError):
            await revoke(session, _ctx(), reason=None)

    @pytest.mark.asyncio
    async def test_reason_required_passes_with_valid_reason(self) -> None:
        session = FakeAsyncSession()

        @audited("REG-A08", reason_required=True)
        async def revoke(session_, ctx_, *, reason):
            return _Result(id=uuid4())

        await revoke(session, _ctx(), reason="audit-trail-required")

        assert len(session.calls) == 1
        assert _last_insert_params(session)["reason"] == "audit-trail-required"

    @pytest.mark.asyncio
    async def test_reason_required_false_does_not_raise_when_reason_missing(self) -> None:
        """`reason_required: bool = False` default — kills the ``False -> True`` mutation on line 94."""
        session = FakeAsyncSession()

        @audited("REG-A04")  # default reason_required=False
        async def decide(session_, ctx_, *, reason="ignored"):
            return _Result(id=uuid4())

        # No reason passed and reason_required is False → must succeed.
        result = await decide(session, _ctx())
        assert isinstance(result, _Result)
        assert len(session.calls) == 1
        assert _last_insert_params(session)["reason"] is None

    @pytest.mark.asyncio
    async def test_reason_required_and_requires_reason_arg_present(self) -> None:
        """`if reason_required and reason_arg:` — kills the ``and -> or`` mutation on line 127.

        With ``and -> or`` this branch would also fire when ``reason_required``
        is False but a ``reason`` kwarg is present (and ``kwargs.get(reason)``
        is falsy), incorrectly raising on every falsy reason value.
        """
        session = FakeAsyncSession()

        @audited("REG-A04")  # reason_required=False
        async def decide(session_, ctx_, *, reason=""):
            return _Result(id=uuid4())

        # With reason_required=False and an empty reason, must NOT raise.
        result = await decide(session, _ctx(), reason="")
        assert isinstance(result, _Result)
        assert len(session.calls) == 1

    @pytest.mark.asyncio
    async def test_correction_reason_alias_is_recognized(self) -> None:
        """``reason_arg`` resolution picks ``correction_reason`` when present."""
        session = FakeAsyncSession()

        @audited("ASM-A07", object_kind="assessment", reason_required=True)
        async def correct(session_, ctx_, *, correction_reason):
            return _Result(id=uuid4())

        with pytest.raises(ValidationError):
            await correct(session, _ctx())  # missing correction_reason

        await correct(session, _ctx(), correction_reason="scoring-typo")
        assert _last_insert_params(session)["reason"] == "scoring-typo"

    @pytest.mark.asyncio
    async def test_revocation_reason_alias_is_recognized(self) -> None:
        session = FakeAsyncSession()

        @audited("PRT-A04", reason_required=True)
        async def revoke(session_, ctx_, *, revocation_reason):
            return _Result(id=uuid4())

        with pytest.raises(ValidationError):
            await revoke(session, _ctx())

        await revoke(session, _ctx(), revocation_reason="gdpr-erasure")
        assert _last_insert_params(session)["reason"] == "gdpr-erasure"


# ---------------------------------------------------------------------------
# audited(...) — correlation_id resolution
# ---------------------------------------------------------------------------


class TestAuditedCorrelationId:
    """Pins: row's correlation_id matches whatever ``ctx`` carries (or ``unknown``)."""

    @pytest.mark.asyncio
    async def test_correlation_id_echoes_ctx_attribute_when_present(self) -> None:
        """`getattr(ctx, "correlation_id", "unknown")` — row mirrors the ctx attribute."""
        session = FakeAsyncSession()
        ctx = _ctx_with_correlation("corr-abc-123")

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_):
            return _Result(id=uuid4())

        await decide(session, ctx)

        assert _last_insert_params(session)["cid"] == "corr-abc-123"

    @pytest.mark.asyncio
    async def test_correlation_id_defaults_to_unknown_when_ctx_lacks_attribute(self) -> None:
        """Plain ``TenantContext`` has no ``correlation_id`` → row gets ``unknown``."""
        session = FakeAsyncSession()
        ctx = _ctx()  # no correlation_id attribute

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_):
            return _Result(id=uuid4())

        await decide(session, ctx)

        assert _last_insert_params(session)["cid"] == "unknown"

    @pytest.mark.asyncio
    async def test_correlation_id_defaults_to_unknown_when_ctx_is_none(self) -> None:
        """``if ctx else "unknown"`` — kills a dropped-condition mutation on line 133."""
        session = FakeAsyncSession()

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_=None):
            return _Result(id=uuid4())

        # Hand-call with no ctx — the decorator must still write the row.
        await decide(session)

        assert _last_insert_params(session)["cid"] == "unknown"

    @pytest.mark.asyncio
    async def test_correlation_id_per_call_reflects_call_time_value(self) -> None:
        """The decorator reads ``ctx.correlation_id`` at the moment of the call."""
        session = FakeAsyncSession()
        ctx = _ctx()

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_):
            return _Result(id=uuid4())

        # 1st call: no correlation_id → "unknown"
        await decide(session, ctx)
        assert _last_insert_params(session)["cid"] == "unknown"

        # 2nd call: ctx extended with correlation_id → echoes it
        ctx_with = _ctx_with_correlation("call-42")
        await decide(session, ctx_with)
        assert _last_insert_params(session)["cid"] == "call-42"


# ---------------------------------------------------------------------------
# audited(...) — positional vs kwarg ctx resolution
# ---------------------------------------------------------------------------


class TestAuditedCtxResolution:
    """Pins: ``args[1]`` for positional ctx, ``kwargs["ctx"]`` for kwarg ctx."""

    @pytest.mark.asyncio
    async def test_ctx_taken_from_positional_args_when_two_plus_positional(self) -> None:
        """``args[1] if len(args) > 1 else kwargs.get("ctx")`` — line 126.

        With two positional args the decorator must take ctx from ``args[1]``,
        NOT from a kwarg named "ctx" passed alongside.
        """
        session = FakeAsyncSession()
        pos_ctx = _ctx_with_correlation("pos-corr")
        kw_ctx = _ctx_with_correlation("kw-corr")

        @audited("REG-A04", object_kind="application")
        async def decide(session_, ctx_, **rest):
            return _Result(id=uuid4())

        # Pass ctx positionally AND as a kwarg. The wrapper must take
        # ``args[1]`` (positional) — kwarg "ctx" is just forwarded through.
        await decide(session, pos_ctx, ctx=kw_ctx)

        assert _last_insert_params(session)["cid"] == "pos-corr", (
            "when both positional and kwarg ctx are present, positional wins"
        )

    @pytest.mark.asyncio
    async def test_ctx_taken_from_kwarg_when_only_one_positional(self) -> None:
        """``args[1] if len(args) > 1 else kwargs.get("ctx")`` — line 126 (kwarg fallback)."""
        session = FakeAsyncSession()
        kw_ctx = _ctx_with_correlation("kw-corr")

        @audited("REG-A04", object_kind="application")
        async def decide(session_, **rest):
            return _Result(id=uuid4())

        # Only one positional arg; ctx is given as a kwarg.
        await decide(session, ctx=kw_ctx)

        assert _last_insert_params(session)["cid"] == "kw-corr"


# ---------------------------------------------------------------------------
# audited(...) — change_ref handling
# ---------------------------------------------------------------------------


class TestAuditedChangeRef:
    """Pins: change_ref_arg populates the row's change_ref column."""

    @pytest.mark.asyncio
    async def test_change_ref_arg_passes_dict_through(self) -> None:
        """`if not isinstance(change_ref, dict):` — line 147 (dict pass-through).

        Kills the ``delete not`` mutation: a dict input must land as JSON
        ``{"k": "v"}``, NOT be wrapped as ``{"value": {"k": "v"}}``.
        """
        session = FakeAsyncSession()

        @audited("REG-A04", change_ref_arg="prior_state")
        async def decide(session_, ctx_, *, prior_state):
            return _Result(id=uuid4())

        await decide(session, _ctx(), prior_state={"status": "draft", "version": 2})

        change = json.loads(_last_insert_params(session)["change"])
        assert change == {"status": "draft", "version": 2}
        assert "value" not in change, (
            "dict input must NOT be wrapped — only non-dict values are wrapped"
        )

    @pytest.mark.asyncio
    async def test_change_ref_arg_wraps_non_dict_in_value_envelope(self) -> None:
        """`if not isinstance(change_ref, dict):` — line 147 (wrapping branch).

        Kills the ``delete not`` mutation: a string input must be wrapped as
        ``{"value": "string"}``, NOT land as the raw string.
        """
        session = FakeAsyncSession()

        @audited("REG-A04", change_ref_arg="prior_state")
        async def decide(session_, ctx_, *, prior_state):
            return _Result(id=uuid4())

        await decide(session, _ctx(), prior_state="old-string-value")

        change = json.loads(_last_insert_params(session)["change"])
        assert change == {"value": "old-string-value"}

    @pytest.mark.asyncio
    async def test_change_ref_none_when_change_ref_arg_omitted(self) -> None:
        """`if change_ref_arg and change_ref_arg in kwargs:` — line 145.

        Kills the ``and -> or`` mutation: when the configured kwarg name is
        absent from kwargs, change_ref stays None rather than reading from an
        unrelated kwarg.
        """
        session = FakeAsyncSession()

        @audited("REG-A04", change_ref_arg="prior_state")
        async def decide(session_, ctx_, *, reason="x"):
            return _Result(id=uuid4())

        await decide(session, _ctx(), reason="x")  # no prior_state

        assert _last_insert_params(session)["change"] is None


# ---------------------------------------------------------------------------
# write_audit_record — direct helper
# ---------------------------------------------------------------------------


class TestWriteAuditRecord:
    """Pins the low-level ``write_audit_record`` helper (used outside the decorator)."""

    @pytest.mark.asyncio
    async def test_writes_row_with_account_authorization_ref_when_ctx_present(self) -> None:
        """``authorization_ref = authorization_ref or {…membership…}`` — line 56 (account branch)."""
        session = FakeAsyncSession()
        ctx = _ctx(role_codes=("FI",))

        await write_audit_record(
            session,
            action_id="FIN-A05",
            object_ref={"kind": "invoice", "id": str(uuid4())},
            ctx=ctx,
            correlation_id="corr-xyz",
        )

        assert len(session.calls) == 1
        params = _last_insert_params(session)
        assert params["action_id"] == "FIN-A05"
        assert params["tid"] == str(ctx.tenant_id)
        assert params["cid"] == "corr-xyz"
        auth = json.loads(params["auth"])
        assert auth["kind"] == "membership"
        assert auth["id"] == str(ctx.membership_id)
        assert auth["role_code"] == "FI"

    @pytest.mark.asyncio
    async def test_writes_system_authorization_ref_when_ctx_none(self) -> None:
        """``if ctx is None: … authorization_ref = … or {"kind": "system"}`` — line 52.

        Kills the ``or -> and`` mutation: when both ``ctx is None`` AND
        ``authorization_ref`` is unset, the row must get ``{"kind": "system"}``,
        not ``None``.
        """
        session = FakeAsyncSession()

        await write_audit_record(
            session,
            action_id="PLT-A05",
            object_ref={"kind": "tenant", "id": str(uuid4())},
            ctx=None,
            correlation_id="sys-1",
            authorization_ref=None,
        )

        auth = json.loads(_last_insert_params(session)["auth"])
        assert auth == {"kind": "system"}
        actor = json.loads(_last_insert_params(session)["actor"])
        assert actor == {"kind": "system", "id": "audit-stale"}
        # tenant_id is NULL when ctx is None.
        assert _last_insert_params(session)["tid"] is None

    @pytest.mark.asyncio
    async def test_caller_supplied_authorization_ref_wins(self) -> None:
        """The caller-provided ``authorization_ref`` overrides the default."""
        session = FakeAsyncSession()
        ctx = _ctx()

        custom_auth = {"kind": "support_grant", "id": str(uuid4())}
        await write_audit_record(
            session,
            action_id="PRT-A04",
            object_ref={"kind": "subject", "id": str(uuid4())},
            ctx=ctx,
            correlation_id="sup-1",
            authorization_ref=custom_auth,
        )

        auth = json.loads(_last_insert_params(session)["auth"])
        assert auth == custom_auth


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Result:
    id: UUID


@dataclass(frozen=True, slots=True)
class _NoIdResult:
    """Result without ``.id`` — used to pin the ``elif`` fallback branch."""


