# 18: Kill the 34 `scripts/seed_vaneta_demo` survivors — parametrise each `_ensure_*` and `_vaneta_*` helper

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A second unit-test layer for the seed pipeline (`scripts/seed_vaneta_demo/__init__.py`) that pins a domain invariant on every `_ensure_*` helper and the `_vaneta_*` payload builders. Each test seeds an in-memory fake `AsyncSession`, runs the helper with a structured input, and asserts the post-condition on the captured write set (inserted ids, statuses, payload shape). Killing the 34 surviving mutations in this file is the end-to-end behaviour this ticket makes work.
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from `kitapandu-backend/src/ksma/scripts/seed_vaneta_demo/__init__.py` reduced to 0.
- [ ] Each test uses the seam pattern from `tests/unit/test_rbac_action_registry.py` (top-of-file docstring declaring module + public surface + behaviour asserted + behaviour not asserted; pure-Python; no DB).
- [ ] Each `_ensure_*` helper has at least one positive assertion on the inserted row's typed fields, not just "function returns without raising" (per spec #16's "domain-shaped" assertion rule).
- [ ] No production code under `kitapandu-backend/src/ksma/scripts/seed_vaneta_demo/` is modified.
- [ ] `pytest tests/unit -q` exits 0.
