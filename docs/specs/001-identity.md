# Spec 001 — Identity Bounded Context

> `Tenant`, `EducationUnit`, `Account`, `Membership`, `AccessAssignment`, `ServicePrincipal`,
> `SupportGrant`, `PolicyVersion`, `RefreshToken`, `RecoveryToken`, `AuditRecord`.
> PRD module codes PLT01–04, OPS-A05/06, X-A03/05.

## Problem Statement

Before any participant, class, or invoice exists, the system needs a way to identify who
is logging in, what tenant they're operating in, what role they hold, and what actions
that role permits. Without this context, the rest of the product has nothing to attach
permissions to, and cross-tenant isolation has no boundary.

The previous session already implemented the bulk of this BC — `identity/*` is the
largest folder in the codebase (10 files, 3,139 lines). This spec records the
contract that the implementation must satisfy, identifies what's still missing,
and locks the seam between identity and every other BC.

## Solution

A self-contained bounded context that owns:

- The `Tenant` row (root of the tenant isolation boundary, ADR-0002).
- The `EducationUnit` row (operational sub-context within a tenant).
- The `Account` row (global G-scope login identity; not per-tenant).
- The `Membership` row (account ↔ tenant, with status + validity).
- The `AccessAssignment` row (role + action codes + JSONB scope + validity).
- The `ServicePrincipal` row (non-interactive global identity).
- The `SupportGrant` row (time-bounded operator assistance per ADR-0003).
- The `PolicyVersion` row (versioned operational config per ADR-0001 §5).
- The `RefreshToken` and `RecoveryToken` tables (ADR-0006 auth).
- The `AuditRecord` table (append-only, R22).

Every other BC depends on identity for `TenantContext` and for `require_action`.
No other BC depends on identity's *internal* tables directly — reads of
`AccessAssignment` rows for ACL evaluation go through the `core.access` shared kernel.

## User Stories

### Tenant and unit lifecycle
1. As **OP**, I want to create a `Tenant` with a display name, default timezone, and
   branding, so that a new customer can begin setup.
2. As **OP**, I want to create an `EducationUnit` inside a tenant with a unique `unit_code`,
   `institution_type`, `education_levels[]`, and `timezone`, so that the unit shows up
   in every admin's context switcher.
3. As **OP**, I want `unit_code` to be unique per tenant (not globally), so that two
   tenants can both have a "MAIN" unit.
4. As **OW**, I want to update the tenant's display name and branding, so that the portal
   reflects my brand — but only via `PLT-A04` and only within my tenant.
5. As **OW**, I want to update my unit's `name` and `contact`, but I want changing the
   `institution_type` to require a re-validation step (Q01), so that we don't quietly
   change what kind of school we are.
6. As **OW**, I want a deactivated unit (`status='inactive'`) to remain queryable for
   historical reports, so that closed cohorts aren't lost.

### Account and authentication
7. As a **new user**, I want to be invited by email and set my own password, so that I
   don't share a platform-wide credential.
8. As a **new user**, I want my password to be Argon2id-hashed with a per-row salt, so
   that a database leak doesn't compromise my password.
9. As a **new user**, I want to log in with email + password and receive a 15-minute JWT
   + 7-day refresh token, so that I can use the SPA without re-prompting.
10. As a **user**, I want my refresh token to be rotated on every refresh, so that a
    stolen refresh token has at most one use before it's revoked.
11. As a **user**, I want to start account recovery with my email and receive a
    time-bounded single-use token, so that I can regain access if I forget my password.
12. As a **user**, I want my account to be deactivated (not deleted) when I leave, so
    that historical audit rows still attribute changes to me.
13. As a **user**, I want any change to my account, membership, or grants to bump
    `authz_revision` and invalidate my current session, so that revoked rights stop
    working on the next request.

### Memberships and grants
14. As **AM**, I want to invite a person into a tenant and assign them one role
    (e.g. `AC`), so that the new admin can begin work without me logging in as them.
15. As **AM**, I want to grant a user a second role in a different unit, so that one
    person can be `AC` in unit A and `IN` in unit B (whole-grant evaluation).
16. As **AM**, I want to revoke a single grant without touching the user's other
    grants, so that I can narrow their access without churning their login.
17. As **AM**, I want every grant to have a `valid_from` and optional `valid_until`,
    so that I can schedule "temporary admin cover" without remembering to revoke it.
18. As **AM**, I want an attempt to self-approve an extended grant to be rejected, so
    that I can't accidentally elevate myself (delegation limit, R03).

### Platform support
19. As **AM**, I want to issue a `SupportGrant` with a ticket reference, a scope
    (which dataset / which columns), and an explicit expiry, so that platform operators
    can investigate a customer issue without seeing more than the ticket needs.
20. As **OP**, I want my support grant to record the ticket, the granter, the customer,
    the object/dataset, and the action, so that my access is auditable per session.
