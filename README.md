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

## Quick links

- [Pilot ticket graph](.scratch/ksma-pilot/issues/) — 14 vertical slices, blockers first.
- [Architecture decision records](docs/adr/) — `0001` scope, `0002` DDD,
  `0003` work items, `0004` action registry, `0005` seed, `0006` auth.
- [Spec index](docs/specs/) — `000` umbrella, `001`–`008` per BC, `009` seed+tests.

## Status

| Layer | Status |
|---|---|
| Source documents | ✅ PRD, Architecture, Access Matrix, Data Dictionary, UX Blueprint (in `docs/`) |
| ADRs | ✅ `0001`–`0006` written |
| Specs | ✅ 10 spec files (`docs/specs/`) |
| Tickets | ✅ 14 vertical-slice tickets (`.scratch/ksma-pilot/issues/`) |
| Backend scaffold | 🚧 Mostly implemented; see `kitapandu-backend/` |
| Tests | ❌ Not yet written (ticket 11) |
| Seed | ❌ Placeholder (ticket 10) |

See `.scratch/ksma-pilot/issues/` for the agent-grabbable work graph.