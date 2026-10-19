# KitaPandu School Management (KSMA)

Pilot iteration of the KitaPandu School Management backend — a multi-tenant
school operations platform for Vaneta Maju Berdaya and beyond.

## What's here

- **`docs/`** — Source PDFs (KSMA v1.0: PRD, Architecture & Data Model,
  Access Control Matrix, Data Dictionary & Validation, UX Blueprint) plus
  architectural decision records (`docs/adr/`).
- **`docs/specs/`** — Per-bounded-context specs (umbrella + identity +
  participant + program + session + assessment + finance + portal +
  operations + seed/test).
- **`docs/adr/`** — Architecture decision records (scope, DDD layout,
  work-items via service request, action registry, seed, auth).
- **`GLOSSARY.md`** — Canonical vocabulary in Indonesian/PascalCase.
- **`kitapandu-backend/`** — FastAPI + SQLAlchemy 2.x + PostgreSQL backend.
- **`.scratch/ksma-pilot/issues/`** — Per-ticket work breakdown that maps the
  spec series to vertical slices ready for an agent.
- **`Makefile`** — Top-level entry points (`make up`, `make migrate`,
  `make test`, `make smoke`, …) — see the `help` target for the full list.

## Quick links

- [Pilot ticket graph](.scratch/ksma-pilot/issues/) — 14 vertical slices, blockers first.
- [Architecture decision records](docs/adr/) — `0001` scope, `0002` DDD,
  `0003` work items, `0004` action registry, `0005` seed, `0006` auth.
- [Spec index](docs/specs/) — `000` umbrella, `001`–`008` per BC, `009` seed+tests.
- [Backend README](kitapandu-backend/README.md) — module layout, dev quick-start,
  ADR pointers, PRD↔module traceability.
- [Top-level Makefile](Makefile) — `make help` prints every target.

## Pilot ticket layout

The pilot work is split into two parallel taxonomies that should be read
together:

- **Per-BC specs** in [`docs/specs/`](docs/specs/) — the canonical product
  surface (10 files: `000` umbrella + `001`–`008` per BC + `009` seed + test data).
- **Vertical-slice tickets** in
  [`.scratch/ksma-pilot/issues/`](.scratch/ksma-pilot/issues/) — 14 implementation
  tickets (`00`–`13`) that map the specs to agent-grabbable work.

Recommended reading order:

1. `00-foundations-shared-kernel.md` — shared kernel, money, tenancy, RBAC, outbox.
2. `01-schema-baseline.md` — Alembic baseline + the 55-entity migration plan.
3. `02`–`09` — bounded-context slices in this order: identity,
   participant + enrollment, program + class, session, finance,
   assessment + reporting, portal, operations.
4. `10-seed-cli.md` — Vaneta DEMO seed + tenancy bootstrap.
5. `11-test-pyramid.md` — unit + integration + acceptance tests (V01–V24).
6. `12-smoke-pilot.md` — end-to-end smoke CLI that exercises the happy paths.
7. `13-readme-and-adr-pointer-fix.md` — root Makefile + ADR-pointer cleanup
   (this ticket).

The specs describe *what* to build; the tickets describe *how* to slice it.
Read both side-by-side before starting a slice.

## Status

| Layer | Status |
|---|---|
| Source documents | ✅ PRD, Architecture, Access Matrix, Data Dictionary, UX Blueprint (in `docs/`) |
| ADRs | ✅ `0001`–`0006` written |
| Specs | ✅ 10 spec files (`docs/specs/`) |
| Tickets | ✅ 14 vertical-slice tickets (`.scratch/ksma-pilot/issues/`) |
| Backend scaffold | 🚧 `identity`, `participant`, `program`, `batch`, `session`, `assessment`, `reporting`, `finance`, `portal`, `shared/*` implemented in `kitapandu-backend/src/ksma/` |
| Tests | 🚧 Unit + integration (shared-kernel + finance V13–V18) wired; V01–V24 e2e scenarios tracked under ticket 11 |
| Seed | ✅ `python -m ksma.scripts.seed_vaneta_demo` (ticket 10) |
| Smoke pilot | ⏳ Tracked under ticket 12 |
| Build / DX | ✅ Root Makefile + ADR-pointer fix (ticket 13 — this ticket) |

See `.scratch/ksma-pilot/issues/` for the agent-grabbable work graph.