21. As **OP**, I want my help session to be labeled and audited, so that the customer
    sees clearly when a platform operator is in their data.
22. As **OP**, I want my operator access to expire automatically at `valid_until`, so
    that I cannot extend my own help session without re-approval.

### Policy versioning
23. As **OW**, I want to create a `PolicyVersion` with `policy_domain='finance'`,
    a unit scope, a version number, an effective range, a typed `ruleset`, and an
    `approval` decision, so that finance rules can change without rewriting history.
24. As **OW**, I want a new policy version's `effective_range` to be non-overlapping
    with the prior version of the same domain and scope, so that two policies can't
    apply at once.
25. As **OW**, I want a `PolicyVersion` in `status='draft'` to not affect any business
    action, so that I can stage a change safely.
26. As **OW**, I want a retired `PolicyVersion` to remain referenced by historical
    records, so that audit trails can show which policy was in force at the time.

### Audit
27. As **AU**, I want to read the audit trail for my tenant, filtered by unit, actor,
    action, and time, so that I can answer "who cancelled this participant?"
28. As any **user**, I want my denial responses to never leak participant name or object
    existence, so that I can't enumerate records by trying random IDs (NFR08).
29. As **operator platform (OP)**, I want my automated jobs to record `actor` as
    `ServicePrincipal('code')`, so that machine actions are traceable too.
30. As any **user**, I want a row in `AuditRecord` to record `correlation_id` from the
    request, so that the row links to the originating request trace.

## Implementation Decisions

### Schema (already migrated, see `alembic/versions/0002_identity_root.py`)
- `tenant(id, display_name, service_status, default_timezone, active_policy_id?,
  activated_at?, created_at, created_by, updated_at, updated_by)`.
- `education_unit(id, tenant_id, unit_code, name, institution_type, education_levels[],
  location?, status, timezone, …)`. Unique `(tenant_id, unit_code)`.
- `account(id, auth_subject, auth_namespace, display_name, verified_contact?, status,
  password_hash?, authz_revision, …)`. Unique `(auth_namespace, auth_subject)`.
- `membership(id, tenant_id, account_id, status, valid_from, valid_until?, invitation_ref?,
  revocation?, …)`. Unique `(tenant_id, account_id)`.
- `access_assignment(id, tenant_id, membership_id?, service_principal_id?, role_code,
  action_ids[], scope JSONB, valid_from, valid_until?, status, policy_id,
  authorization JSONB, revocation?, …)`.
- `service_principal(id, principal_code UNIQUE, purpose, environment, credential_ref,
  status, rotation_due_at?, created_at)`.
- `policy_version(id, tenant_id, policy_domain, unit_scope_id?, program_scope_id?,
  version_no, effective_range tstzrange, ruleset JSONB, approval?, status, …)`.
  Unique `(tenant_id, policy_domain, version_no)`.
- `support_grant(id, tenant_id, operator_account_id, ticket_ref, scope JSONB,
  approved_by JSONB, valid_from, valid_until, status, revocation?, …)`.
- `refresh_token(id, tenant_id, account_id, membership_id, token_hash UNIQUE, issued_at,
  expires_at, revoked_at?, revoked_reason?, replaced_by_id?)`.
- `recovery_token(id, account_id, token_hash UNIQUE, purpose, issued_at, expires_at,
  consumed_at?, consumed_ip?)`.
- `audit_record(id, tenant_id, actor JSONB, action_id, object_ref JSONB, occurred_at,
  change_ref?, reason?, authorization_ref JSONB, correlation_id)`. Append-only.
- All U-scope tables have `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`
  + policy `USING/WITH CHECK (ksma_bypass_rls() OR tenant_id = ksma_current_tenant())`.

### Service layer (`ksma.identity.application`)
- `create_tenant(ctx, *, display_name, default_timezone, branding?) -> Tenant` — OP only.
- `create_unit(ctx, *, unit_code, name, institution_type, education_levels, timezone, location?) -> EducationUnit`.
- `invite_account(ctx, *, email, display_name, password?, initial_role_codes: list[(role, unit_id)])`
  — sends recovery/activation token if no password.
- `set_password(ctx, *, account_id, password_hash)` — bumps `authz_revision`.
- `login(*, email, password, tenant_id?) -> {access_token, refresh_token, expires_in}`.
- `refresh(*, refresh_token_hash) -> Hash` — rotates; replay of rotated → revoke family.
- `recover_start(*, email) -> RecoveryToken` — 30-min expiry.
- `recover_complete(*, token_hash, new_password) -> Hash` — consumes, bumps `authz_revision`.
- `logout(*, refresh_token_hash)` — marks `revoked_reason='user_logout'`.
- `create_membership(ctx, *, account_id, valid_from, valid_until?)`.
- `assign_grant(ctx, *, membership_id, role_code, action_ids, scope, valid_from, valid_until?) -> AccessAssignment`.
  Rejects self-grant outside mandate (R03).
