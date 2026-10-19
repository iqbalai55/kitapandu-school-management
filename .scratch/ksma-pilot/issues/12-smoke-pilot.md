# 12: Smoke pilot — end-to-end happy-path script

**What to build:** A single CLI command `python -m ksma.scripts.smoke_pilot` that exercises the demo chain from login through receipt download and asserts the projected state matches the expected state. Used as the demo-ready proof that the whole stack is wired correctly, and as a fast smoke gate before each release.

**Blocked by:** 10 (seed), 11 (test pyramid)

**Status:** ready-for-agent

- [ ] `ksma.scripts.smoke_pilot` runs against the seeded dev DB and prints a pass/fail summary.
- [ ] Step 1: log in as `ac@vaneta.id` (AC) — expect 200 + access token.
- [ ] Step 2: read `GET /resources/me/memberships` — expect one membership for `Vaneta DEMO`.
- [ ] Step 3: read `GET /participants?q=Sari` — expect two candidates with `duplicate_suspect=true` (P-0001 + P-0006).
- [ ] Step 4: read `GET /batches/{EN-A1-Okt2026}/capacity` — expect `current_capacity=10`, `occupied=2` (from seeded enrollments).
- [ ] Step 5: log in as `fi@vaneta.id` (FI), read `GET /invoices/INV-VANETA-0001` — expect `paid_status='paid'`, `arrears=0` (because of seeded allocation).
- [ ] Step 6: read `GET /invoices/INV-VANETA-0002` — expect `paid_status='unpaid'`, `arrears=0` (installments not yet due).
- [ ] Step 7: log in as `in@vaneta.id` (IN), read `GET /sessions/{first}` — expect effective revision + attendance list.
- [ ] Step 8: read `GET /portal/inbox` for `st@vaneta.id` (ST) — expect at least one notification row from the seeded allocation (`InvoiceIssued` or `PaymentVerified`).
- [ ] Step 9: read `GET /audit-trail?action_id=FIN-A05` — expect one row from the seeded allocation.
- [ ] Step 10: print "PILOT SMOKE PASS" or "PILOT SMOKE FAIL: <step>" with appropriate exit code.
- [ ] Documented in the root `README.md` and in `docs/specs/009-seed-and-test-data.md` as the demo proof.