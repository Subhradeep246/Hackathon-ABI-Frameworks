# PRD — ABI Wound IQ

**Hackathon:** ABI Frameworks — Wound Care Billing Pipeline
**Team size:** 3–4 engineers
**Time budget:** Weekend (~20+ hours)
**Design intent:** Production-ready patterns that scale unchanged from 300 → 30M rows. Not production infrastructure (no HA, no K8s) — production *code*.

---

## 1. Context

ABI Frameworks runs post-acute care facilities. Billers manually review wound care patients and decide whether they qualify for Medicare Part B reimbursement. The process is slow, error-prone, and bottlenecked on a human reading free-text clinical notes.

We're building an automated pipeline that:
1. Pulls patient data from a mock PointClickCare (PCC) API
2. Extracts wound details from messy clinical notes (both structured SOAP and unstructured Envive narratives)
3. Decides whether each patient is **billable**, **needs review**, or **should be rejected**
4. Presents the result to a non-technical biller through a polished dashboard
5. **Demonstrates the pipeline scales** to 1M+ patients via a synthetic-data benchmark

**Why this matters for judging:** the rubric rewards pipeline robustness, extraction accuracy, clean schema, and presentation. Our edge will come from (a) extraction quality on Envive narratives, (b) a demo that feels like a real product, and (c) a benchmark proving the architecture scales to ABI's real future load.

---

## 2. Constraints & Hard Requirements

From `README.md` / `API.md`:

- **300 patients** across 3 facilities (101 / 102 / 103) in the live mock API
- **API base:** `https://hackathon.prod.pulsefoundry.ai`
- **30% of requests return HTTP 429** — must retry with `Retry-After` header honored
- **Two patient identifiers:** string `patient_id` (FA-001) for `/diagnoses` and `/coverage`; integer `id` for `/notes` and `/assessments`
- **5 endpoints:** `/pcc/patients`, `/pcc/diagnoses`, `/pcc/coverage`, `/pcc/notes`, `/pcc/assessments`
- **Eligibility rules (Medicare Part B billable):**
  1. Active wound (pressure ulcer, DFU, venous, arterial, surgical, abscess, burn)
  2. Active Medicare Part B coverage (payer_code `MCB`, `effective_to` null)
  3. Documented L/W/D measurements + drainage level
- Routing decisions: `auto_accept` | `flag_for_review` | `reject`

**Self-imposed scale target:** pipeline must ingest, extract, and decide on **1M synthetic patients in under 15 minutes** on a single laptop. This is the production-readiness benchmark.

### 2a. API-derived design notes (verified against API.md)

These are details we cross-checked from the actual API spec; they shape the rules and extraction:

- **Assessment status:** API returns `status='Complete'` or `'In-Progress'`. Only `Complete` assessments are trusted by the rules engine — in-progress ones are unfinished and ignored for billing.
- **Recency tiebreaker:** when a patient has multiple `is_current=true` records, the rules engine uses the one with the most recent `assessment_date` (assessments) or `effective_date` (notes).
- **Drainage has two sub-fields:** `drainage_type` (e.g., serosanguineous) and `drainage_amount` (none/light/moderate/heavy). Eligibility only depends on `drainage_amount`; type is stored for audit.
- **`org_id` is partial in the API:** notes and assessments include `org_id` (e.g., `"ORG-101"`); patients/diagnoses/coverage do not. We derive `org_id` from `facility_id` (facility 101 → ORG-101) for the missing tables to keep the multi-tenant key consistent.
- **`assessment.raw_json` is a JSON-encoded string**, not a parsed object. We must `json.loads()` it before reading fields.
- **Envive `note_type` value is not specified in API.md** — the docs say "Envive narrative format" without giving the literal string. We'll discover it from live data in Phase 1 and gate the LLM tier on `note_type NOT IN ('Wound (SPN)', 'HP Skin & Wound Note')`.
- **Doc inconsistency on patient counts:** README says 120+90+90, API.md says 100+100+100. Our pipeline handles N dynamically — no code change needed, but the team should expect either.
- **Nullable patient fields:** `first_name`, `last_name`, `birth_date`, `gender`, `primary_payer_code` can all be null. Dashboard renders "(unknown)" rather than failing.
- **Wound diagnosis without measurements is NOT a reject:** if the diagnosis table proves a patient has an active wound but the notes/assessments don't have usable measurements, the decision is `flag_for_review` (clinician should chase the missing documentation), not `reject`.

