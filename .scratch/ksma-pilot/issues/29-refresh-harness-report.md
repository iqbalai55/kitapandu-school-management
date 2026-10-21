# 29: Refresh `.metrics/harness-report.md` end-to-end and verify the ≥ 95 % gate

**Parent spec:** #16 (Kill the 337-survivor gap) — Implementation Decision "Harness-report refresh"
**What to build:** Re-run the full audit harness — `make harness-report` (or the underlying `crapper`, `dryer`, `mutator` chain with the spec's documented flags) — and regenerate `.metrics/harness-report.md` from `.metrics/{mutator,dryer,crapper}.txt`. Verify that the `Change resilience:` verdict moves from `FAIL (374/711, 52.6 %)` to either `PASS (≥ 95 %)` or a narrower `FAIL` whose surviving-mutation count is ≤ 35 and every survivor is explicitly enumerated. Cross-references to `#16`, `#14`, `#15`, `#13` are updated to reflect the post-wave status. Closing the cycle — measurable kill rate, enumerated residuals, refreshed report — is the end-to-end behaviour this ticket makes work.
**Blocked by:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28 — must land first so the residual-survivor set is known and documented.
**Status:** ready-for-agent

- [ ] `make harness-report` exits 0 (verification step) with `.metrics/harness-report.md` non-empty.
- [ ] All five acceptance-gate headings (`Executive verdict`, `CRAP`, `Duplicates`, `Surviving mutants`, `Recommendations`) are present in the regenerated `.metrics/harness-report.md`.
- [ ] The `Change resilience:` line reads `PASS (≥ 95 %)` OR `FAIL (K/N, P %)` with `P ≥ 95` OR `FAIL (K/N, P %); ≤ 35 survivors` with every survivor enumerated.
- [ ] Cross-references to `#16`, `#14`, `#15`, `#13` are updated to reflect post-wave status (e.g. `#16 closed on YYYY-MM-DD with X/N killed`).
- [ ] `crapper --root .` and `dryer --root .` outputs are re-checked: the duplication count of 177 pairs and the CRAP top-function ranking are unchanged from the pre-wave baseline (no source-code change means no movement in these axes).
- [ ] The diff against `main` in `.metrics/harness-report.md` is bounded to the `Executive verdict` line, the `## Surviving mutants` table, and the `## Recommendations` block — no new prose deviates from the spec template.
- [ ] `git commit` is performed (per the user-facing branch policy) on the `fix/mutator-baseline-2026-10-20` branch (or its successor) — this commit is the spec's close.
