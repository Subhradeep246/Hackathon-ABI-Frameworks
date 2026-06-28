# Wound-Care Triage MCP Server

`mcp_server.py` is the **decision/extraction core** for the wound-care billing
triage pipeline. It is a [FastMCP](https://github.com/jlowin/fastmcp) server that
exposes five deterministic tools. It is the **only** code that reads raw note /
assessment text — everything else (ingestion, gates, storage, dashboard) is built
*around* it by the rest of the team and calls these tools with already-fetched
data.

> Spec source of truth: [`PRD.md`](./PRD.md). This server implements the
> "MCP server" section and the Phase 3 / Phase 5 logic exactly.

---

## What this server does (and does not)

| Does | Does **not** |
|---|---|
| Parse structured assessments → wound fields | Make any HTTP / EHR API calls |
| Regex-extract wound fields from free-text notes | Touch the database |
| Decide `auto_accept` / `flag_for_review` / `reject` | Know how many patients exist |
| Pick the primary wound when several are documented | Rate-limit, retry, or paginate |

Every tool is a **pure, deterministic function of its inputs**. No network, no DB,
no global state. That is deliberate — it's why the server is identical whether you
process 300 patients or 30 million (see [Scalability](#scalability)).

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install fastmcp                 # plus pytest to run the tests
```

Run the stdio server:

```bash
python mcp_server.py
```

Run the tests:

```bash
pytest test_mcp_server.py -v
```

---

## The five tools

All extraction tools return the **same field set**:
`wound_type, stage, location, length_cm, width_cm, depth_cm, drainage_amount,
drainage_type, source, confidence, missing`.

`missing` is the list of absent **required** fields. Required =
`length_cm, width_cm, depth_cm, drainage_amount`.

### `extract_from_assessment(raw_json: str) -> dict`
`json.loads()` the assessment string and map the structured fields.
`confidence = "high"` if all four required fields are present, else `"medium"`.
`source = "assessment"`.

```python
extract_from_assessment('{"wound_type":"pressure_ulcer","stage":2,'
                        '"location":"Sacrum","length_cm":3.2,"width_cm":2.1,'
                        '"depth_cm":0.4,"drainage_amount":"moderate"}')
# -> {... "source":"assessment", "confidence":"high", "missing":[]}
```

### `extract_from_note(note_text: str) -> dict`
Regex extraction from free text. Handles:
- **Labeled** measurements: `Length: 3.2 cm  Width: 2.1 cm  Depth: 0.4 cm`
- **Shorthand**: `4.2x3.1x1.5`
- **Drainage** keywords + synonyms → `none / light / moderate / heavy`
- **Wound type** by keyword; **multi-wound** primary selection by
  severity → surface area → recency.

`confidence = "medium"` if all required fields found, else `"low"`.
`source = "note"`.

### `check_part_b(coverage_records: list[dict]) -> dict`
```python
{"has_active_part_b": bool, "payer": str}
```
`True` if any record has `payer_code == "MCB"` (or `payer_type == "Medicare B"`)
**and** `effective_to is None`.

### `check_active_wound_dx(diagnoses: list[dict]) -> dict`
```python
{"has_active_wound": bool, "icd10_code": str}
```
`True` if any diagnosis is `clinical_status == "active"` **and** matches a wound by
**ICD-10 prefix** (`L89, L97, L98, I83.0, I83.2, T81.4, T81.3, L02, E11.621,
E10.621, T20–T32` burns) **or** by **description keyword**
(`ulcer, wound, abscess, burn, surgical site`).

### `decide_eligibility(wound_fields, has_active_part_b, has_active_wound) -> dict`
Deterministic rule table, evaluated **in order**. Returns the wound fields plus
`decision`, `reason` (plain English), and `rule_fired` (audit key):

| Order | Condition | decision | rule_fired |
|---|---|---|---|
| 1 | no active Part B | `reject` | `no_part_b` |
| 2 | no active wound dx | `reject` | `no_active_wound` |
| 3 | wound + Part B, but measurements/drainage missing | `flag_for_review` | `incomplete_measurements` |
| 4 | all fields present but `confidence == "low"` | `flag_for_review` | `low_confidence` |
| 5 | otherwise | `auto_accept` | `complete` |

> **Key rule:** an active wound + active Part B with missing measurements is
> **flagged, never rejected**. Rejection is only for missing Part B or no wound.

---

## How the pipeline should call it

The team's `pipeline.py` orchestrates; this server only computes. Per patient:

```
1. check_part_b(coverage)           # gate — reject if false
2. check_active_wound_dx(diagnoses) # record result; let step 5 decide
3. extract_from_assessment(raw_json)            # best-available extraction
   └─ if any required field missing: also extract_from_note(note_text)
   └─ keep the higher-confidence complete result
4. decide_eligibility(fields, has_active_part_b, has_active_wound)
5. upsert the returned row into the eligibility table
```

You can call the tools two ways:

**A. Directly import the pure functions** (fastest; recommended for a
single-process pipeline):

```python
from mcp_server import (
    _extract_from_assessment_impl, _extract_from_note_impl,
    _check_part_b_impl, _check_active_wound_dx_impl, _decide_eligibility_impl,
)
fields = _extract_from_assessment_impl(raw_json)
```

**B. Over the MCP protocol** (if a separate agent/LLM client drives the tools —
this keeps raw text isolated to the tool side):

```python
import asyncio
from fastmcp import Client
import mcp_server

async def run():
    async with Client(mcp_server.mcp) as c:
        res = await c.call_tool("check_part_b", {"coverage_records": coverage})
        print(res.data)   # {"has_active_part_b": ..., "payer": ...}

asyncio.run(run())
```

To register with an MCP-capable client (Claude Desktop, etc.), point it at
`python mcp_server.py` over stdio.

---

## Scalability

The founder expects **millions of records**. Here is the honest picture and the
steps to get there. **None of these changes touch this MCP server** — its tools are
pure functions and scale by running more of them. All the work lives in the
pipeline (fetch + persist) that the team builds around it.

### Why the core is already scale-safe
Each tool is stateless and deterministic over one patient's data. To process
millions, you **fan out** — run many tool invocations in parallel (threads,
processes, or workers). The tool code is unchanged.

### Where the real work is (in `pipeline.py`, not here)

1. **Client-side rate limiter (token bucket).** The mock API throttles ~30% of
   requests. Don't fire-and-retry blindly; pace requests *under* the limit with a
   token bucket so 429s become rare. → predictable throughput.

2. **Distinguish "retries exhausted" from "no data."** The naive retry returns
   `[]` both when a patient truly has no coverage *and* when all retries failed.
   At millions of requests that silently mis-rejects thousands of real patients.
   Have `request_with_retry` signal exhaustion (e.g. a result object with an
   `exhausted` flag); on exhaustion mark the patient `fetch_incomplete` and retry
   on the next run — **never** record `reject`.

3. **Batched / bulk inserts.** Replace row-by-row `INSERT ... ON CONFLICT` with
   batched `executemany` or Postgres `COPY` (500–1000 rows per flush), keeping the
   upsert semantics so reruns stay duplicate-free.

4. **Incremental sync (`since`).** The API supports a `since` parameter on
   `/patients`, `/notes`, `/assessments`. After the first full load, only fetch
   records modified since the last run — turns "re-scan everything" into "process
   the delta."

5. **Concurrency.** Make fetching and tool invocation parallel (async HTTP with a
   bounded connection pool, or a worker pool / queue). This is the change that
   actually makes "millions" feasible in a reasonable window — sequential, even
   perfectly paced, cannot pull millions of throttled requests in time.

6. **Paginated / aggregated dashboard.** Don't render one row per patient for
   millions. Serve indexed, aggregated counts and a paginated, filterable list
   (by facility and decision); add DB indexes on `decision`, `facility_id`.

### Suggested build order

| Step | Effort | Payoff |
|---|---|---|
| Token bucket rate limiter | low | fewer 429s, steady throughput |
| Exhausted-vs-empty signal | low | **correctness** — no silent mis-rejects |
| Batched inserts | low | 10–100× faster writes |
| Incremental `since` sync | medium | only process the delta |
| Concurrency (pool/queue) | high | the actual "millions" unlock |
| Paginated dashboard + indexes | medium | UI usable at scale |

> Bottom line: the decision logic in this server is done and scale-agnostic. The
> path to millions is entirely in the ingestion and persistence layers the team
> builds around it — start with the three low-effort items (rate limiter,
> exhausted-vs-empty, batched inserts), then add `since` and concurrency.