---

## 3. Goals & Non-Goals

**Goals**
- Pipeline that survives 429s without manual restart and is fully idempotent
- Accurate extraction across all 4 note formats (SOAP, prose, multi-wound, Envive)
- One row per patient with extracted fields + decision + plain-English reason
- Dashboard a non-technical biller can use without training
- A demo with a clear "wow" moment that differentiates us
- **Benchmarked scale**: same code handles 1M+ rows

**Non-Goals (explicit deferrals)**
- Real PHI handling / HIPAA compliance (data is synthetic)
- High availability, multi-region, auto-scaling infra
- Real auth / RBAC (use a stub `org_id`)
- Kubernetes — Docker Compose is enough for the demo
- Distributed tracing (OpenTelemetry) — structured logs + Prometheus metrics only
- Real secret management (Vault / AWS Secrets Manager) — `.env` with `.env.example` committed

Each non-goal has a documented "what we'd add for prod" note in the README. That's how we honor "production-ready" without doing all of production.

---

## 4. Tech Stack

### Storage
- **PostgreSQL 16** — primary OLTP store, runs in Docker Compose locally, drop-in RDS / Cloud SQL in prod
- **Redis 7** — LLM output cache (keyed by content hash) + API response cache
- **Alembic** — versioned schema migrations
- **pgbouncer** — connection pooling (config committed; only spun up at higher worker counts)

### Pipeline orchestration
- **Prefect 3** — Python-native flows, retries, scheduling, run UI. Scales from local process → Prefect Cloud → self-hosted workers without code change.
  - *Alternative considered:* plain `asyncio` + APScheduler. Lighter but loses the run UI and retry policy framework — both of which matter for "production-ready" feel.

### Backend (Python 3.11+)
- `httpx` — async HTTP client for the PCC API
- `tenacity` — retry decorator honoring `Retry-After` (wrapped inside Prefect tasks for double-retry: Prefect at task-level, tenacity at HTTP-level)
- `pydantic` v2 — typed models for API responses + LLM structured output
- `sqlalchemy` 2.0 + `asyncpg` — typed ORM, async Postgres
- `fastapi` + `uvicorn` — REST API
- `structlog` — structured JSON logs
- `prometheus-client` — `/metrics` endpoint scraped locally by Grafana
- `uv` — env / dep manager

### Extraction
- Python `re` — Tier-1 regex for SOAP / labeled prose
- **Microsoft Presidio** — PHI redaction before any LLM call. Open-source, runs locally, strips names / DOBs / MRNs / addresses / phone numbers from note text. The LLM only ever sees clinical content, never identifiers.
- **Baseten** HTTP client — Tier-2 LLM for Envive narratives + ambiguity (called on redacted text only)
- Small Llama 3.1 8B for classification; larger model (Llama 70B / Mistral Large) for narrative extraction
- **Content-hash cache in Postgres** — same note text → same extraction result, ever. Demo is offline-resilient; LLM cost grows sub-linearly with patient count.

**PHI handling guarantee:** raw note text never leaves our infrastructure unredacted. The patient_id is held in our process, only redacted clinical text crosses the network to Baseten, and the LLM response is re-joined to the patient locally. This is the foundation of the HIPAA-compliant path even though our hackathon data is synthetic.

### Frontend (Next.js 14, App Router)
- TypeScript
- Tailwind CSS + shadcn/ui
- **TanStack Table with row virtualization** — required for the 1M-row view
- **Server-side pagination + filtering** on `/api/patients` (cursor-based, indexed)
- Recharts — healing trajectory (Phase 4)
- TanStack Query — data fetching + cache

