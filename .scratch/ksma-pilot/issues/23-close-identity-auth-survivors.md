# 23: Kill the 39 `identity/application` + `shared/auth/*` survivors — pin hash / grant-revocation / JWT / `tenant_context_dep` boundaries

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for `ksma.identity.application` (29 surviving mutations) and the `ksma.shared.auth` family (`auth/__init__.py`, `auth/admin_token.py`, `auth/admin_token_middleware.py`, `auth/dependencies.py`, `auth/jwt.py` — ~10 surviving mutations). Each test pins a boundary from ADR-0006 (auth and JWT) and the Q08 contract: `== → !=` flips on password-hash comparison, grant-revocation timestamps, JWT claim equality, and the FastAPI dependency seam (`tenant_context_dep`, `load_active_grants`, `require_action`). Killing the 39 surviving mutations across these modules is the end-to-end behaviour this ticket makes work.
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from `ksma.identity.application` and `ksma.shared.auth.*` reduced to 0.
- [ ] `verify_password`, `hash_password`, `needs_rehash`, `issue_access_token`, `issue_refresh_token` each have a positive assertion on the result's typed fields (hash digest, JWT claim set), not just round-trip equality (per spec #16 user story #8).
- [ ] `tenant_context_dep` has parametrised tests over every JWT shape — missing token, expired token, wrong-tenant token, `authz_version` mismatch — per `docs/specs/000-umbrella-platform.md` §Testing Decisions.
- [ ] `load_active_grants` and `require_action` have positive assertions on the granted-actions set and the 401/403 boundary.
- [ ] No production code under `ksma.identity` or `ksma.shared.auth` is modified.
- [ ] `pytest tests/unit -q` exits 0.
