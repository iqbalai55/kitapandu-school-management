# 30: Follow-up to the kill-survivor-gap wave — auth/* residual + production defects

**Parent spec:** #16 (Kill the 337-survivor gap)
**Opened by:** #28 (Document and suppress genuinely-unreachable mutants)

**What to build:** Two blocks that the T17–T27 wave deliberately left on the table, plus the production defects the wave surfaced while writing tests against the real code paths.

1. **`shared/auth/*` residual mutation sites.** T27 reported **9 `SURVIVED`** in the auth family — `dependencies.py` 7, `admin_token.py` 1, `admin_token_middleware.py` 1. These were *not* in the original 337 because they were `UNCOVERED` at baseline time (the baseline reported **0 uncovered**, so the whole family was invisible until T23 added tests that made its mutation sites reachable). T23's full-file targeted run killed `is_authorized 9/9` and `load_policy 3/3` on a 33-line subset; the broader scan-level enumeration below shows the family carries ~35 mutation sites in total, the rest still unreached. This ticket is the next-wave scope for the auth family.
2. **Production defects found during the wave.** Seven real bugs, none fixed, all documented below with a fix direction. Two of them (D1, D2) are latent production crashes / authz-versioning bugs that the mutation work exposed.

**Blocked by:** None. Independent of #28 (which lands in the same commit).
**Status:** ready-for-agent

---

## Part 1 — `shared/auth/*` residual survivors (9 `SURVIVED` + ~26 unaddressed sites)

### Provenance

T23 ran a **targeted full-file** mutation run and reported:

```
is_authorized 9/9, load_policy 3/3, ... 0 SURVIVED
```

against a specific 33-line subset of `shared/auth/*`. T27's **scan-level** run afterwards still reported 9 `SURVIVED` across the family. The two numbers differ because T23's targeted run mutated only lines its subset covered; the scan-level run mutates every site in the file, including ones no test can currently reach.

> **Note for the agent picking this up:** `.metrics/mutator.txt` is not committed, so the exact per-line survivor map is not recoverable from the tree. Re-derive it first with
> `mutator --since-last-run --max-workers 2 --no-coverage --test-command "...tests/unit..."` scoped to `src/ksma/shared/auth/`, then diff against the table below. The table lists every **mutation-bearing line** in the family (with the operators mutmut generates), so any `SURVIVED` line you see will map to a row.

### 1a. `shared/auth/dependencies.py` — 7 `SURVIVED` (the largest share)

| Line | Statement | Mutation sites | Reported status |
| ---: | --- | --- | --- |
| 57 | `if creds is None or not creds.credentials:` (`bearer_token`) | `is None` → `is not None`; `not` → (dropped); `or` → `and` (3) | unaddressed |
| 60 / 71 / 76 | `detail={"code": "AUTHENTICATION_REQUIRED" / "TOKEN_EXPIRED" / "TOKEN_REVOKED"}` | string literal (2 numeric-status siblings) | unaddressed |
| 71 / 76 | `status_code=status.HTTP_401_UNAUTHORIZED` | 401 → 402 (2) | unaddressed |
| 102 | `if not sub or not tenant_id_raw or authz_rev is None:` | 3× `not`, 2× `or`, 1× `is None` (6) | unaddressed |
| 104 / 115 / 129 / 134 | `status_code=status.HTTP_401_UNAUTHORIZED` (4 sites) | 401 → 402 (4) | unaddressed |
| **124** | `select(AccountRow).where(AccountRow.id == sub)` | `==` → `!=` | **UNCOVERED at baseline — known unkillable with the current fake (see D7)** |
| 127 | `if account is None or account.status != "active":` | `is None`, `or`, `!=` (3) | unaddressed |
| 132 | `if int(account.authz_revision) != int(authz_rev):` | `!=` → `==` (1) | unaddressed |
| 141–143 | `MembershipRow.account_id == sub, MembershipRow.tenant_id == tenant_id` | 2× `==` → `!=` (2) | unaddressed |
| 147 | `if membership is None or membership.status != "active":` | `is None`, `or`, `!=` (3) | unaddressed |
| 149 | `status_code=status.HTTP_403_FORBIDDEN` | 403 → 404 (1) | unaddressed |
| 158 | `(m.get("roles") or [])` | `or` → `and` (2) | unaddressed |
| 161 | `(sublist if isinstance(sublist, list) else [])` | `else` branch condition (2) | unaddressed |

