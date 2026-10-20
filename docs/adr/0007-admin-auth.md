# ADR-0007: Admin-Path Authentication (Static `KITAPANDU_ADMIN_TOKEN`)

- **Status**: Accepted
- **Date**: 2026-10-20
- **Ticket**: P4.1 sub-area #3 (container hardening), issue #31
- **Supersedes**: none
- **See also**: ADR-0001 (pilot scope), ADR-0005 (seed + tenancy bootstrap), ADR-0006
  (auth + JWT), `src/ksma/shared/auth/admin_token.py`,
  `src/ksma/shared/auth/admin_token_middleware.py`.

## Context

The `/api/v1/admin/*` routes exist for **platform-operator** actions that sit outside the
account/membership model of ADR-0006:

- `POST /api/v1/admin/tenants` — PLT-A01, create a tenant (the bootstrap call a school
  makes before any user exists).
- `POST /api/v1/admin/tenants/{id}/activate`, `POST /api/v1/admin/tenants/{id}/units` —
  tenant activation + education-unit setup.
- `POST /api/v1/admin/accounts` — PLT-A03/04, platform-admin account/grant management.

None of these can be expressed as "a logged-in user with a grant": tenant bootstrap happens
*before* any account exists, and PLT-A03/04 are performed by the KitaPandu operator, not by
the tenant. In the pilot these routes were reachable by anyone who could route a request to
the container. That is acceptable on a laptop and unacceptable the moment a staging or
production load-balancer points at the same image ("container hardening" in the P4.1 spec).

Two constraints pull in opposite directions:

1. **Staging/production must not expose an open admin surface.**
2. **Local development must stay frictionless** — a developer running `uvicorn
   ksma.main:app` should not have to mint and paste a token before any work.

A full identity integration (service principals with their own JWTs, mTLS, per-operator
audit) is the right end state, but it is larger than the pilot and would land as a large
unreviewed change on the critical path of the acceptance run.

## Decision

We gate `/api/v1/admin/*` behind a **static bearer token supplied by the environment**.

- **Config**: one new setting, `admin_token: str | None = ""`, read from the env var
  **`KITAPANDU_ADMIN_TOKEN`** (prefix fixed by ADR-0001). No DB table, no migration, no
  bootstrap chicken-and-egg: the operator reads the same secret out of secret management
  that they use to deploy.
- **Enforcement**: an ASGI middleware (`AdminTokenMiddleware`, `BaseHTTPMiddleware`) runs
  before routing and matches every path beginning with `/api/v1/admin/`. Routes are
  untouched — the gate is additive, and nothing under `/api/v1/admin/` is removed.
- **Credential shape**: `Authorization: Bearer <token>`. Scheme match is exact
  (`Bearer ` with the trailing space); the scheme is case-sensitive, matching RFC 9110's
  `auth-scheme` grammar, so `bearer …` is rejected rather than silently accepted.
- **Comparison**: the presented token is compared to the configured token with
  **`hmac.compare_digest`** — constant time. A `==` comparison short-circuits on the first
  differing byte and leaks the token prefix through response latency; `compare_digest` does
  not, so the timing channel is closed. (A test asserts `compare_digest` is the function
  actually used, so a future refactor to `==` fails CI.)
- **Failure**: `401` with the standard error envelope
  `{ code: "ADMIN_TOKEN_REQUIRED", message, correlation_id }`. The response never echoes the
  presented token, the expected token, or any hint about which part failed — a missing
  header and a wrong token are byte-identical apart from `correlation_id`.
- **Dev mode**: when `KITAPANDU_ADMIN_TOKEN` is unset or empty, the policy is
  "unconfigured" and every request passes through. This is announced **once at startup** —
  `load_policy()` logs a WARNING during `create_app()` and the policy is frozen on
  `app.state.admin_token_policy`. It is deliberately *not* per-request logging, and it is
  deliberately loud: an open admin surface should be visible in the startup log of every
  dev box and every misconfigured deployment.

### Why the policy is a startup snapshot

`load_policy()` runs once, in `create_app()`, and the resulting frozen
`AdminTokenPolicy(token, is_configured)` dataclass is stashed on `app.state`. Consequences:

- the WARNING is one-shot, not per request (a per-request log line would be both a log
  flood and a hint that the endpoint exists);
- the token is read from settings exactly once, so a later mutation of `app.state` cannot
  silently re-open the surface mid-flight;
- tests can build a policy directly and exercise the pure compare without HTTP.

## Consequences

- **Operators MUST set `KITAPANDU_ADMIN_TOKEN` for staging and production.** Unset means
  open, and the startup log says so. There is no "fail closed in production" flag in the
  pilot: the explicit, loud default is dev-friendly, and the deployment checklist carries
  the obligation.
- **Rotation is an operator action**: change the secret in secret management and restart
  the API (the policy is a startup snapshot — it is not hot-reloaded). Multiple deployments
  sharing one secret will accept each other's tokens, so per-environment values.
- **Blast radius of a leaked token**: anyone holding it can create tenants, activate them,
  and create accounts. It is a bootstrap credential, not a user credential — it must live in
  secret management, must not appear in CI logs, and should be rotated after any staging
  demo. Route-level RBAC (`/me/*` routes) remains the authorization layer for tenant work;
  this token is authentication for the operator plane only.
- **Positive**: the gate is 100 lines of dependency-free Python with no DB round-trip on the
  hot path, and the 401 happens before the request reaches a handler, so a rejected caller
  cannot probe tenant IDs or provoke writes.
- **Negative**: one shared secret means no per-operator attribution. A 401 audit line
  records method + path + correlation id, not *who* — accepted for the pilot, and the reason
  the alternatives below are listed for later.
- **Operational note**: `GET /api/v1/admin/tenants` is not a registered route (PLT-A01 is
  POST-only). The integration test therefore asserts "no 401" (the router answers, e.g.
  `405`) as the pass-through signal rather than a literal `200`.

## Alternatives considered

- **mTLS between the operator client and the API.** Strongest transport-level identity and
  no shared secret to leak. Deferred: it requires client-cert provisioning, a cert
  authority story, and per-client certificate mapping, none of which the pilot deployment
  has. Revisit when the first real external operator integration ships.
- **Separate identity-platform tokens** (a service-principal JWT minted through the
  ADR-0006 flow, with the operator's own `authz_revision` and audit trail). Out of scope
  for the pilot — it is a production feature that depends on tenant bootstrap being solved
  first, which is exactly the chicken-and-egg problem this ADR sidesteps. This is the
  natural successor to the static token.
- **No authentication** (the pilot default). Rejected for every non-dev environment: it
  turns any reachable port into tenant-creation capability, and tenant rows are the root of
  every RLS policy in the schema.
- **Per-route dependencies instead of middleware.** Rejected: four admin routes today and
  more tomorrow; forgetting the dependency is a silent hole, whereas the middleware's path
  prefix makes the guarded surface explicit in one place. Middleware also fails *before* the
  handler, so it cannot be bypassed by a route added later.