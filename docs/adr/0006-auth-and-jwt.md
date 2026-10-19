# ADR-0006: Authentication, JWT, and Recovery

- **Status**: Accepted (subject to Q08 finalisation)
- **Date**: 2026-10-20
- **Supersedes**: none
- **See also**: GLOSSARY (Account, Membership, Authz revision).

## Context

NFR02: encrypted transport, no plaintext credentials, time-bounded recovery tokens, session
revocation. Access matrix: account status, membership status, `authz_revision` invalidation.

## Decision

- **Password storage**: Argon2id (`argon2-cffi`) with sensible defaults; per-row salt; no
  password to the audit log.
- **Login**: `POST /routes/auth/login` with `{email, password, tenant_id?}`. Returns
  `{access_token, refresh_token, expires_in}`. Access token TTL = 15 min; refresh token TTL = 7
  days. Refresh tokens are stored hashed (SHA-256 of opaque token) in `RefreshToken`.
- **JWT claims**: `sub` = account_id, `membership_id`, `authz_version`, `iat`, `exp`, `iss`,
  `aud`. Mismatch on `authz_version` forces a refresh; mismatch on revocation removes the
  session.
- **Refresh rotation**: every refresh issues a new pair and revokes the previous refresh token
  (`status = rotated`). Replay of a rotated token = `403` and revokes the whole family.
- **Recovery**: `POST /routes/auth/recover/start` → creates `AccountRecoveryToken` with 30-min
  TTL. `POST /routes/auth/recover/complete` with `{token, new_password}` → marks account active,
  increments `authz_revision`.
- **Logout**: `POST /routes/auth/logout` revokes the refresh token. Access token expires
  naturally.
- **Authz invalidation**: every write to `AccessAssignment`, `Membership`, `Account`,
  `SupportGrant` increments `Account.authz_revision` (or `Membership.authz_revision`). Active
  requests re-check authority at fetch time (R02 / R03).

## Consequences

- Argon2id is OWASP-recommended.
- Refresh rotation limits replay damage.
- Recovery tokens are time-bounded single-use.
- `authz_revision` makes revocation apply on next request without central cache invalidation.

## Deferred (Q08 still open)

- Multi-factor authentication (not yet required by NFR02).
- Account creation policy for `ServicePrincipal` (env-driven only).
- Self-service signup (PRD allows it for some flows; not P0).
- WebAuthn / passkeys (P1+).