### WOW layer
- 11Labs API — TTS for voice copilot
- Web Audio API — playback

### Quality & DX
- `pytest` + `pytest-asyncio` — unit tests for extraction + rules + ingest contract
- `ruff` + `black` — Python lint/format
- `mypy --strict` on `extraction/` and `eligibility/` (highest blast radius)
- **Docker Compose** — Postgres + Redis + Prefect server + API + worker + web in one command
- `pnpm` — Node deps
- `Makefile` — `make bootstrap`, `make sync`, `make scale-test`, `make demo`

### Observability (local-only — flagged as gap)
- structlog → console + JSON file
- Prometheus client on the API + worker → local Grafana board
- README documents the prod path: ship logs to Loki, metrics to Grafana Cloud, traces to Tempo, alerts via PagerDuty.

### Why Postgres (not DuckDB)
DuckDB is excellent for analytics-only workloads, but our path is OLTP-style ingest at write-time, transactional updates per sync, eligibility queries with filters per facility/decision, and eventually concurrent multi-tenant access. Postgres is the right primitive for all of that and scales unchanged. We'd add a DuckDB / Snowflake analytics warehouse downstream of Postgres only when BI / reporting becomes a thing — not now.

### Why Prefect (not Airflow / Dagster)
- Airflow: too much YAML/DAG ceremony for a weekend, but the most mature for true prod.
- Dagster: asset-based paradigm is great long-term but slows day-1 productivity.
- Prefect 3: decorators, async-native, fast to learn, good observability, free OSS, clean scale path. Best fit for our team velocity + production aspirations.

---

## 5. Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Mock PCC API (https://hackathon.prod.pulsefoundry.ai)              │
│  + Synthetic Data Generator (local, copies API distributions)       │
└─────────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Prefect Flows (orchestration)                                      │
│   - sync_patients_flow (per facility, parallel)                     │
│   - extract_wounds_flow (per patient, parallel, cache-aware)        │
│   - decide_eligibility_flow (bulk, idempotent)                      │
│   - scheduling: every N min in prod, on-demand in demo              │
└─────────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Ingestion Tasks                                                    │
│   - httpx async client + tenacity retry (Retry-After honored)       │
│   - Prefect retries on top of tenacity                              │
│   - Bulk INSERT ... ON CONFLICT DO UPDATE (idempotent upserts)      │
│   - Incremental sync via `since` watermarks per endpoint            │
│   - structlog every retry / 429 / batch                             │
└─────────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PostgreSQL 16                                                      │
│   - raw_*                (mirror of API, idempotent upserts)        │
│   - extraction_cache     (content_hash → extracted_json)            │
│   - extracted_wounds     (latest extraction per patient)            │
│   - eligibility_decisions (current decision + audit trail)          │
│   - sync_watermarks      (per-endpoint last-success timestamp)      │
│   Indexes on (org_id, facility_id, decision, last_modified_at)      │
└─────────────────────────────────────────────────────────────────────┘
                │                          ▲
                ▼                          │
┌────────────────────────────────┐   ┌────────────────────────────┐
│  Extraction Workers            │   │  Redis                     │
│   1. regex (SOAP / prose)      │◀──│   - LLM output cache       │
│   2. PHI redaction (Presidio)  │   │   - API response cache     │
│   3. Baseten LLM (Envive only, │   │   - rate-limit token bucket│
│      on redacted text only)    │   └────────────────────────────┘
│   4. cross-validate, score     │
│   5. cache by sha256(note_text)│
└────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Eligibility Engine (deterministic rules + reason templates)        │
└─────────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  FastAPI service                                                    │
│   - GET /api/patients?facility&decision&payer&cursor&limit          │
│   - GET /api/patients/{id}                                          │
│   - POST /api/chat   (LLM Q&A with tool-use over Postgres)          │
│   - POST /api/voice  (11Labs TTS)                                   │
│   - GET /metrics     (Prometheus)                                   │
│   - GET /healthz, /readyz                                           │
└─────────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Next.js Dashboard                                                  │
│   - Virtualized patient queue, server-paginated                     │
│   - Detail drawer: fields + source highlights + audit trail         │
│   - Voice copilot button                                            │
└─────────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Local observability: structlog + Prometheus → Grafana              │
│  Prod path (documented): Loki + Grafana Cloud + Tempo + PagerDuty   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 6. Data Model

