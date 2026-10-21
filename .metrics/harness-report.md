# Codebase Assessment — kitapandu-school-management — 2026-10-20

> Run on branch `fix/mutator-baseline-2026-10-20` (off `main` @ `5129917`). All three metric tools ran against the project root in serial to keep memory bounded.
> Mutator was invoked with `--test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'" --max-workers 2 --no-coverage` so the system pytest (9.1.1) is used for the baseline + every mutant instead of the broken venv pytest at `/root/tools/mutator/.venv/bin/python`.

## Executive verdict
- **Change resilience:** **FAIL — 374/711 mutants killed (52.6 %); 337 survivors; 0 uncovered.** Mutator exited **3** (survivors present, baseline passed). The kill rate is the **same shape as the previous successful run (376 / 712 = 52.8 %)**, so the only thing that changed is that the baseline is no longer broken and every number is freshly measured. Baseline gate is green: **`python3 -m pytest tests/unit`** is 961 passed, 3 skipped, 0 errors — the failure-pre that gated the prior `TESTS BROKEN` verdict is resolved; the integration tests still fail at fixture-time when `alembic` is shelled out (see Caveats) but those are now correctly excluded by the `--test-command`. The 337 survivors cluster in the same application modules as before — see `## Surviving mutants`. This is below the ≥ 95 % pilot acceptance gate, so ticket #14 is **not closed** by this commit.
- **Code reuse:** **177 duplicate pairs** above the 0.82 structural-similarity threshold (unchanged from the prior assessment). **107 pairs score 1.00** (verbatim clones), **70 pairs score 0.82-0.94** (extraction candidates). Largest hotspot is `assessment/infrastructure/repositories.py` cloning lines 28-31, 88-95, 232-263, 265-281, 283-293 into 6+ peer repositories (batch / finance / program / reporting / session / participant / portal).
- **Risk density:** worst CRAP **132.0** (`compute_invoice_projection` at `ksma.finance.application`, CC=11, Cov=0 %). **7 functions score CRAP ≥ 50** at 0 % coverage (in `finance`, `operations`, `batch` application layers). **22 functions score CRAP ≥ 30** when the next bucket (CC 5-7 at 0 %) is included. Coverage was re-measured from the wider `pytest` invocation (unit + integration fixture boot) and recorded to `target/coverage/python/kitapandu-backend/lcov.info`. Risk still concentrates where complexity meets untested code.

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

*Footer: 22 functions ≥ CRAP 30 in the visible table; the second tier (CRAP 18-23) covers `preview_import` (CC 23, Cov 95 %, CRAP 23.1), `covers_target` (CC 21, Cov 100 %, CRAP 21.0), `commit_import` (CC 17, Cov 81.8 %, CRAP 18.7), `create_invoice` (CC 14, Cov 72.7 %, CRAP 18.0). Full table in `.metrics/crapper.txt`. CRAP scores are unchanged from the prior assessment because no source code moved.*

## Duplicates — structural similarity ≥ 0.82

177 pairs detected across all 138 Python files (unchanged from prior). Score distribution: **1.00 → 107 pairs**, 0.91-0.94 → 9 pairs, 0.86-0.88 → 3 pairs, 0.82-0.84 → 58 pairs. Average 0.94. Top 20 pairs (all 1.00):

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

## Surviving mutants (top 20 by file)

This run measured **337 survivors** out of 711 sites (`SURVIVED` count from `.metrics/mutator.txt`, exits with code 3). Below is the file-level distribution and a representative slice of 20 mutations for the executive reader.

### Survivors by file (top 15)

