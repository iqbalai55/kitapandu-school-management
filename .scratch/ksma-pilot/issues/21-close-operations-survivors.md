# 21: Kill the 67 `operations/application` + `operations/interface` + `operations/infrastructure/repositories` survivors — pin V20 atomic-commit + metric-dispatch + attachment-scan invariants

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for `ksma.operations.application` (47 surviving mutations), `ksma.operations.interface` (7 surviving mutations), and `ksma.operations.infrastructure.repositories` (13 surviving mutations). The headline invariant is V20 from `docs/specs/000-umbrella-platform.md` §Testing Decisions: a 100-row import with one bad row → 0 rows committed (atomic-commit guarantee). Secondary invariants include the metric-dispatch tables on `compute_metric` / `define_metric`, the attachment-scan / -read paths, and the `_to_*` row mappers in the infrastructure layer. Killing the 67 surviving mutations in this BC is the end-to-end behaviour this ticket makes work.
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from `ksma.operations.application`, `ksma.operations.interface`, and `ksma.operations.infrastructure.repositories` reduced to 0.
- [ ] The V20 atomic-commit test is parametrised over (a) one-bad-row-out-of-N, (b) one-cross-tenant-row-out-of-N, (c) all-rows-valid → all committed.
- [ ] `run_export`, `queue_export`, `start_operational_run`, `finish_operational_run` have positive assertions on the operational-run state machine (`pending → running → succeeded / failed`), not just "function returns".
- [ ] No production code under `ksma.operations` is modified.
- [ ] `pytest tests/unit -q` exits 0.
