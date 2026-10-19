# KSMA Pilot — comprehensive ticket graph

This file is the source-of-truth for the **second-wave comprehensive tickets**
that supplement the per-spec specs in `docs/specs/`. Every ticket maps to a
BC surface plus a unit/integration test layer.

The ticket set was published to GitHub Issues **#16–#31** on
<https://github.com/iqbalai55/kitapandu-school-management> with labels
`pilot`, `phase-2:comprehensive`, and `wave:{foundation|bc|consolidate|ops}`.

## Issue ↔ ticket mapping

| Issue | Code | Title | Blocked by |
|---|---|---|---|
| #16 | P0.1 | Foundations: Money + Clock + JWT + Argon2id | — |
| #17 | P0.2 | Foundations: Tenancy + RBAC + Idempotency + Outbox | — |
| #18 | P0.3 | Foundations: Errors + Audit + Attachments + Persistence + Events + Logging | — |
| #19 | P1 | Schema baseline: full RLS + btree_gist exclusion + ENUM types | #17 |
| #20 | P2.1 | Identity BC comprehensive | #16, #17, #18, #19 |
| #21 | P2.2 | Participant BC comprehensive | #16, #17, #18, #19, #20 |
| #22 | P2.3 | Program BC comprehensive | #16, #17, #18, #19, #20 |
| #23 | P2.4 | Session BC comprehensive | #16, #17, #18, #19, #20, #21, #22 |
| #24 | P2.5 | Finance BC comprehensive | #16, #17, #18, #19, #20, #21 |
| #25 | P2.6 | Assessment + Reporting BCs comprehensive | #16, #17, #18, #19, #20, #21, #22, #23 |
| #26 | P2.7 | Portal BC comprehensive | #16, #17, #18, #19, #20, #21, #22, #23, #24, #25 |
| #27 | P2.8 | Operations BC comprehensive | #16, #17, #18, #19, #20 |
| #28 | P3.1 | Seed CLI: two-tenant idempotent demo with full happy-path chain | #16, #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27 |
| #29 | P3.2 | Test pyramid: unit + integration (testcontainers) + V01–V24 e2e scenarios | #16, #17, #18, #19, #28 |
| #30 | P3.3 | Smoke pilot: end-to-end happy-path script + Makefile + CI | #28, #29 |
| #31 | P4.1 | Observability + container hardening: OTel, audit-projection consumer, recovery drill test | #18, #26 |

## Ticket bodies

Each ticket's full acceptance summary, blocked-by list, and surface enumeration
lives in two places:

1. The issue body on GitHub (issue numbers above).
2. The local `.scratch/ksma-pilot/issues/<NN>-<slug>.md` file (numbered to
   match the new comprehensive order, not the closed 14-ticket set).

## Spec ↔ ticket mapping

| Spec | Comprehensive ticket(s) |
|---|---|
| `docs/specs/000-umbrella-platform.md` | #16, #18, #19, #31 |
| `docs/specs/001-identity.md` | #20 |
| `docs/specs/002-participant.md` | #21 |
| `docs/specs/003-program-and-class.md` | #22 |
| `docs/specs/004-session.md` | #23 |
| `docs/specs/005-assessment-and-reporting.md` | #25 |
| `docs/specs/006-finance.md` | #24 |
| `docs/specs/007-portal.md` | #26 |
| `docs/specs/008-operations.md` | #27 |
| `docs/specs/009-seed-and-test-data.md` | #28, #29, #30 |

## Old tickets (superseded)

The first-wave vertical-slice tickets (#2–#15) have been closed with a
"superseded by" comment pointing at this new set. Issue #13 (seed CLI) was
left open because it corresponds directly to #28 (P3.1) in the new set.

## Frontier — what can start now

Tickets **#16, #17, #18** have no blockers and can be picked up immediately
by an agent. Everything else waits on at least one of them.