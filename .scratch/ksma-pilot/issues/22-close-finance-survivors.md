# 22: Kill the 69 `finance/application` + `finance/infrastructure/repositories` survivors — pin V13 / V14 / V15 / V16 / V17 / V18 finance invariants

**Parent spec:** #16 (Kill the 337-survivor gap)
**What to build:** A unit-test layer for `ksma.finance.application` (57 surviving mutations, the hottest file in the wave) and `ksma.finance.infrastructure.repositories` (12 surviving mutations). Each test pins a V13–V18 invariant from `docs/specs/006-finance.md` §Testing Decisions: V13 installment sum, V14 recognised-vs-claimed delta, V15 allocation capacity, V16 concurrent allocation serialisation, V17 idempotency, V18 reversal-without-allocation rejection. Killing the 69 surviving mutations in this BC is the end-to-end behaviour this ticket makes work — closing this single file is the largest single-ticket kill-rate gain (≈ +10 percentage points on the headline 374/711).
**Blocked by:** None (independent).
**Status:** ready-for-agent

- [ ] `mutator --since-last-run --max-workers 2 --no-coverage` after this ticket reports `SURVIVED` lines from `ksma.finance.application` and `ksma.finance.infrastructure.repositories` reduced to 0.
- [ ] `compute_invoice_projection` has a positive assertion on the recognised-vs-claimed delta (not just `result is not None`) — this is the worst-CRAP function in the assessment and the first target for the wave.
- [ ] `allocate_funds` has a parametrised test that asserts two concurrent allocations against the same installment cannot both succeed (V15) and that the same idempotency key produces one effect (R21).
- [ ] `invoice_credit`, `verify_payment`, `void_invoice`, `_seed_invoice_with_payment_alloc` each have at least one positive assertion pinning the documented invariant.
- [ ] No production code under `ksma.finance` is modified.
- [ ] `pytest tests/unit -q` exits 0.
