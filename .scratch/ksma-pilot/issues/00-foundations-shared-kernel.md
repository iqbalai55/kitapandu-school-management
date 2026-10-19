# 00: Foundations — shared kernel completeness

**What to build:** The cross-cutting primitives that every bounded context depends on. After this ticket lands, every BC route can declare `Depends(tenant_context_dep)`, `Depends(require_action(ActionCode.X))`, write through `enqueue(...)`, claim `Idempotency-Key`, run `Money(...)` arithmetic, decorate with `@audited(...)`, and rely on Postgres RLS via the `session_scope()` GUC. No BC code is touched in this ticket.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `ksma.shared.money.Money(amount: int, currency: Currency)` value object with `+`, `-`, `<`, `==` operators; pilot `Currency` enum contains only `IDR`; raises `ValidationError` on negative amounts.
- [ ] `ksma.shared.time.Clock` protocol with `now()`, `today()`, and a `SystemClock` default; tests use `freezegun` to pin time.
- [ ] `ksma.shared.auth.jwt` encodes/decodes JWT with claims `sub`, `membership_id`, `tenant_id`, `role_code`, `authz_version`, `iat`, `exp`, `iss`, `aud`; rejects mismatched `authz_version`.
- [ ] `ksma.shared.auth.password` Argon2id hash + verify wrappers; rejects empty passwords; never returns the hash in any error payload.
- [ ] `ksma.shared.idempotency.IdempotencyStore` provides `lookup_or_reserve(tenant, requester, action, key, request_hash) -> (cached_result_ref | None)`; same key + same hash returns the cached result; same key + different hash raises `IdempotencyConflictError`.
- [ ] `ksma.shared.outbox.enqueue(session, event, aggregate_ref)` writes an `OutboxEvent` row in the same transaction as the caller's business mutation; raises if no transaction is active.
- [ ] `ksma.shared.outbox.run` worker CLI loop polls `WHERE dispatch_state='pending' ORDER BY occurred_at FOR UPDATE SKIP LOCKED LIMIT :n`, dispatches each to in-process `bus`, marks `dispatch_state='dispatched'`, and writes `ProcessedEvent` per consumer.
- [ ] `ksma.shared.tenancy.TenantContext` Pydantic model and `tenant_context_dep` FastAPI dependency; reads JWT, builds the context, binds to `request.state`.
- [ ] `ksma.shared.persistence.session_scope()` async context manager sets `SET LOCAL app.current_tenant = :tid` (or `SET LOCAL app.bypass_rls = 'on'` for seeds); `ksma_current_tenant()` and `ksma_bypass_rls()` plpgsql functions are defined in migration `0001`.
- [ ] `ksma.shared.errors.DomainError` hierarchy maps to HTTP statuses via the existing handler in `ksma.main`; common codes `INVALID_VALUE`, `REQUIRED_FIELD`, `REFERENCE_SCOPE_MISMATCH`, `STATE_CONFLICT`, `VERSION_CONFLICT`, `CAPACITY_EXCEEDED`, `ACCESS_DENIED`, `IDEMPOTENCY_CONFLICT`, `POLICY_NOT_CONFIGURED`.
- [ ] `ksma.shared.rbac.ACTION_REGISTRY` is the single `ActionCode(str, Enum)` of 73 codes; `can(actor, action_id, target) -> Decision` evaluates ONE grant at a time (R02); `require_action(ActionCode.X, scope_kind=None)` FastAPI dependency returns `TenantContext` or raises `AccessDeniedError` with no object payload.
- [ ] `ksma.shared.audit.audit(action_id, *, reason_required=False, change_ref_builder=None)` decorator writes `AuditRecord(actor, action_id, object_ref, occurred_at, change_ref?, reason?, authorization_ref, correlation_id)` inside the same transaction as the wrapped call.
- [ ] `ksma.shared.events.bus().subscribe(event_type, handler)` and `bus().dispatch(event)` for in-process event delivery; the worker subscribes the same handlers.
- [ ] `ksma.shared.attachments.AttachmentStorage` port with `LocalAttachmentStorage` (dev) and `S3AttachmentStorage` (prod, MinIO in docker-compose) adapters; `storage_key` is server-generated opaque.
- [ ] `structlog` JSON logger configured with `correlation_id` contextvar; middleware reads `X-Correlation-Id` header (or generates UUID4); echoes it on the response.
- [ ] Unit tests for `Money` arithmetic, `Clock` freezegun, JWT encode/decode, Argon2id round-trip, `IdempotencyStore` lookup/reserve/conflict, action-registry lookup, RLS GUC set/get.