**T27's 7 survivors are drawn from the unaddressed rows above** — most plausibly the `or`/`not` cluster at 102 (six overlapping mutants where the short-circuit order makes several indistinguishable), the `!=` rows at 127/147, and the `int(...) != int(...)` at 132. Re-run to confirm which seven.

### 1b. `shared/auth/admin_token.py` — 1 `SURVIVED`

| Line | Statement | Mutation sites | Reported status |
| ---: | --- | --- | --- |
| 45 | `if not token:` (`load_policy`) | `not` (1) | KILLED (T23, `load_policy 3/3`) |
| 50 | `return AdminTokenPolicy(token=None, is_configured=False)` | `False` → `True` (1) | KILLED (T23) |
| 51 | `return AdminTokenPolicy(token=token, is_configured=True)` | `True` → `False` (1) | KILLED (T23) |
| 64 | `if not policy.is_configured or policy.token is None:` (`is_authorized`) | `not`, `is None`, `or` (3) | KILLED (T23, `is_authorized 9/9`) |
| 65 | `return True` | `True` → `False` (1) | **SURVIVED (T27)** — see D3 |
| 66 / 68 / 71 | `if not authorization_header:` / `if not authorization_header.startswith(BEARER_PREFIX):` / `if not presented:` | 3× `not` (3) | KILLED (T23) |
| 67 / 69 / 72 | `return False` | 3× `False` → `True` (3) | KILLED (T23) |
| 73 | `return hmac.compare_digest(presented, policy.token)` | `True` → `False` (1) | KILLED (T23) |

The one survivor is almost certainly **line 65**: `is_authorized(AdminTokenPolicy(token=None, is_configured=True), header)` returns `True` for **any** header, so flipping `return True` to `return False` is only observable if a test constructs that half-configured policy — which is exactly the state D3 describes as a silent bypass, and no test currently constructs it.

### 1c. `shared/auth/admin_token_middleware.py` — 1 `SURVIVED`

| Line | Statement | Mutation sites | Reported status |
| ---: | --- | --- | --- |
| 38 | `DEFAULT_POLICY = AdminTokenPolicy(token=None, is_configured=False)` | `False` → `True` (1) | **SURVIVED (T27)** — module constant, never asserted |
| 66 | `if request.url.path.startswith(ADMIN_PATH_PREFIX):` (`dispatch`) | 1 | **SURVIVED (T27)** |
| 68 | `if not is_authorized(policy, request.headers.get(HEADER_NAME)):` | 1 | unaddressed |
| 85 | `if policy.is_configured:` (`init_admin_token_policy`) | 1 | unaddressed |

Line 66 is the single `dispatch` site: flipping the guard to `not ...startswith(...)` inverts the whole admin gate, and every current test hits the middleware through paths where both polarities are exercised only indirectly.

### 1d. `shared/auth/__init__.py` — 3 sites in `verify_password` (baseline `UNCOVERED`)

| Line | Statement | Mutation sites | Reported status |
| ---: | --- | --- | --- |
| 34 | `return True` (after `_hasher.verify`) | `True` → `False` (1) | unaddressed |
| 36 | `return False` (on `VerifyMismatchError`) | `False` → `True` (1) | unaddressed |
| 38 | `return False` (on `InvalidHashError`) | `False` → `True` (1) | unaddressed |

Argon2 hashing is slow, so this trio was left uncovered at baseline for cost reasons rather than reachability reasons. Kill it with a cached pre-computed hash fixture (hash once in a module-scoped fixture, verify many times).

### Definition of done for Part 1

- [ ] A targeted `mutator` run scoped to `src/ksma/shared/auth/` reports **0 `SURVIVED`** across `__init__.py`, `admin_token.py`, `admin_token_middleware.py`, `dependencies.py`.
- [ ] `tests/unit/test_auth.py` (and a new `tests/unit/test_auth_dependencies.py`) asserts a **positive value**, not just `is not None`, for every row in the tables above — per spec #16 user story #8.
- [ ] `dependencies.py:124` (`==` → `!=`) is killed **or** is documented with a `# pragma: no mutate` naming D7 (`FakeAsyncSession._where` cannot model `!=`) — it is not left as a bare survivor.
- [ ] `test_admin_token.py` pins `AdminTokenPolicy(token=None, is_configured=True) -> is_authorized(...) is True` as **known behaviour**, cross-referenced to D3, so the mutation score reflects a decision rather than a gap.
- [ ] `pytest tests/unit -q` exits 0.

