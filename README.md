# ABI Wound Care Eligibility Dashboard

A rule-based pipeline that decides which skilled-nursing-facility patients are
billable to **Medicare Part B** for wound care — by pulling their records from the
PointClickCare (PCC) mock API, extracting wound details from clinical notes and
assessments, and routing each patient to **auto-accept**, **flag for review**, or
**reject** with a plain-English reason.

> Product requirements: [PRD.md](PRD.md) · Full architecture notes: [PROJECT_GUIDE.md](PROJECT_GUIDE.md) · API contract: [API.md](API.md)

---

## Table of contents

1. [What it does](#what-it-does)
2. [Architecture at a glance](#architecture-at-a-glance)
3. [Quick start](#quick-start)
4. [Configuration](#configuration)
5. [The pipeline in detail](#the-pipeline-in-detail)
   - [Phase 1 — Sync (ingestion)](#phase-1--sync-ingestion)
   - [Phase 2 — Extract](#phase-2--extract)
   - [Phase 3 — Decide](#phase-3--decide)
6. [Rate limiting & resilience](#rate-limiting--resilience)
7. [The eligibility rules](#the-eligibility-rules)
8. [Data model](#data-model)
9. [HTTP API](#http-api)
10. [CLI reference](#cli-reference)
11. [Project layout](#project-layout)
12. [Scaling to millions](#scaling-to-millions)

---

## What it does

Billers at skilled nursing facilities (SNFs) waste hours manually checking whether
a wound-care claim is even billable to Medicare Part B. Three things have to be
true: the patient must have **active Part B coverage**, an **active wound
diagnosis**, and **complete wound documentation** (measurements + drainage). This
project automates that triage:

- **Ingests** every patient from the PCC API across all configured facilities,
  along with their diagnoses, coverage, notes, and assessments.
- **Extracts** structured wound fields (type, stage, location, length/width/depth,
  drainage) from both **structured assessments** and **free-text notes** — including
  messy "Envive-style" narrative notes.
- **Decides** each patient's billing routing with a deterministic rule engine and
  attaches a human-readable reason a biller can act on immediately.
- **Surfaces** it all in a live dashboard: per-facility counts, a filterable
  patient list, and a per-patient detail view showing exactly *why* the decision
  was made and what data was missing.

Every decision is **deterministic and auditable** — no field is ever invented, and
every "unknown" is recorded as an explicit flag rather than silently guessed.

---

## Architecture at a glance

```
┌─────────────┐   sync     ┌──────────────┐  extract   ┌──────────────┐  decide  ┌─────────────┐
│   PCC API   │ ─────────► │   SQLite     │ ─────────► │    wound     │ ───────► │ eligibility │
│ (rate-      │            │  raw tables  │            │ extractions  │          │  decisions  │
│  limited)   │            │ patients/dx/ │            │ (notes +     │          │ + flags     │
│             │            │ coverage/... │            │ assessments) │          │             │
└─────────────┘            └──────────────┘            └──────────────┘          └─────────────┘
                                                                                        │
                                                                                        ▼
                                                                              ┌───────────────────┐
                                                                              │  FastAPI + static │
                                                                              │     dashboard     │
                                                                              └───────────────────┘
```

The design principle is **fetch wide, decide narrow**: we pull *everyone* and store
them locally, then apply eligibility rules at decision time. A non-Part-B patient
is not discarded — it becomes a recorded `reject` with its actual payer listed, so
the biller knows where to route the claim instead.

### Built for scale from day one

The pipeline is designed to handle **millions of records**, so scalability is not
bolted on at the end — it shapes every layer. The decision logic is a **pure
function of one patient's data**, so it scales horizontally for free (run more of
it). The hard part — pulling data out of a heavily throttled API and persisting it
without losing or duplicating a single record — is where most of the engineering
went, and the mechanisms below are **already implemented**, not aspirational:

| Scale concern | How it's handled today | Where |
|---|---|---|
| Throttling (~30% 429s) | `Retry-After`-aware retry + concurrency cap + jitter | `ingestion/client.py` |
| Never mis-reject under load | "retries exhausted" ≠ "no data" → marked **partial**, retried | `pipeline.py` |
| Don't re-scan everything | Incremental `since` sync via per-facility **watermarks** | `pipeline.py` |
| Parallelism | `asyncio.gather` + two tunable semaphores | `ingestion/client.py`, `pipeline.py` |
| Fast, duplicate-free writes | **Batched commits** + idempotent `ON CONFLICT` upserts | `pipeline.py` |
| Dashboard at scale | Server-side **pagination**, **aggregated** counts, stats cache | `api/main.py`, `cache.py` |
| Query speed | **Indexes** on decision, facility, risk + composite index | `schema/schema.sql` |

See [Scaling to millions](#scaling-to-millions) for the full breakdown of what's
done and what's next.

---

## Quick start

```bash
# 1. Create and activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r backend/requirements.txt

# 3. (Optional) configure environment — see Configuration below
cp .env.example .env    # then edit values

# 4. Run the full pipeline: ingest → extract → decide
python backend/cli.py pipeline

# 5. Start the dashboard
./scripts/start.sh
#   or manually:
uvicorn backend.api.main:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000**.

> The dashboard also **auto-syncs on boot** — starting the server kicks off a
> pipeline run in the background and pushes live progress to the UI over
> Server-Sent Events. You don't strictly need step 4; it's there for a clean,
> one-shot populate from the CLI.

---

## Configuration

All configuration is via environment variables (loaded from `.env` if present).
Sensible defaults are baked in, so the app runs with no `.env` at all.

| Variable | Default | Purpose |
|---|---|---|
| `PCC_BASE_URL` | `https://hackathon.prod.pulsefoundry.ai` | Base URL of the PCC mock API |
| `DATABASE_URL` | `sqlite:///data/pulse.db` | Local database location |
| `FACILITY_IDS` | `101,102,103` | Which facilities to sync |
| `API_PAGE_SIZE` | `500` | Pagination size for list endpoints |
| `API_MAX_CONCURRENT` | `4` | Max simultaneous in-flight API requests |
| `API_MAX_429_RETRIES` | `60` | How many times to retry a single throttled request |
| `API_MAX_ERROR_RETRIES` | `8` | Retries for 5xx / transport errors |
| `API_MAX_RETRY_AFTER` | `15` | Clamp (seconds) on the server's `Retry-After` |
| `SYNC_CONCURRENCY` | `3` | Concurrent per-patient detail fetches |
| `SYNC_BATCH_SIZE` | `25` | Rows per DB commit batch |
| `SYNC_PATIENT_RETRIES` | `3` | Retry rounds for patients that fail detail fetch |
| `STATS_CACHE_SECONDS` | `15` | Dashboard stats cache TTL |

---

## The pipeline in detail

The pipeline is three sequential phases, orchestrated in `backend/pipeline.py`. You
can run them individually (`sync`, `extract`, `decide`) or all at once
(`pipeline`).

### Phase 1 — Sync (ingestion)

`run_sync()` pulls everything from the PCC API into local SQLite tables.

For each facility:

1. **List patients** (`GET /pcc/patients?facility_id=...`), paginating with
   `limit`/`offset` until a short page is returned. Each patient is **upserted**
   (`INSERT ... ON CONFLICT DO UPDATE`) so re-runs never create duplicates.
2. **Fetch per-patient detail** — for every patient, four endpoints are fetched
   **concurrently** (`asyncio.gather`): diagnoses, coverage, notes, assessments.
   These are stored in their respective tables, also via upsert.
3. **Track progress & watermarks** — each endpoint's outcome is logged to
   `facility_sync_status`, and a per-facility `sync_watermark` is written on
   success so the *next* run can fetch only what changed (`since` parameter).

Patient detail fetches that fail (after the API retry layer gives up) are collected
and **retried in additional rounds** (`SYNC_PATIENT_RETRIES`). Crucially, a patient
whose detail fetch never succeeds is marked **partial** — it is **not** treated as
"this patient has no data." Correctness over speed: we never mis-reject a real
patient because the API throttled us.

Interrupted runs are self-healing: on the next boot, `mark_stale_sync_runs()` closes
any `sync_run` left in `running` state by a crash or hot reload.

### Phase 2 — Extract

`run_extract()` turns raw clinical text into structured wound records
(`backend/extraction/parser.py`). It processes:

- **Current notes** → `extract_from_text()`, a regex-based parser that pulls:
  - **Measurements** — both `4.2 x 3.1 x 1.5 cm` shorthand and
    `2.9 cm x 2.8 cm` labeled forms.
  - **Wound type** — by keyword (pressure ulcer, diabetic foot ulcer, venous
    stasis, arterial, surgical site infection, abscess, burn).
  - **Stage**, **location**, and **drainage** (normalized to none/light/moderate/heavy).
  - **Note format detection** — `soap`, `prose`, `multi_wound`, or `envive`
    (narrative-only notes that hide wound details in prose).
- **Current assessments** → `extract_from_assessment_json()`, mapping structured
  JSON fields directly.

The key idea is **explicit unknowns**. Every field carries a *status*:
`known`, `unknown_missing`, or `unknown_unparseable`. The parser never guesses —
if a field isn't there, it says so, and that status flows into the risk scoring
downstream. Envive-style narrative notes are additionally flagged
(`envive_narrative_only`) so a biller knows the wound detail lives only in free text.

### Phase 3 — Decide

`run_decide()` joins each patient's diagnoses, coverage, wound extractions, and
flags, then calls the rule engine (`backend/eligibility/rules.py`). Before deciding,
it derives two extra signals:

- **Note-vs-assessment conflict** — if a note and an assessment disagree on stage,
  a high-severity flag is raised.
- **Multiple active payers** — more than one coverage record raises a medium flag.

The result is written to the `eligibility` table (one row per patient, replaced on
each run), carrying the decision, reason, Part B status, other payers, the primary
wound's fields, the unknown-risk score/tier, and all the boolean signals.

---

## Rate limiting & resilience

The PCC API rejects roughly **30% of requests** with **HTTP 429** and includes a
**`Retry-After`** header. All of the handling lives in
`backend/ingestion/client.py`, and it is built in four layers:

1. **Cap concurrency (prevention).** A semaphore (`API_MAX_CONCURRENT`, default 4)
   limits how many requests are in flight at once. Fewer simultaneous requests means
   fewer 429s triggered in the first place.

2. **Detect the 429.** A throttled response isn't returned as data — it's raised as
   a `RateLimitError` carrying the server's `Retry-After` value, so the retry layer
   knows exactly how long to wait.

3. **Retry, honoring `Retry-After`.** `fetch_json()` waits the **exact** time the
   server asked for (clamped to `API_MAX_RETRY_AFTER` so a bad header can't stall
   forever), plus a small random **jitter** so concurrent workers don't all wake and
   re-fire at the same instant. It retries up to `API_MAX_429_RETRIES` (default 60)
   before giving up on a single request.

4. **Two different backoffs for two different problems.** A **429** means "slow
   down" → wait the flat `Retry-After`. A **5xx / transport error** means "something
   broke" → **exponential backoff** (`min(2**attempt, 30)`, capped, max 8 tries).
   Keeping these separate means we never confuse "the API is busy" with "this data
   doesn't exist."

Live counters (`requests`, `rate_limited`, `retries`, `errors`) are tracked
throughout and surfaced on `GET /api/health`. In a typical run, hundreds of requests
are throttled and **zero data is lost** — the system paces itself to work *with* the
limit rather than fighting it.

---

## The eligibility rules

The rule engine (`backend/eligibility/rules.py`) evaluates each patient **in order**
and short-circuits on the first match:

| # | Condition | Decision | Reason |
|---|---|---|---|
| 1 | No active Medicare Part B | **reject** | "No active Medicare Part B coverage." |
| 2 | No active wound diagnosis *and* no documented wound | **reject** | "No active wound diagnosis or documented wound…" |
| 3 | Wound present but measurements / depth / drainage missing | **flag_for_review** | "…missing X — clinician should verify." |
| 4 | Note↔assessment conflict, multiple wounds, Envive-only, or high risk | **flag_for_review** | lists the specific reasons |
| 5 | Active Part B + active wound + complete critical fields | **auto_accept** | full clinical summary |
| — | Otherwise | **flag_for_review** | "…some fields incomplete or ambiguous." |

Supporting logic:

- **`has_active_medicare_b()`** — a coverage row counts only if `payer_code == "MCB"`
  **and** it's currently active (`effective_to` is null or in the future). Non-MCB
  payers are collected into `other_active_payers` so a rejected patient still shows
  where their claim *should* go.
- **`is_wound_icd()`** — matches a curated set of ICD-10 prefixes (pressure ulcers
  `L89.`, diabetic foot `E11.621/E10.621`, venous `I83.0`, arterial `I70.`,
  post-procedure `T81.x`, abscess `L02.`, burns `T20`–`T32`).
- **`pick_primary_wound()`** — when several wounds exist, ranks by stage severity
  then field completeness; ties flag `multiple_eligible_wounds`.
- **`compute_unknown_risk()`** — turns missing/unparseable/conflicting fields and
  flags into a 0–100 score and a green/yellow/red tier, so the highest-risk records
  rise to the top of a biller's queue.

**Why MCB isn't filtered at the API level:** the patient-list endpoint doesn't
expose coverage (it's a separate per-patient call), so it's not even possible to
filter there. More importantly, in a billing tool a non-Part-B patient is a
meaningful, auditable *result* — not noise to silently drop.

---

## Data model

SQLite, defined in [`schema/schema.sql`](schema/schema.sql). Core tables:

| Table | Holds |
|---|---|
| `patients` | One row per patient; raw payload + facility, payer, admission flags |
| `diagnoses` | ICD-10 codes, descriptions, clinical status |
| `coverage` | Payer code/type/name, effective dates |
| `notes` | Free-text clinical notes + detected format |
| `assessments` | Structured assessment JSON |
| `wound_extractions` | Parsed wound fields with per-field `*_status` |
| `unknown_flags` | Explicit data-quality flags (Envive-only, conflicts, multi-payer) |
| `eligibility` | Final decision per patient + reason + risk score |
| `sync_runs` / `facility_sync_status` / `sync_watermarks` | Sync bookkeeping & incremental watermarks |

Reruns are idempotent: raw tables upsert on natural keys; `wound_extractions` and
`eligibility` are rebuilt each run.

---

## HTTP API

Served by `backend/api/main.py` (FastAPI). The static dashboard is mounted at `/`.

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Status, patient count, pipeline state, live API stats |
| `GET /api/stats` | Per-facility and per-decision overview counts |
| `GET /api/dashboard` | Combined stats + paginated patient list (one call for the UI) |
| `GET /api/patients` | Filterable patient list (by facility, decision, risk tier) |
| `GET /api/patients/{patient_id}` | Full detail: wound sources, diagnoses, coverage, unknowns |
| `GET /api/sync/status` | Current pipeline phase / progress |
| `POST /api/sync` | Trigger a sync (incremental by default) |
| `GET /api/events` | Server-Sent Events stream — pushes when data changes |
| `POST /api/chat` | Patient-reasoning Q&A for billers |

---

## CLI reference

```bash
python backend/cli.py <command>
```

| Command | Description |
|---|---|
| `init-db` | Create the SQLite schema |
| `sync` | Fetch all patients + detail from the PCC API (supports `--full`) |
| `extract` | Parse wounds from notes & assessments |
| `decide` | Run the eligibility rules |
| `pipeline` | Run `sync` → `extract` → `decide` end to end |
| `export-features` | Export a feature CSV (for optional model training) |
| `apply-model` | Apply a trained decision tree to `model_insights` |

---

## Project layout

```
.
├── backend/
│   ├── api/main.py          # FastAPI app, endpoints, SSE, dashboard mount
│   ├── cli.py               # Command-line entry point
│   ├── pipeline.py          # sync / extract / decide orchestration
│   ├── ingestion/client.py  # PCC API client: rate-limit retry, pagination
│   ├── extraction/parser.py # Wound extraction from notes + assessments
│   ├── eligibility/rules.py # Deterministic routing rules
│   ├── cache.py             # Stats cache + data-version tracking
│   ├── jobs.py              # Background auto-sync + progress state
│   └── db/                  # SQLAlchemy session + init
├── frontend/                # Static dashboard (served at /)
├── schema/schema.sql        # SQLite schema
├── scripts/start.sh         # One-command launch helper
├── ml/                      # Optional decision-tree training assets
├── data/pulse.db            # Local SQLite database (created at runtime)
├── API.md                   # PCC API contract + retry requirements
├── PRD.md                   # Product requirements
└── PROJECT_GUIDE.md         # Architecture deep-dive
```

---

## Scaling to millions

The pipeline is built to handle millions of records from the start. The decision
logic is a **pure function of one patient's data**, so
it scales horizontally for free — you process millions by running more invocations,
never by rewriting a rule. All the hard engineering is in ingestion and persistence,
and most of it is **already done.**

### Already implemented

These are live in the current codebase, not future plans:

1. **Correctness under throttling — "retries exhausted" ≠ "no data."**
   When a patient's detail fetch can't complete after all retries, the sync is
   marked **partial** and that patient is retried on the next run. It is *never*
   recorded as a `reject`. At millions of requests, this is the single most
   important property: a naive pipeline would silently mis-reject thousands of real,
   billable patients. *(`pipeline.py` — `SYNC_PATIENT_RETRIES`, partial marker.)*

2. **`Retry-After`-aware rate limiting with a concurrency cap and jitter.**
   We obey the server's backoff, cap in-flight requests, and jitter retries so
   parallel workers don't stampede — surviving ~30% throttling with zero data loss.
   *(`ingestion/client.py`.)*

3. **Incremental `since` sync via per-facility watermarks.**
   After the first full load, each facility records a `last_success_at` watermark, so
   subsequent runs fetch only what changed. "Re-scan everything" becomes "process the
   delta." *(`pipeline.py` — `sync_watermarks`, `get_incremental_since()`.)*

4. **Concurrency throughout.** Each patient's four detail endpoints are fetched in
   parallel (`asyncio.gather`), governed by tunable semaphores
   (`API_MAX_CONCURRENT`, `SYNC_CONCURRENCY`). *(`ingestion/client.py`, `pipeline.py`.)*

5. **Batched commits + idempotent upserts.** Writes are flushed in batches
   (`SYNC_BATCH_SIZE`) and every table uses `INSERT ... ON CONFLICT DO UPDATE`, so
   reruns stay fast *and* duplicate-free. *(`pipeline.py`.)*

6. **A dashboard that doesn't melt at scale.** The API serves **server-side
   paginated** patient lists (`LIMIT`/`OFFSET`) and **pre-aggregated** counts, behind
   a short-TTL **stats cache** with a data-version bump that drives SSE so the UI
   only refetches when data actually changed. *(`api/main.py`, `cache.py`.)*

7. **Indexes on every hot path.** Including a **composite index** on
   `(facility_id, routing_decision)` plus indexes on risk score/tier and patient
   foreign keys — so filtered dashboard queries stay fast as rows grow.
   *(`schema/schema.sql`.)*

### What's next (the path beyond a single process)

To go from "hundreds of thousands comfortably" to "tens of millions in a tight
window," the remaining work is horizontal:

- **Token-bucket pacing + a wider worker pool / queue.** Pace requests just *under*
  the API's limit so 429s become rare by design, then fan out across many workers
  (or processes). This is the change that turns "feasible" into "fast."
- **Move from SQLite to Postgres** and swap batched `executemany` for `COPY`
  (500–1000 rows per flush), keeping the same upsert semantics.
- **Distribute the work** — a queue of patient IDs consumed by a pool of stateless
  workers, since the decision logic already has no shared state.

> Bottom line: the brain of the system is scale-proof, and the ingestion/persistence
> layer already has the correctness, incrementality, batching, and indexing that
> most pipelines only add after they fall over. The remaining work is pure
> horizontal fan-out — and the property we guard hardest is that **we never throw
> away a real patient just because the API throttled us.**
```