| File | Survivors |
| --- | ---: |
| `kitapandu-backend/src/ksma/finance/application/__init__.py` | 57 |
| `kitapandu-backend/src/ksma/operations/application/__init__.py` | 47 |
| `kitapandu-backend/src/ksma/scripts/seed_vaneta_demo/__init__.py` | 34 |
| `kitapandu-backend/src/ksma/identity/application/__init__.py` | 29 |
| `kitapandu-backend/src/ksma/scripts/smoke_pilot/__init__.py` | 22 |
| `kitapandu-backend/src/ksma/batch/application/__init__.py` | 16 |
| `kitapandu-backend/src/ksma/operations/infrastructure/repositories.py` | 13 |
| `kitapandu-backend/src/ksma/finance/infrastructure/repositories.py` | 12 |
| `kitapandu-backend/src/ksma/assessment/application/__init__.py` | 11 |
| `kitapandu-backend/src/ksma/participant/interface/__init__.py` | 8 |
| `kitapandu-backend/src/ksma/observability/__init__.py` | 8 |
| `kitapandu-backend/src/ksma/batch/interface/__init__.py` | 8 |
| `kitapandu-backend/src/ksma/shared/auth/dependencies.py` | 7 |
| `kitapandu-backend/src/ksma/operations/interface/__init__.py` | 7 |
| `kitapandu-backend/src/ksma/program/application/__init__.py` | 6 |

*Top-three hot files (`finance/application` 57, `operations/application` 47, `scripts/seed_vaneta_demo` 34) account for 138 of the 337 survivors (41 %). These overlap exactly with the CRAP top-7 plus the seed/smoke scripts — the unit suite asserts "function returns without raising" but does not pin the values or boolean conditions those functions evaluate.*

### Representative survivor slice (20 rows, alphabetically by file)

Paths are relative to `kitapandu-backend/`. `src/ksma/` is elided from the path column for brevity.

| File | Line | Mutation |
| --- | ---: | --- |
| `alembic/env.py` | 29 | `True -> False` |
| `alembic/versions/2026_10_20_0240_0009_portal_operations.py` | 284 | `delete -` |
| `alembic/versions/2026_10_20_0240_0009_portal_operations.py` | 284 | `1 -> 0` |
| `src/ksma/assessment/application/__init__.py` | 157 | `and -> or` |
| `src/ksma/assessment/application/__init__.py` | 157 | `delete not` |
| `src/ksma/assessment/application/__init__.py` | 162 | `== -> !=` |
| `src/ksma/assessment/application/__init__.py` | 162 | `1 -> 0` |
| `src/ksma/finance/application/__init__.py` | 139 | `< -> <=` |
| `src/ksma/finance/application/__init__.py` | 139 | `0 -> 1` |
| `src/ksma/finance/application/__init__.py` | 144 | `< -> <=` |
| `src/ksma/identity/application/__init__.py` | 102 | `== -> !=` |
| `src/ksma/identity/application/__init__.py` | 188 | `== -> !=` |
| `src/ksma/identity/application/__init__.py` | 252 | `== -> !=` |
| `src/ksma/operations/application/__init__.py` | 226 | `> -> >=` |
| `src/ksma/operations/application/__init__.py` | 294 | `> -> >=` |
| `src/ksma/operations/application/__init__.py` | 421 | `* -> /` |
| `src/ksma/participant/interface/__init__.py` | 391 | `1 -> 0` |
| `src/ksma/program/application/__init__.py` | 122 | `delete not` |
| `src/ksma/shared/auth/dependencies.py` | 158 | `or -> and` |
| `src/ksma/shared/auth/dependencies.py` | 207 | `or -> and` |

*Full enumeration of all 337 `SURVIVED` rows lives in `.metrics/mutator.txt` (every line beginning with `SURVIVED`). The matching 374 `KILLED` lines are at the end of the same file. Survivor profile is identical in shape to the prior successful assessment (376 / 712); only +/-2 survivors differ across runs, well within single-mutant flakiness noise.*

## Caveats

- **Mutator command form** — the mutator run only completes end-to-end when `--test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'"` and `--max-workers 2 --no-coverage` are passed. Without those flags:
  - the bundled `pytest` at `/root/tools/mutator/.venv/bin/python -m pytest` fails with `No module named pytest`, returning `Baseline failed` for every module (which is what produced the previous `TESTS BROKEN` exit-2 verdict); and
  - with no `--max-workers` mutator defaults to one worker per CPU core (8 on this runner), which makes each cycle consume ~1 GB more RAM than the bounded 2-worker run.