Every table carries `org_id` from day one. Indexes are tuned for the queries the dashboard and pipeline run most.

```sql
-- Raw cache (mirror of API, idempotent upserts)
CREATE TABLE raw_patients (
  id INT NOT NULL,
  org_id TEXT NOT NULL,
  patient_id TEXT NOT NULL,
  facility_id INT NOT NULL,
  first_name TEXT, last_name TEXT, birth_date DATE, gender TEXT,
  primary_payer_code TEXT,
  last_modified_at TIMESTAMPTZ,
  is_new_admission BOOLEAN,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, patient_id)
);
CREATE INDEX ON raw_patients (org_id, facility_id);
CREATE INDEX ON raw_patients (org_id, last_modified_at);
-- (raw_diagnoses, raw_coverage, raw_notes, raw_assessments mirror the API similarly)

-- Sync watermarks for incremental fetching
CREATE TABLE sync_watermarks (
  org_id TEXT NOT NULL,
  endpoint TEXT NOT NULL,            -- 'patients' | 'notes' | 'assessments'
  facility_id INT,                   -- nullable when not facility-scoped
  last_success_at TIMESTAMPTZ,
  last_run_at TIMESTAMPTZ,
  PRIMARY KEY (org_id, endpoint, facility_id)
);

-- LLM cache: same input → same output, forever
CREATE TABLE extraction_cache (
  content_hash TEXT PRIMARY KEY,     -- sha256(note_text)
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  extracted_json JSONB NOT NULL,
  confidence FLOAT,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Derived: one row per patient with our best extraction
CREATE TABLE extracted_wounds (
  org_id TEXT NOT NULL,
  patient_id TEXT NOT NULL,
  internal_id INT NOT NULL,
  wound_type TEXT,
  wound_stage TEXT,
  location TEXT,
  length_cm FLOAT, width_cm FLOAT, depth_cm FLOAT,
  drainage_amount TEXT,              -- none | light | moderate | heavy (drives eligibility)
  drainage_type TEXT,                -- serosanguineous | purulent | serous | etc. (captured for audit, not used in rule)
  confidence_wound_type FLOAT,
  confidence_measurements FLOAT,
  confidence_drainage FLOAT,
  overall_confidence FLOAT,
  source_table TEXT,                 -- 'note' | 'assessment' | 'both'
  source_record_id INT,
  extraction_method TEXT,            -- 'regex' | 'llm' | 'agreed' | 'llm_after_regex_failed'
  extraction_version INT NOT NULL,   -- bump when prompts change → forces re-extract
  extracted_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (org_id, patient_id)
);

-- Final output table the biller cares about
CREATE TABLE eligibility_decisions (
  org_id TEXT NOT NULL,
  patient_id TEXT NOT NULL,
  decision TEXT NOT NULL,            -- auto_accept | flag_for_review | reject
  has_active_wound BOOLEAN,
  has_active_mcb BOOLEAN,
  has_measurements BOOLEAN,
  has_drainage BOOLEAN,
  reason TEXT NOT NULL,
  audit_json JSONB NOT NULL,
  decided_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (org_id, patient_id)
);
CREATE INDEX ON eligibility_decisions (org_id, decision);
CREATE INDEX ON eligibility_decisions (org_id, decided_at DESC);
```

**Migration strategy:** Alembic. Every schema change is a versioned, reversible migration. `make migrate` runs them in CI and locally.

---

## 7. Phased Plan

Phase 1–3 are **required for submission**. Phase 4 is **WOW**. Phase 4.5 is **scale benchmark**. Phase 5 is **demo polish**.

