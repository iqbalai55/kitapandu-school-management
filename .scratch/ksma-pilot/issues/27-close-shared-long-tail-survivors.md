# 27: Kill the `shared/*` long-tail survivors — parametrise the 13-role enum, currency rejection, idempotency conflict, clock determinism, outbox preconditions, scope evaluation

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for the `ksma.shared` long-tail files — `shared/money`, `shared/idempotency`, `shared/outbox`, `shared/time`, `shared/errors`, `shared/persistence`, `shared/events`, `shared/tenancy`, `shared/rbac`, plus `shared/audit` and `shared/attachments`. Each test pins the documented invariant from `docs/specs/000-umbrella-platform.md`:

- `Money.__add__` raises `ValidationError` on cross-currency add (per spec #16 user story #13)
- `IdempotencyStore.check_or_register` raises `IdempotencyConflictError` on same-key/different-body replay (per spec #16 user story #12)
- `Clock.now()` / `Clock.today()` are stable under `freezegun` (per user story #14)
- `Outbox.enqueue` is a no-op when the surrounding `session_scope()` exits without committing (per user story #16)
- `DomainError` → HTTP-status mapping covers every code in `INVALID_VALUE`, `REQUIRED_FIELD`, `REFERENCE_SCOPE_MISMATCH`, `STATE_CONFLICT`, `VERSION_CONFLICT`, `CAPACITY_EXCEEDED`, `ACCESS_DENIED`, `IDEMPOTENCY_CONFLICT`, `POLICY_NOT_CONFIGURED`, and the body never leaks an object payload when the code is `ACCESS_DENIED` (per user story #20)
- `TenantContext.{is_service, is_support, is_account, has_role}` returns the documented value for every member of the 13-role enum (per user story #17)
- `Scope.from_json` and `evaluate` are parametrised over every `ScopeKind` enum member, killing the 8 surviving mutations in `shared/rbac/__init__.py` (per user story #18)
- `AttachmentStorage.{put, get, delete, exists}` round-trip on both `LocalAttachmentStorage` and `S3AttachmentStorage`

Killing the long-tail survivors across these shared modules is the end-to-end behaviour this ticket makes work.

**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from `ksma.shared.*` reduced to 0 across all touched files.
- [ ] Every existing test file in `tests/unit/test_money.py`, `test_idempotency.py`, `test_clock.py`, `test_outbox.py`, `test_errors.py`, `test_tenancy.py`, `test_rbac_action_registry.py`, `test_auth.py`, `test_attachments.py` is extended with a positive assertion (not just `result is not None`).
- [ ] At least one new assertion per shared file is enforced as a parametrised boundary table.
- [ ] No production code under `ksma.shared` is modified.
- [ ] `pytest tests/unit -q` exits 0.