---

## Part 2 — Production defects found during the wave (NOT fixed)

> None of these are fixed by #28. Each needs an ADR pair per #16, and each is a behaviour change, so they are filed here rather than smuggled into a test-only commit.

### D1 — `CapacityExceededError(..., code=...)` raises `TypeError` on both enrollment endpoints

**Found by:** T20
**Files:** `kitapandu-backend/src/ksma/batch/application/__init__.py:360-363` and `:547-550`

```python
raise CapacityExceededError(
    "An overlapping active enrollment already exists for this participant + batch",
    code="ENROLLMENT_OVERLAP",
) from exc
```

**What is wrong:** `DomainError.__init__` (`kitapandu-backend/src/ksma/shared/errors/__init__.py:32-44`) accepts only `field_path`, `details`, and `correlation_id`. There is **no `code=` parameter**. So the V06 overlap branch raises `TypeError: __init__() got an unexpected keyword argument 'code'` instead of a `CapacityExceededError` — the Postgres exclusion-constraint handler never actually handles the overlap. Both `place_enrollment` (`:360`) and `transfer_enrollment` (`:547`) are affected, so the V06 overlap rejection is broken on both paths and the caller sees a 500 instead of the intended 409/422.

**Suggested fix direction:** decide where the machine-readable code lives. Either (a) add an explicit `code: str | None = None` field to `DomainError` (and to `to_payload`, per D4) so every error can carry a stable machine code; or (b) drop `code=` at both call sites and encode `ENROLLMENT_OVERLAP` in `details={"reason": "enrollment_overlap"}`. Option (a) is the better long-term shape but touches every error subclass; option (b) is a two-line change confined to `batch/application`. **Decision required — do not guess.**

**Acceptance:** a test that raises a real `IntegrityError` with `exclusion_violation` in `str(exc.orig)` and asserts a `CapacityExceededError` (not a `TypeError`) escapes `place_enrollment` and `transfer_enrollment`.

### D2 — `activate_membership` double-bumps the same account's `authz_revision`

**Found by:** T23
**Files:** `kitapandu-backend/src/ksma/identity/application/__init__.py:258` and `:263`

```python
m.account.authz_revision += 1 if hasattr(m, "account") else 1   # :258
# Bump the account's authz_revision too (separate transaction so we re-query).
a = (await session.execute(select(AccountRow).where(AccountRow.id == m.account_id))).scalar_one()
a.authz_revision += 1                                            # :263
```

**What is wrong (two defects on adjacent lines):**

