# Contributing to Wound IQ

This is the developer setup guide. For the hackathon brief, see [`README.md`](./README.md). For the full design, see [`PRD.md`](./PRD.md).

---

## Prerequisites

- **Docker Desktop** (or OrbStack) — for the Postgres / Redis / Prefect / API / web / Grafana stack
- **Python 3.11+**
- **[`uv`](https://docs.astral.sh/uv/)** — Python package manager: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Node.js 20+** + `npm` — only needed if you want to run the web app outside Docker

Confirm:

```bash
docker --version
python3 --version  # 3.11+
uv --version
```

---

## One-command bootstrap

```bash
make bootstrap
```

This will:
1. Copy `.env.example` → `.env` (only if `.env` doesn't already exist)
2. Install Python deps locally (for tests + tooling)
3. Bring up the full Docker Compose stack
4. Run Alembic migrations against Postgres

When it finishes, these URLs are live:

| Service | URL |
|---|---|
| Next.js dashboard | http://localhost:3000 |
| FastAPI | http://localhost:8000/healthz |
| Prefect UI | http://localhost:4200 |
| Grafana | http://localhost:3001 |
| Prometheus | http://localhost:9090 |
| Postgres | `localhost:5432` (user/pass in `.env`) |

---

## Makefile cheatsheet

```
make help          # show all targets
make bootstrap     # first-time setup (idempotent)
make up            # bring stack up
make down          # bring stack down (keeps volumes)
make nuke          # bring stack down + wipe volumes (destroys local data)
make migrate       # run Alembic migrations
make logs          # tail all service logs
make ps            # show service status
make psql          # psql shell on the running Postgres
make test          # run pytest
make lint          # ruff check
make fmt           # ruff --fix + black
make typecheck     # mypy
make check         # lint + typecheck + test
make ci            # what GitHub Actions runs
```

Phase 1+ targets (will work once those phases ship):

```
make sync          # ingest from the PCC mock API
make extract       # extract wound fields
make decide        # run eligibility rules
make scale-test N=1000000   # synthetic data benchmark
```

---

## Local dev without Docker

Most workflows go through Docker, but you can run tests and tooling locally:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy
```

The web app outside Docker:

```bash
cd web
npm install
npm run dev
```

---

## Environment variables

`.env.example` is the source of truth. Copy it to `.env` and fill in real values when you have them. Baseten + 11Labs keys are not required for Phase 0.

Sensitive keys never get committed — `.env` is gitignored. If you accidentally commit one, rotate it and force-push the fix.

---

## Project layout

```
.
├── api/              FastAPI app + shared config + observability
├── pipeline/         Prefect flows + Alembic migrations + ingestion (Phase 1)
├── extraction/       regex + Presidio (PHI redaction) + Baseten LLM (Phase 2)
├── eligibility/      Decision rules + ICD-10 wound codes (Phase 3)
├── synthetic/        1M-row data generator (Phase 4.5)
├── web/              Next.js 14 (App Router) dashboard
├── infra/            Dockerfiles, prometheus.yml, grafana provisioning
├── tests/            pytest
├── .github/          CI workflows
├── docker-compose.yml
├── Makefile          single entry point for everything
├── pyproject.toml    Python deps + tool configs (ruff, black, mypy, pytest)
├── alembic.ini
└── PRD.md            full design doc
```

---

## Workflow

- Work on a feature branch (e.g. `phase-1-ingestion`)
- Open a PR against `main`
- CI runs ruff + black --check + mypy + pytest + Next.js typecheck/lint
- Squash-merge when green

---

## What's NOT in here (production gaps)

See [`PRODUCTION_GAPS.md`](./PRODUCTION_GAPS.md) for the honest list of what we'd add for real production: HA, RBAC, secret management, distributed tracing, HIPAA controls, backups/DR, cost guardrails. These are intentional deferrals — the PRD documents the path.