### Phase 0 — Infra Foundation (3–4 hr, whole team)
**Deliverable:** `docker compose up` spins up Postgres + Redis + Prefect server + FastAPI + Next.js + Grafana. `make migrate` creates all tables.

- [ ] Repo layout: `pipeline/`, `extraction/`, `eligibility/`, `api/`, `web/`, `synthetic/`, `infra/`, `tests/`
- [ ] `docker-compose.yml` with all services + healthchecks
- [ ] `Makefile` targets: `bootstrap`, `migrate`, `sync`, `extract`, `decide`, `scale-test`, `demo`, `test`
- [ ] Alembic initialized, baseline migration with the §6 schema
- [ ] `.env.example` committed; secrets gitignored
- [ ] Prefect server running, smoke test flow ("hello world") visible in UI
- [ ] FastAPI `/healthz` + `/readyz` + `/metrics` endpoints
- [ ] Next.js scaffolded with one page calling `/api/healthz`
- [ ] GitHub Actions: lint + test on PR

**Verify:** new teammate clones the repo and runs `make bootstrap && make demo` — everything comes up clean.

### Phase 1 — Ingestion (4–5 hr, owner: Person A)
**Deliverable:** `make sync` runs Prefect flow that populates raw_* tables idempotently. Re-running yields zero new rows. Incremental sync via `since` works.

- [ ] httpx async client with semaphore (cap 5 concurrent)
- [ ] `tenacity` retry with `Retry-After` honoring + exponential backoff
- [ ] Bulk `INSERT ... ON CONFLICT DO UPDATE` for all raw_* tables
- [ ] `sync_watermarks` updated atomically per flow run
- [ ] `--since` flag drives incremental mode
- [ ] structlog event per fetch (status, retries, duration_ms, bytes)
- [ ] Prometheus counters: `pcc_requests_total{endpoint,status}`, `pcc_retries_total{endpoint}`
- [ ] Contract test against mock API (real network, marked slow)
- [ ] Unit test with 429 fixture proving retry honors `Retry-After`

**Verify:** `make sync` populates all 300 patients + related records, completes in <2 min, second run is a no-op.

### Phase 2 — Extraction Engine (4–5 hr, owner: Person B)
**Deliverable:** `make extract` populates `extracted_wounds` for all patients with confidence scores. Cached LLM calls.

- [ ] **Source selection per patient** — filter assessments to `status='Complete'`; when multiple `is_current=true` records exist, pick the latest by `assessment_date` / `effective_date`
- [ ] Parse `assessment.raw_json` directly via `json.loads()` (highest trust, deterministic). Capture both `drainage_type` and `drainage_amount`.
- [ ] Regex extractors for SOAP-style and prose notes
- [ ] **PHI redaction with Microsoft Presidio** — runs before any LLM call. Strips PERSON, DATE_TIME, US_SSN, PHONE_NUMBER, EMAIL_ADDRESS, LOCATION, MEDICAL_LICENSE entities. Outputs redacted text + a local-only mapping table for re-attachment.
- [ ] Baseten LLM extractor for Envive narratives (structured JSON via pydantic schema, redacted text only)
- [ ] **Content-hash cache check** before every LLM call: `sha256(redacted_note_text) + model + prompt_version`
- [ ] Multi-wound: LLM picks primary by size/severity
- [ ] Cross-validation: assessment vs note → agreement→1, disagreement→0.5 + audit entry
- [ ] Wound type classifier (regex + ICD-10 lookup against `raw_diagnoses`)
- [ ] `prompt_version` field bumps force re-extraction on next run
- [ ] Unit tests with fixture notes covering all 4 formats
- [ ] **PHI redaction test**: synthetic note containing fake name/DOB/MRN; assert all three are absent from the string sent to the LLM client

**Verify:** spot-check 10 random patients across formats. Re-run extraction shows >95% cache hit rate.

### Phase 3 — Eligibility + Dashboard MVP (5–6 hr, owners: Person B for rules, Person C for UI)
**Deliverable:** Next.js dashboard backed by FastAPI, server-paginated, with decision routing for all patients.

