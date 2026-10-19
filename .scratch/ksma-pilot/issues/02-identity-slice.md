# 02: Identity slice — login, refresh, recovery, grants end-to-end

**What to build:** A complete, demoable identity flow: an OP creates a tenant + unit + admin account; that admin logs in, refreshes the JWT, recovers the password via a time-bounded token, and assigns a grant to another user. After this ticket, a developer can run a curl-based demo of the auth lifecycle.

**Blocked by:** 00, 01

**Status:** ready-for-agent

- [ ] `POST /api/v1/auth/login` accepts `{email, password, tenant_id?}`, returns `{access_token, refresh_token, expires_in}`. Argon2id-verifies, rotates `authz_revision` if it differs from the JWT claim, increments failed-attempt counter on miss (cap at 5/min/account).
- [ ] `POST /api/v1/auth/refresh` accepts `{refresh_token}`, validates SHA-256 hash against `RefreshToken`, rotates (old `status='rotated'`, new row issued); replay of a rotated token revokes the entire family and returns 403.
- [ ] `POST /api/v1/auth/logout` revokes the current refresh token (`revoked_reason='user_logout'`); access token expires naturally.
- [ ] `POST /api/v1/auth/recover/start` accepts `{email}`, creates a `RecoveryToken` with 30-min TTL, returns `{recovery_id}` (never the token); rate-limited at 3/hour/email.
- [ ] `POST /api/v1/auth/recover/complete` accepts `{recovery_id, token, new_password}`, consumes the token, sets the new Argon2id hash, bumps `authz_revision`, returns new pair.
- [ ] `POST /api/v1/tenants` (PLT-A01) creates a `Tenant` with display_name, default_timezone, branding; OP only.
- [ ] `POST /api/v1/units` creates an `EducationUnit` with `unit_code` unique per tenant.
- [ ] `POST /api/v1/accounts/invite` (PLT-A05) creates an `Account`, sends/recovery flow, returns a one-time activation URL.
- [ ] `POST /api/v1/memberships` links an `Account` to a tenant with `valid_from` and optional `valid_until`.
- [ ] `POST /api/v1/grants` creates an `AccessAssignment` with `role_code`, `action_ids[]`, JSONB `scope`, `valid_from`, optional `valid_until`; rejects self-grant outside the granter's mandate (R03).
- [ ] `POST /api/v1/grants/{id}/revoke` sets `status='revoked'`, bumps `Account.authz_revision` for the grantee.
- [ ] `GET /api/v1/audit-trail?unit=&actor=&action_id=&from=&to=` returns paginated `AuditRecord` rows (PLT-A07); tenant + unit scoped.
- [ ] Every endpoint returns 401 without a valid JWT, 403 with no object payload when the grant fails, never leaks participant/object existence.
- [ ] Integration tests covering login → refresh → rotation → replay-revokes-family → recover-start → recover-complete → grant-then-revoke-then-active-session-sees-401.