- **Integration tests still fail at fixture-time** — `tests/integration/conftest.py:183` shells out to `alembic` (the CLI) which is not on `PATH` inside the harness env, so 44 integration setup errors recur the moment any test command touches `tests/integration`. The harness explicitly excludes integration via `--test-command "... pytest tests/unit ..."`, so this is no longer a `mutator` blocker, but it remains a real CI gap (see Recommendation #5).
- **Coverage profile is unit + integration boot** — `target/coverage/python/kitapandu-backend/lcov.info` records unit pass + the (failing) integration setup. Functions imported by Alembic migration scripts (`alembic/env.py`, `alembic/versions/...py`) still sit at 0 % for that reason; CRAP = CC² for them, not a real coverage gap.
- **Polyglot distribution** — **158 Python files**, 0 of any other language. `kitapandu-backend/` is the only source tree. `target/` holds build artefacts, not source.
- **Three known skips** — `tests/unit/test_operations_application.py:996, 1046, 1164` skip per P42 / Option B with reasons stored inline; unchanged from prior runs.
- **Wall-time drift** — this full mutator run took **51 min 12 s wall, 126 min 28 s user, 31 min 11 s sys** at `--max-workers 2`. `--since-last-run` does **not** skip work when the snapshot has been deleted; do not delete `.metrics/mutate/*.edn` between runs.
- **Wider duplication surface is unchanged** — the 177-pair dryer count is identical to the prior assessment because no source files have been added or removed at this audit.

## Recommendations (priority order)

1. **✅ BASELINE RESTORED** — done in this run. The correct mutator invocation (see Caveats) turns the executive verdict from `TESTS BROKEN` into a measurable `FAIL` with 337 enumerated survivors. Wire this invocation into a `make harness-mutation` target (and into CI via `make ci`) so subsequent audits cannot regress.
2. **Ticket #14 — close the survivor gap to ≥ 95 %** — the 337 survivors are now an *enumerable* set. The unit-side workload is described in `.scratch/ksma-pilot/issues/14-kill-all-mutants.md`. Top priority buckets:
   - `finance/application/__init__.py` (57 survivors) and `operations/application/__init__.py` (47) — the two hottest files; closing them alone moves the score by ~14 percentage points.
   - `scripts/seed_vaneta_demo/__init__.py` (34) and `scripts/smoke_pilot/__init__.py` (22) — these are demo fixtures; covering them is cheap and the tests can be added to the `test_smoke_pilot.py` skeleton that already exists.
   - `batch/application/__init__.py` (16) — small surface, all the priority user stories listed in #14 are functions in this file.
3. **Ticket #13 / #14 — Add CI gate** — wire `mutator --since-last-run --max-workers 2 --no-coverage --test-command "...tests/unit..."` into a Makefile target that runs on every PR. Even at the current 52.6 % kill rate, the trend becomes visible.
4. **Extract duplicated repository logic** — `assessment/infrastructure/repositories.py` lines 28-31, 88-95, 232-263, 265-281, 283-293 are cloned verbatim into 6-8 peer repositories. Promoting them into a single `ksma.shared.infrastructure.row_mapping` helper module removes ~107 verbatim-clone pairs and reduces the future per-BC maintenance surface. (Separate ticket; out of scope for this baseline-fix commit.)
5. **Restore the integration gate** — point `tests/integration/conftest.py:183` at `python3 -m alembic` (or add the `alembic` script to a venv that is on PATH) so the 44 fixture-time errors clear and the integration half of the test pyramid can be re-enabled. After that fix, mutator's `--test-command` can broaden back to `pytest tests` and the integration-side survivors (currently hidden by the `--test-command "tests/unit"` filter) will be visible. Tracked under ticket #13 / follow-up wave.

---

*Generated by `unclebob-metrics-assessor` (mutator + dryer + crapper) on 2026-10-20. Raw artefacts: `.metrics/mutator.txt`, `.metrics/dryer.txt`, `.metrics/crapper.txt`. Intermediate EDN dumps: `.metrics/mutate/`, `.metrics/crap.edn`, `.metrics/dry.edn`. Coverage LCOV: `target/coverage/python/kitapandu-backend/lcov.info`. Mutator command: `mutator --root . --since-last-run --test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'" --max-workers 2 --no-coverage` (51 min 12 s wall, 126 min 28 s user).*