**Rules engine (`eligibility/rules.py`):**
- [ ] **ICD-10 wound code constants** (`eligibility/wound_codes.py`):
  - `L89.*` — pressure ulcers (all stages + unstageable)
  - `E11.621`, `E10.621` — diabetes with foot ulcer (DFU)
  - `I83.0*` — venous ulcers
  - `I70.*` with ulceration — arterial ulcers
  - `T81.4*`, `T81.3*` — surgical site infection / dehiscence
  - `L02.*` — abscess
  - `T20–T32` — burns
- [ ] **Source selection** before evaluating rules:
  - Only use assessments where `status='Complete'`
  - When multiple `is_current=true` records exist, pick the one with the most recent `assessment_date` (assessments) or `effective_date` (notes)
- [ ] `has_active_wound`: any `clinical_status='active'` diagnosis whose `icd10_code` matches the wound list above, OR an extracted wound from a note
- [ ] `has_active_mcb`: any coverage row with `payer_code='MCB'` and `effective_to IS NULL`
- [ ] `has_measurements`: L, W, D all non-null
- [ ] `has_drainage`: `drainage_amount IN ('none','light','moderate','heavy')`
- [ ] Decision (in order):
  - No active MCB → **`reject`** ("No active Medicare Part B coverage")
  - No active wound diagnosis AND no extracted wound from notes → **`reject`** ("No active wound on record")
  - Has wound + MCB but missing measurements or drainage → **`flag_for_review`** ("Wound documented but measurements/drainage incomplete")
  - Has wound + MCB + all fields but `overall_confidence < 0.8` → **`flag_for_review`** ("Low extraction confidence — clinician should verify")
  - All checks pass → **`auto_accept`**
- [ ] Reason templates compose plain English
- [ ] Audit JSON includes: source notes, extracted values, confidence, rule trace
- [ ] Property-based tests (hypothesis) — invariants like "auto_accept implies has_active_mcb"

**FastAPI:**
- [ ] `GET /api/patients?org_id&facility&decision&payer&cursor&limit` — cursor-paginated, indexed
- [ ] `GET /api/patients/{id}` — extraction + audit trail
- [ ] `GET /api/stats` — counts by decision per facility
- [ ] Redis cache on `/api/stats` with short TTL

**Next.js:**
- [ ] Stat tiles: total, auto_accept, flagged, rejected, estimated revenue
- [ ] Virtualized patient table (TanStack Table + virtual)
- [ ] Server-side filtering (facility, decision, payer)
- [ ] Detail drawer with audit trail
- [ ] Color-coded decision badges + reason box

**Verify:** dashboard loads, biller answers "how many patients should I bill today?" in <5 seconds. Filter to Facility A returns ~120 rows even when DB has 1M+.

### Phase 4 — WOW Factor (4–5 hr, owner: Person D)
**Recommended: voice copilot only** (drop from 2 → 1 WOW given infra cost). Stretch goal: explainability inspector.

**🎙️ Voice-Driven Biller Copilot (11Labs + Baseten)**
- [ ] Mic button on dashboard; browser captures audio
- [ ] Whisper (or Baseten ASR) transcribes
- [ ] Baseten LLM with tool-use over Postgres: `query_patients(filters)`, `summarize_patient(id)`
- [ ] 11Labs TTS streams the answer back
- [ ] Pre-cache TTS for the 5 hero patients (offline-resilient)

**🔍 Stretch: Explainability Inspector**
- [ ] Click any extracted field → side panel
- [ ] Show source text with span highlight
- [ ] Show extraction method + confidence + reason

### Phase 4.5 — Scale Benchmark (3–4 hr, owner: Person A)
**This is the differentiator.** Without it, "production-ready" is unproven.

- [ ] `synthetic/generator.py` — produces N synthetic patients matching API distributions (facility split 4:3:3, payer mix 60/15/10/15, note format mix, wound type distribution)
- [ ] Bulk inserts via `COPY` for max throughput
- [ ] `make scale-test N=1000000` populates DB, runs extraction (regex tier only — LLM tier sampled), runs decision engine
- [ ] Capture & display benchmark dashboard in Grafana:
  - Ingest throughput (rows/sec)
  - Extraction throughput (patients/sec)
  - p50/p95/p99 query latency on `/api/patients`
  - Memory + CPU on each container
