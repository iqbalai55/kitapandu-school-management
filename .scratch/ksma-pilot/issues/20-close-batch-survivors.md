# 20: Kill the 24 `batch/application` + `batch/interface` survivors — pin V05 / V06 / V07 capacity + exclusion + concurrent-update invariants

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for `ksma.batch.application` (16 surviving mutations in `place_enrollment`, `transfer_enrollment`, `decide_application`, `change_batch_capacity`, `assign_teacher`, `open_batch`, `pause_enrollment`, `resume_enrollment`, `submit_application`, `withdraw_enrollment`) and `ksma.batch.interface` (8 surviving mutations in the HTTP layer). Each test pins a domain invariant drawn from `docs/specs/003-program-and-class.md` §Testing Decisions and `docs/specs/000-umbrella-platform.md` V05/V06/V07: capacity-limit enforcement (V05), overlap-by-exclusion-constraint rejection (V06), and concurrent-update serialisation (V07). Killing the 24 surviving mutations in this BC is the end-to-end behaviour this ticket makes work.
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from `batch/application` and `batch/interface` reduced to 0.
- [ ] At least one test in `test_batch_application.py` asserts the post-condition of `place_enrollment` against a fake repository's `enrollments` table (typed `Enrollment.status`, not a JSON-encoded dict).
- [ ] The `decide_application` route can be exercised through both the application-layer seam and a thin HTTP-layer shim that proves the route dispatcher is wired correctly.
- [ ] No production code under `ksma.batch` is modified.
- [ ] `pytest tests/unit -q` exits 0.
