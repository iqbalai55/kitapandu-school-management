# 19: Kill the 22 `scripts/smoke_pilot` survivors — pin each `step_NN_*` script and `SmokeRecorder.record`

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for the smoke pilot driver (`scripts/smoke_pilot/__init__.py`, including `SmokeRecorder`) that asserts every `step_NN_*` function writes the documented audit record and the recorded HTTP request/response pair matches what `SmokeRecorder.record` was given. An in-memory `SmokeRecorder` is reused across the wave (per ticket #18's seed helper fakes); the tests verify the round-trip in-process so the smoke script can be re-run in CI without Docker. Killing the 22 surviving mutations in this file is the end-to-end behaviour this ticket makes work.
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from `kitapandu-backend/src/ksma/scripts/smoke_pilot/__init__.py` reduced to 0.
- [ ] `SmokeRecorder.record` has a positive assertion on the recorded entry shape (status, latency_ms, request_body, response_body), not a `len(records) > 0` shim.
- [ ] Each `step_NN_*` test exits with the documented final `SmokeRecorder` count, asserting order and contents.
- [ ] No production code under `kitapandu-backend/src/ksma/scripts/smoke_pilot/` is modified.
- [ ] `pytest tests/unit -q` exits 0.
