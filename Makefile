# Top-level Makefile for the KitaPandu School Management (KSMA) pilot.
#
# All targets are self-contained: they `cd` to the right directory using an
# absolute path and do not rely on Make variables or the caller's CWD.
# This means `make -n <target>` correctly dry-runs the expected shell,
# and every target can be invoked from anywhere on the filesystem.

.DEFAULT_GOAL := help

.PHONY: help
help:                   ## Print this help (default goal)
	@echo "KitaPandu School Management (KSMA) — Makefile"
	@echo ""
	@echo "Bootstrap:"
	@echo "  up                 docker compose up postgres + minio (background)"
	@echo "  down               docker compose down postgres + minio"
	@echo "  migrate            apply alembic migrations to head"
	@echo "  seed               seed the Vaneta DEMO tenant + demo data"
	@echo "  worker             run the outbox worker (foreground)"
	@echo "  dev                run the FastAPI dev server with reload"
	@echo ""
	@echo "Quality:"
	@echo "  lint               ruff check the backend package"
	@echo "  typecheck          mypy the backend source tree"
	@echo ""
	@echo "Tests:"
	@echo "  test               run unit + integration pytest"
	@echo "  test-acceptance    run the V01..V24 PRD scenario pytest"
	@echo "  smoke              run the end-to-end smoke pilot script"
	@echo ""
	@echo "Harness:"
	@echo "  mutation           run mutator over the unit-test suite (kill-rate)"
	@echo "  harness-report     run crapper + dryer + mutator and write the verdict"
	@echo ""
	@echo "Usage:  make <target>   (or  make -n <target>  to dry-run)"

.PHONY: up
up:                    ## docker compose up postgres + minio (background)
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management && docker compose -f kitapandu-backend/docker-compose.yml up -d postgres minio

.PHONY: down
down:                  ## docker compose down postgres + minio
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management && docker compose -f kitapandu-backend/docker-compose.yml down

.PHONY: migrate
migrate:               ## alembic upgrade head
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend && alembic upgrade head

.PHONY: seed
seed:                  ## seed the Vaneta DEMO tenant
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend && python -m ksma.scripts.seed_vaneta_demo

.PHONY: lint
lint:                  ## ruff check the backend package
	ruff check /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend

.PHONY: typecheck
typecheck:             ## mypy the backend source tree
	mypy /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend/src

.PHONY: test
test:                  ## pytest unit + integration
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend && pytest tests/unit tests/integration

.PHONY: test-acceptance
test-acceptance:       ## pytest V01..V24 PRD scenarios
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend && pytest tests/e2e -m scenario

.PHONY: smoke
smoke:                 ## run the smoke pilot script
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend && python -m ksma.scripts.smoke_pilot

.PHONY: worker
worker:                ## run the outbox worker
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend && python -m ksma.shared.outbox.run

.PHONY: dev
dev:                   ## uvicorn dev server with reload on :8000
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management/kitapandu-backend && uvicorn ksma.main:app --reload --port 8000

# Mutation target.
#
# Invokes the `mutator` harness over the kitapandu-backend unit suite.
# The `--test-command` runs from the project root (not the backend dir)
# so it must `cd kitapandu-backend` first. `--max-workers 2` caps the RAM
# budget per the original audit. `--no-coverage` treats every discovered
# site as in-scope (unit-only run; coverage is not a gate here).
#
# Output is piped to stdout — JSON-like lines for each mutant plus a
# final `[mutator] <killed>/<total>` summary line that exposes the kill
# rate. Exit code mirrors mutator's: 0 every mutant killed (or nothing
# to run), 2 baseline tests failed, 3 at least one mutant survived.
.PHONY: mutation
mutation:              ## run mutator against the unit-test suite
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management && \
	mutator --root . \
		--test-command "bash -c 'cd kitapandu-backend && python3 -m pytest tests/unit -q --no-header --no-cov'" \
		--max-workers 2 \
		--no-coverage 2>&1

# Harness report target.
#
# Runs the three harness tools in order (crapper for CRAP, dryer for
# structural duplication, mutator for change resilience) and verifies
# .metrics/harness-report.md is in place. All raw artefacts land in
# .metrics/ (crap.edn, dry.edn, mutate/*.edn, mutator.txt, ...).
#
# The verdict document .metrics/harness-report.md is written by the
# unclebob-metrics-assessor skill from those raw artefacts — `make`
# alone does not author prose. Re-run the skill (or update the file by
# hand using the existing template) to refresh the verdict.
#
# .metrics/ is git-ignored; only harness-report.md is tracked.
.PHONY: harness-report
harness-report: crapper dryer mutation   ## run crapper + dryer + mutator; verify report
	@echo "[harness-report] raw artefacts written to .metrics/"
	@test -s .metrics/harness-report.md \
		|| { echo "ERROR: .metrics/harness-report.md missing or empty — run the unclebob-metrics-assessor skill to author it from .metrics/*"; exit 1; }
	@echo "[harness-report] verdict present: .metrics/harness-report.md"

.PHONY: crapper
crapper:               ## run crapper (CRAP metric) over the backend
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management && crapper --root .

.PHONY: dryer
dryer:                 ## run dryer (structural duplication) over the backend
	cd /home/panduai/repo/pandu-ai-id/kitapandu-school-management && dryer --root .