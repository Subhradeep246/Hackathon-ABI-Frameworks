# PCC Mock API — Rate Limiting & Retry Guide

This document describes how the hackathon PCC API handles rate limits and how **Pulse** implements retry logic so pipelines complete reliably.

---

## Rate limiting behavior

> **Every request has a ~30% chance of returning HTTP 429.**

When rate limited:

| Item | Value |
|------|--------|
| Status | `429 Too Many Requests` |
| Header | `Retry-After` — seconds to wait before retrying |
| Body | Error message (optional) |

**Pipelines that do not handle 429s will fail to load data.** With 300 patients × 4 endpoints per patient, expect hundreds of 429s per full sync. Retries are mandatory, not optional.

---

## Required retry pattern

### 1. On HTTP 429 — always honor `Retry-After`

```python
response = await client.get("/pcc/patients", params={"facility_id": 101})
if response.status_code == 429:
    retry_after = float(response.headers.get("Retry-After", "1"))
    await asyncio.sleep(retry_after)
    # retry the same request
```

Do **not** use a fixed sleep. Do **not** ignore the header.

### 2. Retry until success (with a safety cap)

With a 30% failure rate, a single endpoint may need **many** retries. Pulse defaults:

| Env var | Default | Purpose |
|---------|---------|---------|
| `API_MAX_429_RETRIES` | `60` | Max 429 retries **per request** |
| `API_MAX_ERROR_RETRIES` | `8` | Max retries for 5xx / transport errors |
| `API_MAX_CONCURRENT` | `4` | Concurrent in-flight requests (lower = fewer 429s) |
| `SYNC_CONCURRENCY` | `3` | Parallel patients during detail sync |

### 3. Limit concurrency

Bursting 20+ parallel requests increases 429 storms. Pulse uses:

- Global `asyncio.Semaphore` on all API calls
- Per-patient sync semaphore (`SYNC_CONCURRENCY`)

### 4. Retry failed patients after batch sync

Even with per-request retries, a patient-level sync can fail. Pulse re-queues failed patients and retries up to `SYNC_PATIENT_RETRIES` times with lower concurrency.

### 5. Do not retry 422

`422 Unprocessable Entity` means invalid parameters — fix the request, don't retry blindly.

---

## Implementation in Pulse

| File | Responsibility |
|------|----------------|
| `backend/ingestion/client.py` | `fetch_json()` — 429 loop with `Retry-After`, 5xx backoff, stats |
| `backend/pipeline.py` | Patient-level retry queue after `asyncio.gather` |
| `backend/jobs.py` | Auto-sync with incremental watermarks; retries on next cycle |

### Client retry flow

```
fetch_json(path)
  └─ loop:
       ├─ GET path
       ├─ 429 → sleep(Retry-After + jitter) → retry (up to API_MAX_429_RETRIES)
       ├─ 5xx → exponential backoff → retry (up to API_MAX_ERROR_RETRIES)
       ├─ 422 → raise immediately
       └─ 200 → return JSON
```

### Observability

`get_client_stats()` returns:

```json
{
  "requests": 1200,
  "rate_limited": 380,
  "retries": 410,
  "errors": 0
}
```

Logged at end of each sync run.

---

## Recommended settings for large datasets

```env
# Conservative — fewer 429s, slower sync
API_MAX_CONCURRENT=3
SYNC_CONCURRENCY=2
API_MAX_429_RETRIES=80

# Faster — more 429s, relies on retries
API_MAX_CONCURRENT=6
SYNC_CONCURRENCY=5
API_MAX_429_RETRIES=100
```

---

## Testing retry behavior

```bash
# Run sync and watch 429 stats in logs
python backend/cli.py sync

# Or trigger from dashboard auto-sync and check server logs
```

A successful full sync of 300 patients should show `rate_limited` > 0 in stats but `errors: 0`.

---

## API endpoints (reference)

| Endpoint | Params |
|----------|--------|
| `GET /pcc/patients` | `facility_id`, optional `since`, `limit`, `offset` |
| `GET /pcc/diagnoses` | `patient_id` |
| `GET /pcc/coverage` | `patient_id` |
| `GET /pcc/notes` | `patient_id` (internal id) |
| `GET /pcc/assessments` | `patient_id` (internal id) |

Base URL: `https://hackathon.prod.pulsefoundry.ai` (override with `PCC_BASE_URL`).

---

*Pulse is built around this contract — ingestion will not complete reliably without Retry-After handling.*