- [ ] Document results in `BENCHMARK.md`

**Demo target:** 1M synthetic patients ingested + extracted + decided in <15 min on a single laptop. Dashboard query latency stays <500ms.

### Phase 5 — Demo Polish (2–3 hr, whole team)
- [ ] Pick 5 hero patients: clean auto_accept, ambiguous flag, hard Envive, no-MCB reject, multi-wound
- [ ] 10-minute presentation script:
  1. Problem + architecture (1.5 min)
  2. Live dashboard tour with hero patients (3 min)
  3. Voice copilot demo (2 min)
  4. **Scale benchmark walkthrough** (2 min) — the production-ready proof
  5. Q&A (1.5 min)
- [ ] Smoke test end-to-end the morning of
- [ ] Backup: prerecorded GIFs + a `make demo-offline` mode that uses cached data only
- [ ] README with one-command bootstrap + "what we'd add for prod" section

---

## 8. WOW Factor Menu

Given the infra spend, ship **A only** for the demo. Keep B–I as stretch / post-hackathon.

### 🎙️ A. Voice-Driven Biller Copilot (11Labs + Baseten) — **SHIPPING**
See Phase 4. The demo moment.

### 🔍 B. Explainability Inspector — **STRETCH**
Click extracted field → source highlight + method + confidence.

### 📊 C. Healing Trajectory Timeline
Wound size over time + trend badges.

### 💰 D. Live Revenue Forecast
Estimated billable $ on the dashboard tiles. **Cheap (~1 hr) — include if Phase 3 finishes early.**

### 🤖 E. Auto-Generated Claim Narrative
LLM writes the claim justification paragraph.

### 🩹 F. Wound Visualization (SVG)
Ellipse rendered from L×W, colored by drainage.

### 🔁 G. Live-Streaming Pipeline View
Real-time view of ingestion: 429s, retries, throughput. **Partially free** — we already have Prefect UI + Grafana. Pin those two URLs into the dashboard nav for the demo.

### 🧪 H. Multi-Model Comparison
Same Envive narrative → 3 Baseten models side-by-side.

### 🚨 I. Anomaly Detection
Rule-based flagging of suspicious wound trajectory patterns.

---

## 9. Team Workstreams (3–4 people)

| Person | Phase 0 | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 4.5 |
|---|---|---|---|---|---|---|
| **A — Pipeline** | Docker Compose, Prefect, Makefile | Ingestion + retry + idempotency | Help with cache layer | API endpoints | — | Scale benchmark, synthetic data |
| **B — Extraction** | Alembic, schema | Help with sync test fixtures | Regex + LLM + cross-val | Rules engine + audit + tests | — | LLM throughput tuning |
| **C — Frontend** | Next.js scaffold, design tokens | — | Patient detail wireframe | Dashboard + table + drawer | Voice UI integration | Virtualization tuning |
| **D — WOW / Glue** | DevX, env setup, GH Actions | Observability (Grafana board) | Demo data curation | Polish, copy, hero patients | Voice copilot end-to-end | Benchmark dashboard |

If only 3 people, fold D's pipeline tasks into A; D starts voice in Phase 3.

---

## 10. Critical Files (to create)

