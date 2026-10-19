# 13: Wide refactor — README ADR-pointer fix + Makefile

**What to build:** Find-and-replace for two docs-only inconsistencies between the existing `kitapandu-backend/README.md` / root `README.md` and the actual ADRs in `docs/adr/`, plus a Makefile that codifies the `make up`, `make migrate`, `make seed`, `make lint`, `make test`, `make test-acceptance`, `make smoke` targets. This is a docs-only wide refactor with no blast radius beyond text and a single Makefile.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Update the `kitapandu-backend/README.md` so the `## Architectural decisions` section references the ADRs that actually exist in `docs/adr/` (`0001-scope-pilot-acceptance.md`, `0002-ddd-aggregate-boundaries.md`, `0003-work-item-via-service-request.md`, `0004-action-registry-and-masking.md`, `0005-seed-and-tenancy-bootstrap.md`, `0006-auth-and-jwt.md`) instead of the non-existent `0001-architecture-foundations.md`.
- [ ] Create a root `Makefile` (at `KitaPandu School Management/`) with targets `up` (`docker compose -f kitapandu-backend/docker-compose.yml up -d postgres minio minio-init`), `down` (the symmetric), `migrate` (`cd kitapandu-backend && alembic upgrade head`), `seed` (`python -m ksma.scripts.seed_vaneta_demo`), `lint` (`ruff check kitapandu-backend/src`), `typecheck` (`mypy kitapandu-backend/src`), `test` (`pytest kitapandu-backend/tests/unit kitapandu-backend/tests/integration`), `test-acceptance` (`pytest kitapandu-backend/tests/e2e -m scenario`), `smoke` (`python -m ksma.scripts.smoke_pilot`), `worker` (`python -m ksma.shared.outbox.run`), `dev` (`uvicorn ksma.main:app --reload --port 8000` from `kitapandu-backend/`).
- [ ] Add an `## Pilot ticket layout` section to the root `README.md` linking to `.scratch/ksma-pilot/issues/` and `docs/specs/`.
- [ ] Verify `make -n up` and `make -n migrate` print the expected commands without running them; no shell expansion errors.