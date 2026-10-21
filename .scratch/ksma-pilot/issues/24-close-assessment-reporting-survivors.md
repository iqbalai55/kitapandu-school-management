# 24: Kill the 23 `assessment/application` + `reporting/infrastructure/repositories` survivors — pin V10 / V11 / V12 grading + certificate + report invariants

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for `ksma.assessment.application` (11 surviving mutations: `set_result` is the highest-CRAP function in this module at CC 19, plus `decide_completion`, `issue_certificate`, `publish_report`, `replace_certificate`, and the surrounding lifecycle transitions) and `ksma.reporting.infrastructure.repositories` (12 surviving mutations: the `_to_*` row mappers and the `next_*_no` sequence generators). Each test pins an invariant from `docs/specs/005-assessment-and-reporting.md` §Testing Decisions: V10 numeric range, V11 single-value-kind rejection + indicator-scope enforcement, V12 revision lifecycle (BR09). Killing the 23 surviving mutations across these two BCs is the end-to-end behaviour this ticket makes work.
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from `ksma.assessment.application` and `ksma.reporting.infrastructure.repositories` reduced to 0.
- [ ] `set_result` has parametrised tests for: numeric in range, two-value-kinds-at-once rejection (`INDICATOR_SCOPE_MISMATCH`), indicator-from-another-rubric rejection, zero-is-accepted-when-scale-allows.
- [ ] `decide_completion` has a positive assertion on the typed `CompletionDecision.outcome` field.
- [ ] `next_certificate_no`, `next_publication_no`, `next_completion_no`, `next_revision_no`, `next_session_no`, `next_version_no` each have a positive assertion on the formatted number string + monotonic increment.
- [ ] No production code under `ksma.assessment` or `ksma.reporting` is modified.
- [ ] `pytest tests/unit -q` exits 0.
