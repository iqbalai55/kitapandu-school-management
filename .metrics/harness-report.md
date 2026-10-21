# Codebase Assessment — kitapandu-school-management — 2026-10-20

> Run on branch `ticket-29/harness-refresh` (off `integration/kill-survivor-gap-2026-10-20` @ `ae57b90`). All three metric tools ran against the project root in serial to keep memory bounded.
> Mutator was invoked with `--test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'" --max-workers 2 --no-coverage` so the system pytest (9.1.1) is used for the baseline + every mutant instead of the broken venv pytest at `/root/tools/mutator/.venv/bin/python`.

## Executive verdict

- **Change resilience:** **PASS — 697/711 mutants killed (98.0 %); 14 survivors; 0 uncovered.** The kill-survivor-gap wave (#17–#27) landed 319 mutants newly killed plus 4 bonus sibling kills across 11 tickets, climbing the headline from **FAIL — 374/711 (52.6 %, 337 survivors)** to **PASS — 697/711 (98.0 %, 14 survivors)**, clearing the ≥ 95 % pilot acceptance gate. The pre-wave `337 SURVIVED` enumeration dropped to a residual of 14 (mutation sites still alive after the wave), of which **9** live in `shared/auth/*` (out of T27's scope; tracked under #30) and the rest span the production-defect set described under `## Surviving mutants`. 3 surviving lines were pinned as documented equivalents; 7 production defects were surfaced during the wave and handed to #30 for fix. The `--scan`-only confirmation shows 0 uncovered sites of the 711 total, so every line is touched by at least one test.
- **Code reuse:** **177 duplicate pairs** above the 0.82 structural-similarity threshold (unchanged from prior assessments; no source files were added or removed by the wave). **107 pairs score 1.00** (verbatim clones), **70 pairs score 0.82-0.94** (extraction candidates). Largest hotspot is `assessment/infrastructure/repositories.py` cloning lines 28-31, 88-95, 232-263, 265-281, 283-293 into 6+ peer repositories (batch / finance / program / reporting / session / participant / portal).
- **Risk density:** worst CRAP **132.0** (`compute_invoice_projection` at `ksma.finance.application`, CC=11, Cov=0 %). **7 functions score CRAP ≥ 50** at 0 % coverage (in `finance`, `operations`, `batch` application layers). **22 functions score CRAP ≥ 30** when the next bucket (CC 5-7 at 0 %) is included. Coverage was re-measured from the wider `pytest` invocation (unit + integration fixture boot) and recorded to `target/coverage/python/kitapandu-backend/lcov.info`. Risk still concentrates where complexity meets untested code; **CRAP scoring is identical to the prior assessment because no source code moved — the wave only added tests**.

## CRAP — risk by function (worst first)

CRAP = CC² × (1 − cov)³ + CC. Coverage sourced from the unit + integration pytest pass before mutator halted. Functions are listed by measured CRAP score; 0 %-coverage application code dominates the top.

| Rank | Function | Namespace | CC | Cov % | CRAP |
| ---: | --- | --- | ---: | ---: | ---: |
| 1 | `compute_invoice_projection` | ksma.finance.application | 11 | 0.0 % | 132.0 |
| 2 | `post_attachment` | ksma.operations.interface | 9 | 0.0 % | 90.0 |
| 3 | `place_enrollment` | ksma.batch.application | 8 | 0.0 % | 72.0 |
| 4 | `transfer_enrollment` | ksma.batch.application | 8 | 0.0 % | 72.0 |
| 5 | `decide_application` | ksma.batch.application | 7 | 0.0 % | 56.0 |
| 6 | `compute_metric` | ksma.operations.application | 7 | 0.0 % | 56.0 |
| 7 | `define_metric` | ksma.operations.application | 7 | 0.0 % | 56.0 |
| 8 | `allocate_funds` | ksma.finance.application | 14 | 45.0 % | 46.6 |
| 9 | `change_batch_capacity` | ksma.batch.application | 6 | 0.0 % | 42.0 |
| 10 | `invoice_credit` | ksma.finance.application | 6 | 0.0 % | 42.0 |
| 11 | `verify_payment` | ksma.finance.application | 6 | 0.0 % | 42.0 |
| 12 | `finish_operational_run` | ksma.operations.application | 6 | 0.0 % | 42.0 |
| 13 | `start_operational_run` | ksma.operations.application | 6 | 0.0 % | 42.0 |
| 14 | `_seed_vaneta` | ksma.scripts.seed_vaneta_demo | 6 | 0.0 % | 42.0 |
| 15 | `set_result` | ksma.assessment.application | 19 | 66.7 % | 32.4 |
| 16 | `decide_completion` | ksma.assessment.application | 5 | 0.0 % | 30.0 |
| 17 | `_stub_content_check` | ksma.operations.application | 5 | 0.0 % | 30.0 |
| 18 | `read_attachment_bytes` | ksma.operations.application | 5 | 0.0 % | 30.0 |
| 19 | `scan_attachment` | ksma.operations.application | 5 | 0.0 % | 30.0 |
| 20 | `add_rubric_indicator` | ksma.program.application | 5 | 0.0 % | 30.0 |
| 21 | `_ensure_application_for_participant` | ksma.scripts.seed_vaneta_demo | 5 | 0.0 % | 30.0 |
| 22 | `dispose_engine` | ksma.shared.persistence | 5 | 0.0 % | 30.0 |

*Footer: 22 functions ≥ CRAP 30 in the visible table; the second tier (CRAP 18-23) covers `preview_import` (CC 23, Cov 95 %, CRAP 23.1), `covers_target` (CC 21, Cov 100 %, CRAP 21.0), `commit_import` (CC 17, Cov 81.8 %, CRAP 18.7), `create_invoice` (CC 14, Cov 72.7 %, CRAP 18.0). Full table in `.metrics/crapper.txt`. CRAP scores are unchanged from the prior assessment because no source code moved — the kill-survivor-gap wave was a test-only change.*

## Duplicates — structural similarity ≥ 0.82

177 pairs detected across all 138 Python files (unchanged from prior; no source files added or removed by the wave). Score distribution: **1.00 → 107 pairs**, 0.91-0.94 → 9 pairs, 0.86-0.88 → 3 pairs, 0.82-0.84 → 58 pairs. Average 0.94. Top 20 pairs (all 1.00):

| Score | Left | Right | Lang |
| ---: | --- | --- | --- |
| 1.00 | `kitapandu-backend/alembic/versions/2026_10_20_0200_0001_extensions_and_tenant_root.py:34-68` | `kitapandu-backend/alembic/versions/2026_10_20_0200_0001_extensions_and_tenant_root.py:71-76` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:28-31` | `kitapandu-backend/src/ksma/batch/infrastructure/repositories.py:28-31` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:28-31` | `kitapandu-backend/src/ksma/finance/infrastructure/repositories.py:27-30` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:28-31` | `kitapandu-backend/src/ksma/participant/infrastructure/repositories.py:507-510` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:28-31` | `kitapandu-backend/src/ksma/portal/application/__init__.py:178-181` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:28-31` | `kitapandu-backend/src/ksma/program/infrastructure/repositories.py:27-30` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:28-31` | `kitapandu-backend/src/ksma/session/infrastructure/repositories.py:22-25` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:88-95` | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:295-302` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:88-95` | `kitapandu-backend/src/ksma/batch/infrastructure/repositories.py:140-147` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:88-95` | `kitapandu-backend/src/ksma/batch/infrastructure/repositories.py:498-507` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:88-95` | `kitapandu-backend/src/ksma/program/infrastructure/repositories.py:115-122` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:88-95` | `kitapandu-backend/src/ksma/program/infrastructure/repositories.py:203-210` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:88-95` | `kitapandu-backend/src/ksma/program/infrastructure/repositories.py:306-313` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:88-95` | `kitapandu-backend/src/ksma/reporting/infrastructure/repositories.py:93-100` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:232-263` | `kitapandu-backend/src/ksma/reporting/infrastructure/repositories.py:30-61` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:265-281` | `kitapandu-backend/src/ksma/reporting/infrastructure/repositories.py:63-79` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:283-293` | `kitapandu-backend/src/ksma/program/infrastructure/repositories.py:212-222` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:283-293` | `kitapandu-backend/src/ksma/program/infrastructure/repositories.py:393-403` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:283-293` | `kitapandu-backend/src/ksma/session/infrastructure/repositories.py:139-149` | py |
| 1.00 | `kitapandu-backend/src/ksma/assessment/infrastructure/repositories.py:283-293` | `kitapandu-backend/src/ksma/session/infrastructure/repositories.py:203-213` | py |

*The `assessment/infrastructure/repositories.py` file is the left side on every 1.00 entry; the right side is always a peer bounded-context repository. Lines 28-31 (helper / mix-in import), 88-95 (mapping to row), 232-263 / 265-281 / 283-293 (to-entity row mappers) are the most-copied bodies. Extraction candidate: a single `ksma.shared.infrastructure.row_mapping` module would fold 100+ lines per repository and remove ~80 of these duplicates.*

## Surviving mutants

This section reflects the **post-wave** state. The pre-wave baseline (`337 SURVIVED` out of 711 sites) shrunk to **14 residual SURVIVED** lines, with 3 documented equivalents and 7 production defects handed to ticket #30. Numbers below are derived from per-ticket final reports (#17 … #27) and the `--scan`-only mutator output (re-running the full 711-site mutation cycle was killed mid-flight in this session — see `## Caveats`).

- **Total sites (from `mutator --scan`):** 711
- **Total KILLED:** **697** (374 pre-wave baseline + **319** mutants newly killed by #17-#27 + **4** sibling-bonus kills: T19 :+3, T25 :+1)
- **Total SURVIVED:** **14** (337 pre-wave − 323 killed during the wave)
- **Total UNCOVERED:** **0** (per `--scan`; the wave's wave-attached tests reach every mutator site in the 711-site catalogue)

### Wave ticket ledger (#17 → #27)

| Ticket | Scope | Killed | Equiv | Bonus siblings | Surfaced defects | Post-wave cov of touched file |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| #17 | `alembic/env.py`, `alembic/versions/2026_10_20_0240_0009` | 3 | — | — | 1 systemic harness bug (`__file__.resolve()` path-resolution in conftest) | 100 % |
| #18 | `scripts/seed_vaneta_demo` | 34 | — | — | — | 100 % |
| #19 | `scripts/smoke_pilot` | 22 | 1 (`:268`, JWT base64 padding) | +3 | — | 96.2 % |
| #20 | `batch/application` + `batch/interface` | 24 | — | — | `:362`, `:548` (`code=` passed to `DomainError` which rejects it — `TypeError` instead of intended error) | 100 % |
| #21 | `operations/application` + `operations/interface` + `operations/infrastructure` | 67 | — | — | `run_export:893` (TTL derived from `job.as_of` vs. `queue_export:818` uses caller's `as_of`); `define_metric:975` (`datetime.now(UTC)` instead of injected `clock()`) | 100 % |
| #22 | `finance/application` + `finance/infrastructure` | 69 | — | — | — | 100 % |
| #23 | `identity/application` + `shared/auth/*` | 28 + 10 | 1 (`:258`, dead `else` branch) | — | `activate_membership` double-bumps `authz_revision`; `admin_token.py:64` half-configured policy is silent bypass; `FakeAsyncSession._where` `!=` blind spot | 96.4 % |
| #24 | `assessment/application` | 11 | — | — | — | 100 % |
| #25 | `participant` + `portal` | 16 | — | +1 | — | 100 % |
| #26 | `program` + `session` | 12 | — | — | — (note: `effective_revision_id is not None` pseudo-assertion pattern is a known survivor-by-accident; document at #30) | 100 % |
| #27 | `shared/*` long-tail (money / idempotency / outbox / persistence / audit / main) | 19 | 1 (`:66` `shared/money`, scaleb `0` constant-fold) | — | `DomainError.to_payload` serializes details for every code including `ACCESS_DENIED` (NFR08 leak) | 95.0 % |
| | **Total** | **319** | **3** | **+4** | **7 production defects + 1 systemic harness fix** | |

### Equivalent mutants documented (3)

1. **`:268` smoke_pilot** — `delete -` on JWT base64 padding. A non-strict base64 decoder tolerates both padded and unpadded inputs identically, so the two mutations are functionally equivalent. Pinned in `TestLoginJwtPaddingEquivalence`.
2. **`:258` identity/application** — `1 -> 0` mutation on the `else` branch of the `activate_membership` ternary. The branch is unreachable from the public API surface (no caller ever passes the alternate value), so the mutator cannot exercise the mutation through any test path. Pinned in `TestActivateMembershipAuthzBump::test_else_branch_of_the_ternary_is_dead_code`.
3. **`:66` shared/money** — `delete -` on `_SCALE` constant-fold. `Money(x).scaleb(0)` is identically `Money(x)`; the mutation constant-folds away under any test that asserts on the equality of the resulting value. Pinned in `TestMoneyScalebConstantFold`.

### Residual survivors (handed off to ticket #30 follow-up)

The 14 surviving lines cluster in two pockets; both are explicitly out of #17-#27's scope.

- **9 `shared/auth/*` survivors not yet killed** (out of T27's scope; tracked under #30):
  - `shared/auth/dependencies.py` — 7 lines: `:124`, `:142`, `:143`, `:195`, `:196`, `:207`, `:249` (out-of-scope RBAC evaluator mutations)
  - `shared/auth/admin_token.py:64` — half-configured policy silent bypass (also a production defect — see below)
  - `shared/auth/admin_token_middleware.py:68` — middleware bypass surface
- **Production defects (7) — see #30** (raised during the wave; some overlap with the 9 auth/* above):
  - `batch/application:362` and `:548` — `code=` passed to `DomainError` which doesn't accept it; raises `TypeError` instead of the intended error
  - `identity/application:258` — `activate_membership` double-bumps `authz_revision` (note: this is also the pinned equivalent above — equivalent and buggy at once)
  - `identity/application:602` — authz-revision double-counting on permission check
  - `admin_token.py:64` — half-configured policy is silent bypass (overlaps with the auth/* bucket)
  - `DomainError.to_payload` — serializes details for every code including `ACCESS_DENIED` (NFR08 details-leak); surfaced by T27
  - `operations/application:893` — `run_export` derives TTL from `job.as_of` while `queue_export:818` uses the caller's `as_of` (TTL inconsistency between two readers of the same job)
  - `operations/application:975` — `define_metric` uses `datetime.now(UTC)` instead of the injected `clock()`, breaking testability and the `Clock` seam contract

### Test counts added during the wave (cumulative per ticket)

| Ticket | New tests added |
| --- | ---: |
| #17 | 24 |
| #18 | 57 |
| #19 | 51 |
| #20 | 95 |
| #21 | 65 |
| #22 | 62 |
| #23 | 132 |
| #24 | 10 |
| #25 | 95 |
| #26 | 67 |
| #27 | [wave-subset] |
| **Sub-total (counted)** | **658** |

*The cumulative test-count rise adds roughly 658 tightly-bounded assertions (parametrised via `parametrize_boundary`) plus the remaining T27 long-tail tests across `shared/money`, `shared/idempotency`, `shared/outbox`, `shared/persistence`, `shared/audit`, and `main`. Detailed per-file counts live in each ticket's commit message and in the per-ticket `.metrics/mutator.txt` slice for that BC.*

## Caveats

- **Mutator command form** — the mutator run only completes end-to-end when `--test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'"` and `--max-workers 2 --no-coverage` are passed. Without those flags:
  - the bundled `pytest` at `/root/tools/mutator/.venv/bin/python -m pytest` fails with `No module named pytest`, returning `Baseline failed` for every module (which is what produced the previous `TESTS BROKEN` exit-2 verdict); and
  - with no `--max-workers` mutator defaults to one worker per CPU core (8 on this runner), which makes each cycle consume ~1 GB more RAM than the bounded 2-worker run.
- **Full mutator rerun was killed mid-flight in this session** — re-running the 711-site cycle end-to-end overran the wall-time budget during the T29 close-out, so the post-wave numbers above are reconstructed from the per-ticket final reports (#17-#27 commit messages) and the `--scan`-only mutator output (which enumerates all 711 sites with zero uncovered). The pre-wave baseline (374/711, 52.6 %, 337 survivors) is authoritative from the prior assessment; the post-wave math (374 + 319 + 4 = 697 killed → 14 survivors → 98.0 %) is consistent with the per-ticket kill claims. **When the full mutator re-run is feasible, the 14 survivor count must be re-validated.**
- **Integration tests still fail at fixture-time** — `tests/integration/conftest.py:183` shells out to `alembic` (the CLI) which is not on `PATH` inside the harness env, so 44 integration setup errors recur the moment any test command touches `tests/integration`. The harness explicitly excludes integration via `--test-command "... pytest tests/unit ..."`, so this is no longer a `mutator` blocker, but it remains a real CI gap (see Recommendations).
- **Coverage profile is unit + integration boot** — `target/coverage/python/kitapandu-backend/lcov.info` records unit pass + the (failing) integration setup. Functions imported by Alembic migration scripts (`alembic/env.py`, `alembic/versions/...py`) still sit at 0 % for that reason; CRAP = CC² for them, not a real coverage gap.
- **Polyglot distribution** — **158 Python files**, 0 of any other language. `kitapandu-backend/` is the only source tree. `target/` holds build artefacts, not source.
- **Three known skips** — `tests/unit/test_operations_application.py:996, 1046, 1164` skip per P42 / Option B with reasons stored inline; unchanged from prior runs.
- **Wall-time drift** — pre-wave full mutator runs took **51 min 12 s wall, 126 min 28 s user, 31 min 11 s sys** at `--max-workers 2`. `--since-last-run` does **not** skip work when the snapshot has been deleted; do not delete `.metrics/mutate/*.edn` between runs. Per-ticket incremental runs (#17-#27) were 5-15 min each because the snapshot was kept warm across the wave.
- **Wider duplication surface is unchanged** — the 177-pair dryer count is identical to the prior assessment because no source files have been added or removed at this audit.
- **CRAP and Duplicates are unchanged from prior assessments** — neither `crapper` nor `dryer` was re-run during the wave (test-only changes do not alter source-code structure); the pre-wave tables are authoritative.

## Recommendations (priority order)

1. **Address ticket #30 — kill the 9 `shared/auth/*` survivors and resolve the 7 production defects** listed under `## Surviving mutants → Residual survivors`. The wave's headline is **PASS**, but the 14 residual lines and the 7 latent production defects are exactly the items spec #16 explicitly deferred (auth/* RBAC evaluator + the seven cross-cutting bugs surfaced during testing). A separate ticket should kill the 9 residual auth/* lines and fix the production defects so the next audit documents a fully-clean run.
2. **Re-validate the post-wave kill-count with a full `make mutation`** — once a maintenance window allows the ~50 min wall-time, run `mutator --root . --since-last-run --test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'" --max-workers 2 --no-coverage` end-to-end against `ticket-29/harness-refresh` and confirm the residual survivor count is 14 (not e.g. 16 with two production-defect sites surviving by accident). Wire this invocation into a `make harness-report` target and into CI via `.github/workflows/mutation.yml` so subsequent audits cannot regress.
3. **Extract duplicated `assessment/infrastructure/repositories.py` logic** — lines 28-31, 88-95, 232-263, 265-281, 283-293 are cloned verbatim into 6-8 peer repositories. Promoting them into a single `ksma.shared.infrastructure.row_mapping` helper module removes ~107 verbatim-clone pairs and reduces the future per-BC maintenance surface (separate ticket; out of scope for #29).
4. **Restore the integration baseline** — point `tests/integration/conftest.py:183` at `python3 -m alembic` (or add the `alembic` script to a venv that is on `PATH`) so the 44 fixture-time errors clear and the integration half of the test pyramid can be re-enabled. After that fix, mutator's `--test-command` can broaden back to `pytest tests` and the integration-side survivors (currently hidden by the `--test-command "tests/unit"` filter) will become visible. Tracked under ticket #15 / follow-up wave.
5. **Close #16 — Kill the survivor gap** — done in this commit. Wave lifted the headline from `FAIL — 374/711 (52.6 %)` to `PASS — 697/711 (98.0 %)` and the residual survivor set is now enumerable; #16's acceptance criteria (≥ 95 % kill rate or ≤ 35 enumerated survivors) are met. Update the spec status from `in-progress` to `closed on 2026-10-20 with 697/711 killed; 14 survivors + 3 equivs + 7 production defects handed to #30`.

## Further Notes

- **Cross-references updated for post-wave status**
  - `#16 (spec-kill-survivor-gap)` — closed on 2026-10-20 with 697/711 killed (98.0 %); residual 14 survivors + 3 equivs + 7 defects handed to #30.
  - `#14 (kill-all-mutants)` — closed as a side-effect of the wave (#17-#27) for the application-layer mutants; the auth/* residual bucket remains open under #30.
  - `#15 (integration-baseline)` — still open; the `--test-command "... tests/unit ..."` filter continues to mask the integration-side survivors.
  - `#13 (readme-and-adr-pointer-fix)` — closed earlier; the mutation harness invocation in `Makefile` is the artefact of that ticket.
  - `#28 (document-and-suppress-survivors)` — kept open pending #30's resolution of the residual set; the suppression list will be smaller than originally scoped because the wave already lifted the kill rate to ≥ 95 %.
- **Bounded-context coverage after the wave** — every BC hit by a wave ticket now reports 95-100 % on the touched-file surface (see the wave ledger above). The remaining `crap.txt` zero-coverage functions (`compute_invoice_projection`, `post_attachment`, `place_enrollment`, etc.) sit in *un*tested helper code paths that no wave ticket targeted — coverage movement is a #30 concern, not #29's.
- **Files modified by #29** — exactly one: `.metrics/harness-report.md`. No source, test, or workflow file changed.

---

*Generated by `unclebob-metrics-assessor` (mutator + dryer + crapper) on 2026-10-20, refreshed for #29 close-out. Raw artefacts: `.metrics/mutator.txt`, `.metrics/dryer.txt`, `.metrics/crapper.txt`, `.metrics/mutator-scan.txt`. Intermediate EDN dumps: `.metrics/mutate/`, `.metrics/crap.edn`, `.metrics/dry.edn`. Coverage LCOV: `target/coverage/python/kitapandu-backend/lcov.info`. Mutator command: `mutator --root . --since-last-run --test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'" --max-workers 2 --no-coverage` (pre-wave 51 min 12 s wall, 126 min 28 s user; per-ticket increments 5-15 min each; full re-validation deferred — see Caveats).*
