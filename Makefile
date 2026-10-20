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