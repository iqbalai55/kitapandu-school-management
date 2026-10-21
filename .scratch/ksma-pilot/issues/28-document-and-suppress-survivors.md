# 28: Document and suppress genuinely-unreachable mutants — `# pragma: no mutate` with named justification

**Parent spec:** #16 (Kill the 337-survivor gap) — Implementation Decision "Suppress only with documented justification"
**What to build:** For every surviving mutation that remains in `.metrics/mutator.txt` after tickets #17 through #27 land, either (a) it is genuinely unreachable in the current code path and the production-code deletion is filed as a follow-up source-change ticket with an ADR pair; or (b) the suppression is annotated at the source line with a `# pragma: no mutate` comment that names the operator being suppressed and the test fixture (or ADR) that justifies the suppression. Bare suppressions (with no justification) are forbidden by the spec. All remaining survivors are reduced to a narrow, enumerable set whose every line is either a justified suppression or the parent of a new ADR-trackable source-change ticket. This is the end-to-end behaviour this ticket makes work — it converts "we have survivors" into "every survivor has a documented reason".
**Blocked by:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27 — must land first so the residual-survivor list is known.
**Status:** ready-for-agent

- [ ] Every `SURVIVED` line remaining in `.metrics/mutator.txt` after the wave is either:
  - annotated at the source line with `# pragma: no mutate` plus a sibling `# justification:` comment that names the operator and the test fixture (by relative path + line range) or the ADR (by number) that justifies the suppression, OR
  - referenced by a new ticket number on the issue tracker that opens a follow-up source-change ticket with an ADR pair.
- [ ] No bare `# pragma: no mutate` annotations exist anywhere in the source tree.
- [ ] `git grep "pragma: no mutate"` returns only annotated+justified hits.
- [ ] `.metrics/mutator.txt` residual `SURVIVED` count is documented in `.metrics/harness-report.md` `## Surviving mutants`.
- [ ] `mutator --since-last-run --max-workers 2 --no-coverage --test-command "...tests/unit..."` runs to completion without producing any new unannotated survivor.
- [ ] `pytest tests/unit -q` exits 0.