- `revoke_grant(ctx, *, grant_id, reason)` — sets `status='revoked'`, bumps
  `Account.authz_revision` for the grant's account.
- `issue_support_grant(ctx, *, operator_account_id, ticket_ref, scope, valid_until) -> SupportGrant`
  — `X-A03` only; requires explicit `valid_until`.
- `create_policy_version(ctx, *, policy_domain, version_no, effective_range, ruleset, scope?) -> PolicyVersion`.
- `audit(action_id, object_ref, *, change_ref?, reason?)` — writes `AuditRecord`
  inside the same transaction as the business mutation.

### Router layer (`ksma.identity.interface`)
- `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`,
  `POST /auth/recover/start`, `POST /auth/recover/complete`.
- `POST /tenants`, `GET /tenants/{id}`, `PUT /tenants/{id}/branding` (PLT-A04).
- `POST /units`, `GET /units/{id}`, `PUT /units/{id}`, `GET /units`.
- `POST /accounts/invite` (PLT-A05), `POST /accounts/{id}/deactivate` (PLT-A06).
- `POST /memberships`, `GET /memberships`, `POST /memberships/{id}/revoke`.
- `POST /grants`, `GET /grants`, `POST /grants/{id}/revoke`.
- `POST /support-grants`, `POST /support-grants/{id}/revoke` (X-A03).
- `POST /policies`, `GET /policies`, `POST /policies/{id}/approve`.
- `GET /audit-trail?unit=&actor=&action_id=&from=&to=` (PLT-A07).
- Every endpoint protected by `require_action(ActionCode.PLT_A0x)` matching the access
  matrix.

### GUC helpers (in `src/ksma/shared/persistence/__init__.py`)
- `ksma_current_tenant()` — `SELECT current_setting('app.current_tenant', true)::uuid`.
- `ksma_bypass_rls()` — `SELECT coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'`.
- `ksma_uuid()` — `SELECT gen_random_uuid()` (used as DEFAULT for all PKs).
- `set_tenant_context(session, tenant_id)` — runs `SET LOCAL app.current_tenant = :tid;`
  inside the same transaction.

### Seams
- **Inbound**: every BC depends on `TenantContext` and `require_action` (both in `core.access`).
- **Outbound**: identity depends only on `shared/*`. It must not import from any other BC.
- **ACL exports**: identity exposes `core.access.acl.get_grants_for_account(account_id)` for
  the audit-trail projection (no other BC needs this; the function lives in shared kernel).

## Testing Decisions

What makes a good test for identity:
- Asserts the **API response code, body, and side-effects in the DB**, not the log output.
- Cross-tenant: AC of tenant A asks for `/units/{id_of_B}` → 403 with empty body.
- Self-grant: AM tries to grant themselves `PLT-A05` outside their mandate → 403.
- AuthZ invalidation: AM revokes AC's grant → AC's next request returns 401 with
  `authz_version` mismatch hint.
- Refresh rotation: refresh once → returns new pair; replay old → 403 + family revoked.
- Recovery: token expires after 30 min (freezegun); wrong token → 401; correct → password
  set, `authz_revision` bumped.
- Policy: two `PolicyVersion` rows with overlapping `effective_range` for the same
  `(tenant_id, policy_domain, unit_scope_id)` → second INSERT rejected by exclusion
  constraint (`btree_gist`).
- Audit: any `POST /grants` writes exactly one `AuditRecord` row with `actor.account_id`,
  `action_id='PLT-A05'`, `object_ref={kind:'access_assignment', id:…}`.

What's covered by existing tests in the codebase: none. This spec implies a fresh
`tests/integration/test_identity_*.py` suite and `tests/unit/test_auth_*.py`.

## Out of Scope

- Self-service signup (PRD defers this; pilot is admin-assisted).
- Multi-factor authentication (Q08).
- WebAuthn / passkeys.
- OAuth2/OIDC with external IdP.
- Account merge across tenants (BR02 forbids identity merge; future migration tool).

## Further Notes

- The existing `src/ksma/identity/` implementation is ~3,100 lines and already
  covers the schemas, repositories, application services, and the router. The
  remaining work is:
  1. Verify the router wires `require_action` correctly for PLT-A05/06 etc.
  2. Add unit tests for `core.auth` (Argon2id, JWT, rotation).
  3. Add integration tests for cross-tenant isolation (V01).
  4. Implement `ksma_uuid()` / `ksma_current_tenant()` SQL functions in migration
     `0001_extensions_and_tenant_root.py` (already present per the migration code I
     audited; needs to be verified at runtime).
  5. Add `policy_version` exclusion constraint for non-overlapping effective ranges.