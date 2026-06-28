.PHONY: help bootstrap env install migrate up down logs ps shell db psql redis-cli \
	sync extract decide scale-test test lint fmt typecheck check ci demo clean nuke

SHELL := /bin/bash
COMPOSE := docker compose

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ── First-time setup ──────────────────────────────────────────────────
bootstrap: env install up migrate ## One-command bootstrap from a clean clone
	@echo ""
	@echo "✓ Bootstrap complete."
	@echo "  API:        http://localhost:8000/healthz"
	@echo "  Web:        http://localhost:3000"
	@echo "  Prefect:    http://localhost:4200"
	@echo "  Grafana:    http://localhost:3001"
	@echo "  Prometheus: http://localhost:9090"

env: ## Copy .env.example to .env if .env doesn't exist
	@test -f .env || (cp .env.example .env && echo "✓ Created .env from .env.example — edit it with real keys")

install: ## Install Python deps locally for tests + tooling
	@command -v uv >/dev/null 2>&1 || (echo "uv not found. Install: https://docs.astral.sh/uv/" && exit 1)
	uv sync --extra dev || uv pip install --system -e ".[dev]"

# ── Stack lifecycle ───────────────────────────────────────────────────
up: ## Bring the stack up in the background
	$(COMPOSE) up -d --build

down: ## Stop the stack
	$(COMPOSE) down

logs: ## Tail logs from all services
	$(COMPOSE) logs -f --tail=100

ps: ## Show running services
	$(COMPOSE) ps

# ── Database ──────────────────────────────────────────────────────────
migrate: ## Run Alembic migrations against the running Postgres
	$(COMPOSE) exec -T api alembic upgrade head

psql: ## Open a psql shell on the running Postgres
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-woundiq} -d $${POSTGRES_DB:-woundiq}

redis-cli: ## Open a redis-cli shell on the running Redis
	$(COMPOSE) exec redis redis-cli

# ── Pipeline ops (Phase 1+) ───────────────────────────────────────────
sync: ## Run the ingestion flow against the live mock API
	$(COMPOSE) exec -T worker python -m pipeline.sync

extract: ## Run the extraction flow
	$(COMPOSE) exec -T worker python -m pipeline.extract

decide: ## Run the eligibility decision flow
	$(COMPOSE) exec -T worker python -m pipeline.decide

scale-test: ## Generate N synthetic patients and benchmark (override N=...)
	$(COMPOSE) exec -T worker python -m synthetic.generator --n $${N:-1000000}

# ── Quality ───────────────────────────────────────────────────────────
test: ## Run pytest
	$(COMPOSE) exec -T api pytest -ra

lint: ## ruff check
	uv run ruff check .

fmt: ## Format with ruff + black
	uv run ruff check --fix .
	uv run black .

typecheck: ## mypy
	uv run mypy

check: lint typecheck test ## Run all quality gates

ci: ## What CI runs (lint + typecheck + test, no docker)
	uv run ruff check .
	uv run black --check .
	uv run mypy
	uv run pytest -ra

# ── Demo ──────────────────────────────────────────────────────────────
demo: bootstrap ## Bring up the stack for a live demo
	@echo ""
	@echo "✓ Demo stack is up. Open http://localhost:3000"

# ── Housekeeping ──────────────────────────────────────────────────────
shell: ## Shell into the api container
	$(COMPOSE) exec api /bin/bash

clean: ## Stop containers, keep volumes
	$(COMPOSE) down

nuke: ## Stop everything and delete volumes (destroys local data)
	$(COMPOSE) down -v
