"""KSMA shared kernel.

Cross-cutting concerns used by every bounded context:

- ``auth``        — argon2 password hashing, JWT issue/decode, refresh tokens,
                    ``Account.authz_revision`` invalidation, recovery tokens.
- ``money``       — ``Money`` value object (integer smallest unit + currency), no
                    floating point, ``InsufficientFundsError`` etc.
- ``tenancy``     — ``TenantContext`` value object, RLS GUC helpers
                    (``set_current_tenant``, ``bypass_rls``), per-request session.
- ``rbac``        — ``ACTION_REGISTRY`` (73 codes), whole-grant resolver
                    (``evaluate(actor, action_id, target) -> Decision``), scope
                    JSONB shape.
- ``idempotency`` — ``IdempotencyRecord`` table, ``Idempotency-Key`` header parsing,
                    dedup-by-(tenant, requester, action, key).
- ``outbox``      — ``OutboxEvent`` writer (atomic with business tx),
                    ``python -m ksma.shared.outbox.run`` worker using
                    ``FOR UPDATE SKIP LOCKED``.
- ``errors``      — ``DomainError`` hierarchy → HTTP status + safe payload
                    ``{ code, message, field_path?, correlation_id }``.
- ``events``      — ``DomainEvent`` base + in-process dispatcher; bridges outbox rows
                    to handlers per event type.
- ``time``        — ``Clock`` protocol (injectable for tests), IANA timezone helpers.
- ``persistence`` — SQLAlchemy 2.x ``AsyncEngine`` + ``AsyncSession``, RLS-aware
                    session factory, ``DeclarativeBase``.

All design decisions traced in ``/docs/adr/0001-architecture-foundations.md``.
"""