- `docker-compose.yml`, `Makefile`, `.env.example`
- `infra/grafana/*.json` — preloaded dashboards
- `pipeline/client.py` — httpx + tenacity
- `pipeline/flows.py` — Prefect flows
- `pipeline/storage.py` — SQLAlchemy models + bulk upsert helpers
- `pipeline/migrations/` — Alembic
- `extraction/regex_extractor.py`
- `extraction/phi_redactor.py` — Microsoft Presidio wrapper; redacts text + returns local mapping for re-attachment
- `extraction/llm_extractor.py` — Baseten client + structured prompts + cache lookup (calls phi_redactor first)
- `extraction/reconcile.py` — cross-validation + confidence
- `eligibility/rules.py` — decision logic + reason templates
- `eligibility/wound_codes.py` — ICD-10 wound code constants + matchers (L89.*, E11.621, I83.0*, etc.)
- `api/main.py`, `api/patients.py`, `api/stats.py`
- `api/voice.py` — 11Labs TTS
- `api/chat.py` — LLM Q&A with tool-use
- `api/observability.py` — structlog config + Prometheus metrics
- `synthetic/generator.py` — 1M+ row synthetic data
- `web/` — Next.js (App Router) + Tailwind + shadcn/ui
- `tests/test_extraction.py`, `tests/test_rules.py`, `tests/test_ingest_contract.py`
- `README.md`, `BENCHMARK.md`, `PRODUCTION_GAPS.md`

---

## 11. Verification Checklist (end-to-end)

Before calling it done:
1. `make bootstrap` from a clean clone brings up all services
2. `make sync` → all 300 patients ingested, zero unhandled 429s in logs, second run is a no-op
3. `make extract` → `extracted_wounds` populated; second run >95% cache hits
4. `make decide` → eligibility distribution looks sane (not 100% in any bucket)
5. Dashboard loads at <500ms; filter to Facility A returns ~120
6. Click an auto_accept patient → extracted fields + source text + reason visible
7. Click a flag_for_review patient → reason explains ambiguity in plain English
8. Voice copilot answers a query about the hero patients verbally
9. **`make scale-test N=1000000` completes in <15 min, dashboard still responsive**
10. `pytest` green; `ruff check` clean; `mypy` clean on `extraction/` + `eligibility/`
11. **PHI redaction test green** — no name/DOB/MRN ever reaches the LLM client in any code path
12. 10-minute demo runs without dead air or broken clicks
13. `PRODUCTION_GAPS.md` lists deferred work honestly (HA, RBAC, secrets, tracing)

---

## 12. Production Gaps (what we'd add for real prod)

| Gap | Why deferred | What we'd add |
|---|---|---|
| HA / multi-region | Single laptop demo | RDS Multi-AZ, multi-region read replicas, S3 for blobs |
| AuthN/Z | No real users | OAuth/OIDC via Auth0 or Cognito, RBAC per facility |
| Secret management | `.env` is fine for demo | AWS Secrets Manager / Vault; rotate Baseten + 11Labs keys |
| Distributed tracing | structlog covers it locally | OpenTelemetry → Tempo |
| Alerting | None | PagerDuty on Grafana SLOs (ingest lag, extraction failure rate) |
| HIPAA / SOC 2 | Synthetic data — but PHI redaction layer (Presidio) is **built in already** | BAA with Baseten + 11Labs, self-hosted LLM option, encrypted-at-rest, audit log retention, redaction recall metrics |
| Backups / DR | None | RDS snapshots + cross-region replication |
| Cost guardrails | LLM cache helps | Per-org rate limits, Baseten spend dashboards, model fallback policy |
| Schema-drift detection on API | Manual | Pydantic strict mode + contract tests in CI nightly |

---

## 13. Open Questions

1. **Confirm scope:** drop to 1 WOW (voice copilot) + scale benchmark? Or push harder, accept timeline slip, and ship A + B?
2. **Prefect Cloud vs self-hosted?** Default: self-hosted in Docker Compose for the demo.
3. **Baseten model picks?** Benchmarked in Phase 2.
4. **Demo machine spec:** confirm laptop can run Docker Compose with Postgres + Redis + Prefect + Grafana + 2 Node processes simultaneously.
5. **Synthetic data realism:** copy distributions from the 300 real records, or hand-author a richer distribution? Default: copy real.
6. **Envive `note_type` string:** discovered from live data in Phase 1; gate LLM tier accordingly.
7. **Wound ICD-10 list completeness:** confirm with a clinical reviewer if any of the codes in `eligibility/wound_codes.py` are too broad/narrow for ABI's billing rules.
