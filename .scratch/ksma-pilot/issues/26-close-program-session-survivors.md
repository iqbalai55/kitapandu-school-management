# 26: Kill the `program/*` + `session/*` survivors — pin publishing / retirement / version lifecycle + session closure + attendance recording

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for `ksma.program.application` (6 surviving mutations in `publish_program_version`, `publish_rubric_version`, `retire_program_version`, `add_rubric_indicator`), `ksma.program.interface` (4 surviving mutations), and the session BC (`ksma.session.application` — `close_session` at CC 10 / CRAP 10 and the surrounding lifecycle; `ksma.session.interface`; `record_attendance`, `add_revision`, `correct_session`, `write_journal` in application). Each test pins a boundary from `docs/specs/003-program-and-class.md` and `docs/specs/004-session.md` §Testing Decisions. Killing all surviving mutations in these two BCs is the end-to-end behaviour this ticket makes work.
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from the program and session BCs reduced to 0.
- [ ] `close_session` has a parametrised test over every attendance summary outcome (full / partial / empty / disputed) — V10-style boundary table from `docs/specs/004-session.md`.
- [ ] `add_revision` and `correct_session` have positive assertions on the resulting `SessionRevision.number` and the `corrected_from` chain.
- [ ] `publish_program_version` / `publish_rubric_version` / `retire_program_version` each test the version lifecycle state machine (`draft → published → retired`).
- [ ] No production code under `ksma.program` or `ksma.session` is modified.
- [ ] `pytest tests/unit -q` exits 0.
