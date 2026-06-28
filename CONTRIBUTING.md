# Contributing to Wound IQ

This is the developer setup guide. For the hackathon brief, see [`README.md`](./README.md). For the full design, see [`PRD.md`](./PRD.md).

Dev environment is **native macOS** — Postgres + Redis run via Homebrew, the app processes (FastAPI, Prefect, Next.js) run via [honcho](https://honcho.readthedocs.io/) in a single terminal. No Docker required.

---

## Prerequisites

Install these once. All available via Homebrew:

```bash
brew install postgresql@16 redis node@20 uv
```

Confirm:

```bash
postgres --version    # PostgreSQL 16.x
redis-server --version
node --version        # v20.x
uv --version
```

If you don't have Homebrew, install it first: https://brew.sh

---

## One-command bootstrap

```bash
make bootstrap
```

This will:
1. Copy `.env.example` → `.env` (only if `.env` doesn't already exist)
2. Start Postgres + Redis as `brew services` (system daemons)
3. Install Python deps via `uv sync`
4. Install Node deps via `npm install` in `web/`
5. Create the `woundiq` Postgres role + database (idempotent)
6. Run Alembic migrations

When it finishes, run `make up`. That launches the app stack in one terminal.

| Service | URL |
|---|---|
| Next.js dashboard | http://localhost:3000 |
| FastAPI | http://localhost:8000/healthz |
| Prefect UI | http://localhost:4200 |
| Postgres | `localhost:5432` (user/pass in `.env`) |
| Redis | `localhost:6379` |

To stop everything: `Ctrl-C` in the `make up` terminal, then `make down` to stop Postgres + Redis.

---

## Makefile cheatsheet

```
make help          # show all targets
make bootstrap     # first-time setup (idempotent)
make up            # start api + web + prefect (foreground, one terminal)
make down          # stop Postgres + Redis system services
make migrate       # run Alembic migrations
make psql          # psql shell on woundiq database
make redis-cli     # redis-cli shell
make db-reset      # drop + recreate woundiq DB (destroys local data)
make test          # run pytest
make lint          # ruff check
make fmt           # ruff --fix + black
make typecheck     # mypy
make check         # lint + typecheck + test
make ci            # what GitHub Actions runs
```

Phase 1+ targets (work once those phases ship):

```
make sync          # ingest from the PCC mock API
make extract       # extract wound fields
make decide        # run eligibility rules
make scale-test N=1000000   # synthetic data benchmark
```

---

## Day-to-day workflow

Open two terminals (or use a tmux split):

| Terminal | What's running |
|---|---|
| 1 | `make up` — api, web, prefect (live logs, color-coded by process) |
| 2 | Free for `make test`, `make psql`, `make sync`, git, etc. |

Postgres + Redis stay running in the background as system services. They survive reboots until you `make down` or `brew services stop`.

---

## Why no Docker?

Originally Phase 0 used Docker Compose. We dropped it for local dev because:

- **Faster startup** — native processes start in seconds, no daemon, no VM
- **Lower memory** — no Docker VM eating 2-3 GB on macOS
- **Easier debugging** — attach debugger directly, logs in the terminal

Docker comes back for **production** — see [`PRODUCTION_GAPS.md`](./PRODUCTION_GAPS.md). The code itself doesn't know or care whether Postgres is native or containerized — only `make` does.

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
├── tests/            pytest
├── .github/          CI workflows
├── Makefile          single entry point for everything
├── Procfile          honcho process list (api, web, prefect)
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

See [`PRODUCTION_GAPS.md`](./PRODUCTION_GAPS.md) for the honest list of what we'd add for real production: HA, RBAC, secret management, distributed tracing, HIPAA controls, backups/DR, cost guardrails, **containerization for deployment**. These are intentional deferrals — the PRD documents the path.