- **:258 is dead code.** `m.account.authz_revision += …` compiles to a `m.account` load and an `m.account.authz_revision` load **before** the RHS is evaluated, so `hasattr(m, "account")` is necessarily `True` on every reachable path — a `False` would already have raised `AttributeError`. The `else 1` literal is unreachable and the `1 -> 0` mutation is provably equivalent (pinned by `tests/unit/test_identity_application.py:1309`, `TestActivateMembershipAuthzBump::test_else_branch_of_the_ternary_is_dead_code`).
- **:258 + :263 together are a real authz-versioning bug.** The same `AccountRow` is bumped twice — once through the eagerly-loaded relationship at `:258`, once through the re-queried row at `:263`. A membership activation therefore increments `authz_revision` by **2**, invalidating every already-issued access JWT twice over and making `authz_revision` no longer a usable "number of rights changes" counter (spec #16 relies on it as a monotonic versioning token). The dead `hasattr` ternary is what masked this: the code reads as if `:258` were conditional.

**Suggested fix direction:** delete `:258` entirely (the re-query at `:261-263` is the one that is durable and transactionally correct), reducing the bump to exactly `+1`. Removing the line also removes the equivalent mutant rather than suppressing it. **This is the recommended resolution for wave-equivalent #2** — the `hasattr` ternary is dead code, so the honest fix is deletion, not a `# pragma: no mutate`.

**Acceptance:** `activate_membership` bumps `authz_revision` by exactly 1 (assert `== n+1`, not `>= n+1`), and a JWT minted before activation is rejected after it.

### D3 — Half-configured `AdminTokenPolicy` is a silent bypass

**Found by:** T23
**Files:** `kitapandu-backend/src/ksma/shared/auth/admin_token.py:31-36`, `:64-65`, `:85-86`

```python
@dataclass(frozen=True, slots=True)
class AdminTokenPolicy:
    token: str | None      # None = dev/no-token mode
    is_configured: bool    # True iff a non-empty token is set
```

**What is wrong:** the docstring claims `is_configured` is "True **iff** a non-empty token is set", but the dataclass has **no invariant** enforcing it — both fields are independently writable at construction. `AdminTokenPolicy(is_configured=True, token=None)` passes `is_authorized`'s first guard at `:64` and returns `True` (allow) for **every** request, while `init_admin_token_policy` at `:85` logs `"Admin token gate enabled for /api/v1/admin/*."`. So `/api/v1/admin/*` is fully open in production while the logs claim it is gated. Today `load_policy` is the only construction site and it always builds a consistent pair, so this is **not exploitable** — but any future caller (a second middleware, a test helper, an admin bootstrap script) that constructs the dataclass by hand opens the admin surface with no error and no warning.

**Suggested fix direction:** enforce the invariant at construction. Either (a) `__post_init__` raising `ValueError` when `is_configured and token is None`; or (b) collapse to a single source of truth — drop `is_configured` and derive it (`token is not None`) so the inconsistent state is unrepresentable; or (c) delete `is_configured` from the gate at `:64` and test `policy.token is None` directly. **(b) is the cleanest**; (a) is the smallest diff. Separately, `init_admin_token_policy` should log the *effective* state, not the declared flag.

**Acceptance:** `AdminTokenPolicy(is_configured=True, token=None)` either raises or cannot be constructed; a test asserts `is_authorized` never returns `True` when the policy claims to be configured.

### D4 — `DomainError.to_payload` is code-agnostic (NFR08 exposure)

**Found by:** T27
**Files:** `kitapandu-backend/src/ksma/shared/errors/__init__.py:46-55`

```python
def to_payload(self) -> dict[str, Any]:
    payload = {"code": ..., "message": ..., "correlation_id": ...}
    if self.field_path:
        payload["field_path"] = self.field_path
    if self.details:
        payload["details"] = self.details      # emitted for EVERY code
```

**What is wrong:** `to_payload` serialises `self.details` unconditionally. Spec NFR08 / user story #20 requires that an `ACCESS_DENIED` body never leaks an object payload; the enforcement is **caller discipline, not a guard**. Today the only `AccessDeniedError` construction path (`shared/rbac/__init__.py`, raised when no grant matches) passes no `details`, so this is **not currently exploitable** — but a future call site that attaches `details={"object_ref": ...}` to an `AccessDeniedError` silently ships the object payload to a client that is by definition not authorised to see it.

**Suggested fix direction:** make the guard structural. Either (a) `if self.details and self.code != "ACCESS_DENIED":` in `to_payload`; or (b) override `to_payload` on `AccessDeniedError` to drop `details` unconditionally; or (c) assert in `AccessDeniedError.__init__` that `details` is empty. **(b) is the strongest** — the guarantee travels with the class.

**Acceptance:** a test constructing `AccessDeniedError("nope", details={"object_ref": "secret"})` asserts the serialised payload contains no `details` key.

### D5 — Export TTL derived from two different sources

**Found by:** T21
**Files:** `kitapandu-backend/src/ksma/operations/application/__init__.py:818` and `:893`

```python
# queue_export (:818) — uses the caller's parameter
expires_at=as_of + timedelta(seconds=ttl_seconds),
...
# run_export (:893) — re-derives from the reloaded row
expires_at=job.as_of + timedelta(seconds=ttl_seconds),
```

**What is wrong:** `expires_at` is computed in two places from two different sources — the caller's `as_of` argument at `:818`, and the persisted `job.as_of` at `:893`. They agree only as long as the round-tripped value is identical. Anything that normalises, truncates, or defaults `as_of` between the two calls (DB precision, a caller passing an `as_of` with tz info the column drops, a re-run path that reuses `job.as_of`) silently shifts the expiry window, and the job's lifetime is set by two independent code paths that can drift.

**Suggested fix direction:** compute `expires_at` **once**, at `:818`, and have `run_export` read the already-persisted value (`job.expires_at`) rather than re-deriving it. That deletes the second derivation and removes the class of bug.

**Acceptance:** a test asserts `run_export` writes back the exact `expires_at` computed by `queue_export`, with no recomputation.

### D6 — `define_metric` calls `datetime.now(UTC)` directly, bypassing the injected clock

**Found by:** T21
**Files:** `kitapandu-backend/src/ksma/operations/application/__init__.py:975`

```python
if effective_from is None:
    effective_from = clock().now()          # :973 — injected clock ✅
if effective_until is None:
    effective_until = datetime.now(UTC) + timedelta(days=365 * 100)   # :975 — real clock ❌
```

**What is wrong:** every other timestamp in the module goes through `clock()`. `:975` reaches for the wall clock directly, so `effective_until` cannot be frozen. That breaks freezegun-based determinism **and** pins a 100-year window to process start — a metric defined near the end of a long-lived pilot gets a shorter effective life than one defined at boot, which is almost certainly not the intent.

**Suggested fix direction:** replace `datetime.now(UTC)` with `clock().now()`. One-line change; unblocks time-controlled tests for the whole metric-definition path.

**Acceptance:** under `freezegun`, `define_metric(...)` with `effective_until=None` produces a deterministic `effective_until`; a `test_clock.py` case asserts it.

### D7 — `FakeAsyncSession._where` cannot model `!=` / `<>` predicates

**Found by:** T22 (infrastructure)
**Files:** `kitapandu-backend/tests/unit/conftest_helpers.py:266-283`

```python
def _where(self, sql: str) -> list[tuple[str, str]]:
    """``col = :param`` pairs from the WHERE clause only."""
    found = _WHERE_RE.search(sql)
    if found is None:
        return []
    return [(col.lower(), param) for col, param in _EQUALITY_RE.findall(found.group(1))]
```

**What is wrong:** `_EQUALITY_RE` matches only `=`. Any `!=` / `<>` predicate is silently dropped, so `select(...).where(AccountRow.id != :id)` matches **every** row instead of none. Every in-memory fake built on this helper has the same blind spot. This is why `shared/auth/dependencies.py:124` (`AccountRow.id == sub` → `!=`) is **unkillable today**: flipping it to `!=` does not change what the fake returns, so no test can observe the mutation.

**Suggested fix direction:** teach the fake to model inequality. Extend the regex to capture the operator (`=`, `!=`, `<>`, `<`, `>`, `<=`, `>=`) and have `_matches` (`conftest_helpers.py:277-283`) apply the comparison. Consider a small operator-dispatch table so adding `<`/`>` later is one row. Note this is a **test-infrastructure** change to a shared helper — it will affect every BC's fake, so it needs its own wave and a full `pytest tests/unit` run.

**Acceptance:** `FakeAsyncSession` returns the correct row set for `!=` / `<>` predicates; `dependencies.py:124`'s mutant becomes killable and is killed; `pytest tests/unit -q` still exits 0.

---

## Cross-references

- **Wave-equivalent #2** (`identity/application/__init__.py:258`, the `hasattr` ternary) is resolved by **D2**, not by a suppression — the recommended fix deletes the line, which removes the equivalent rather than documenting it.
- **Wave-equivalent #3** (`shared/money/__init__.py:66`, `scaleb(-_SCALE)`) is a constant-folded equivalent: `_SCALE = 0` at `:30`, so `scaleb(+0) == scaleb(-0)` for every input. Pinned by `tests/unit/test_money.py:254-278` (`TestDecimalConversion`). The `delete -` mutant is unkillable because the sign is a no-op at `_SCALE = 0`; this becomes observable only when a real subunit scale is introduced, at which point the mutant becomes killable and no suppression is needed.
- **Wave-equivalent #1** (`scripts/smoke_pilot/__init__.py:270`, the base64 padding expression) is suppressed in-place by #28 with `# pragma: no mutate`, proved by `tests/unit/test_smoke_pilot.py:1961-1985`.
- Spec #16 decision "Suppress only with documented justification" applies to every `# pragma: no mutate` in the tree.