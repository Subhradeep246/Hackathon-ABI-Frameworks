.PHONY: help bootstrap env install deps-system deps-py deps-web db-init db-reset \
	migrate up down logs psql redis-cli \
	sync extract decide scale-test test lint fmt typecheck check ci demo clean

SHELL := /bin/bash

# Postgres path varies between Intel and Apple Silicon
ifeq ($(shell uname -m),arm64)
  BREW_PREFIX := /opt/homebrew
else
  BREW_PREFIX := /usr/local
endif
PG_BIN := $(BREW_PREFIX)/opt/postgresql@16/bin

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ── First-time setup ──────────────────────────────────────────────────
bootstrap: env install db-init migrate ## One-command setup from a clean clone
	@echo ""
	@echo "✓ Bootstrap complete."
	@echo "  Run \`make up\` to start the stack (api + web + prefect)."
	@echo ""
	@echo "  API:      http://localhost:8000/healthz"
	@echo "  Web:      http://localhost:3000"
	@echo "  Prefect:  http://localhost:4200"

env: ## Copy .env.example to .env if not present
	@test -f .env || (cp .env.example .env && echo "✓ Created .env from .env.example")

install: deps-system deps-py deps-web ## Install Postgres, Redis, Python deps, Node deps

deps-system: ## Install + start Postgres 16 and Redis via Homebrew
	@command -v brew >/dev/null 2>&1 || { echo "Homebrew not found. Install: https://brew.sh"; exit 1; }
	@brew list postgresql@16 >/dev/null 2>&1 || brew install postgresql@16
	@brew list redis >/dev/null 2>&1 || brew install redis
	@brew services start postgresql@16
	@brew services start redis
	@sleep 2  # let postgres open the socket

deps-py: ## Install Python deps via uv
	@command -v uv >/dev/null 2>&1 || { echo "uv not found. Install: brew install uv"; exit 1; }
	uv sync --extra dev

deps-web: ## Install Node deps
	@command -v npm >/dev/null 2>&1 || { echo "npm not found. Install: brew install node@20"; exit 1; }
	cd web && npm install --no-fund --no-audit

db-init: ## Create the woundiq role + database (idempotent)
	@$(PG_BIN)/psql -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='woundiq'" | grep -q 1 \
		|| $(PG_BIN)/psql -d postgres -c "CREATE ROLE woundiq LOGIN PASSWORD 'woundiq_dev_only'"
	@$(PG_BIN)/psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='woundiq'" | grep -q 1 \
		|| $(PG_BIN)/psql -d postgres -c "CREATE DATABASE woundiq OWNER woundiq"
	@$(PG_BIN)/psql -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE woundiq TO woundiq" >/dev/null
	@echo "✓ Database 'woundiq' ready."

db-reset: ## Drop + recreate the woundiq database (destroys local data)
	@$(PG_BIN)/psql -d postgres -c "DROP DATABASE IF EXISTS woundiq"
	@$(MAKE) db-init
	@$(MAKE) migrate

migrate: ## Run Alembic migrations
	uv run alembic upgrade head

# ── Stack lifecycle ───────────────────────────────────────────────────
up: ## Start api, worker (prefect), web in one terminal via honcho
	uv run honcho start

down: ## Stop Postgres + Redis system services
	-brew services stop postgresql@16
	-brew services stop redis

# ── Database helpers ──────────────────────────────────────────────────
psql: ## psql shell against the woundiq database
	$(PG_BIN)/psql -U woundiq -d woundiq -h localhost

redis-cli: ## redis-cli shell
	redis-cli

# ── Pipeline ops (Phase 1+) ───────────────────────────────────────────
sync: ## Run the ingestion flow against the live mock PCC API
	uv run python -m pipeline.sync

extract: ## Run the extraction flow
	uv run python -m pipeline.extract

decide: ## Run the eligibility decision flow
	uv run python -m pipeline.decide

scale-test: ## Generate N synthetic patients and benchmark (override N=...)
	uv run python -m synthetic.generator --n $${N:-1000000}

# ── Quality ───────────────────────────────────────────────────────────
test: ## Run pytest
	uv run pytest -ra

lint: ## ruff check
	uv run ruff check .

fmt: ## ruff --fix + black
	uv run ruff check --fix .
	uv run black .

typecheck: ## mypy
	uv run mypy

check: lint typecheck test ## All quality gates

ci: ## What GitHub Actions runs
	uv run ruff check .
	uv run black --check .
	uv run mypy
	uv run pytest -ra

# ── Demo ──────────────────────────────────────────────────────────────
demo: bootstrap up ## Bring the full stack up for a live demo

# ── Housekeeping ──────────────────────────────────────────────────────
clean: down ## Stop background services
