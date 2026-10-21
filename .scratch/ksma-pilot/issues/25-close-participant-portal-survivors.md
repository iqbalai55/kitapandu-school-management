# 25: Kill the `participant/*` + `portal/*` survivors — pin search/merge boundary + service-request dispatch + audit projection

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for `ksma.participant.application` (`search_participants` is the 10-CC, CRAP-10 function in the top-22 CRAP table and contributes most of the 5 surviving mutations here; `merge_participants`, `request_relation`, `request_account_link`, `update_participant` cover the rest), `ksma.participant.interface` (8 surviving mutations), `ksma.participant.infrastructure.repositories` (`search`, `_json_dumps`, `_compute_suspects`, `archive`), `ksma.participant.domain` (5 surviving mutations in `FinancialScope.from_json` and `to_json`), and the portal BC (`ksma.portal.application`, `ksma.portal.interface`, `ksma.portal.interface.audit_projection._make_handler`). Each test pins a boundary from `docs/specs/002-participant.md` and `docs/specs/007-portal.md` §Testing Decisions. Killing all surviving mutations in these two BCs is the end-to-end behaviour this ticket makes work.
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from the participant and portal BCs reduced to 0.
- [ ] `search_participants` has parametrised boundary tests for the dispatch tables — every `and → or`, `== → !=`, `> → >=` flip from the audit's enumeration is exercised.
- [ ] `FinancialScope.from_json` / `to_json` round-trip a non-trivial JSONB shape (unit IDs + participant IDs + inheritance flags) and assert each typed field.
- [ ] `_make_handler` (audit projection) has a test that asserts a single dispatched event produces exactly one `InternalNotification` row + one audit row.
- [ ] No production code under `ksma.participant` or `ksma.portal` is modified.
- [ ] `pytest tests/unit -q` exits 0.
