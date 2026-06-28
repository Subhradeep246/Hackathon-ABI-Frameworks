# Hackathon Mock API — Reference

This service emulates an API for the ABI Frameworks hackathon. It exposes read-only endpoints for patients, diagnoses, insurance coverage, progress notes, and wound assessments. All data is synthetic — no real PHI.

**Base URL:** `https://hackathon.prod.pulsefoundry.ai`

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Rate Limiting](#rate-limiting)
3. [Data Model Overview](#data-model-overview)
4. [Facilities & Patient IDs](#facilities--patient-ids)
5. [Endpoints](#endpoints)
   - [Health](#health)
   - [Patients](#patients)
   - [Diagnoses](#diagnoses)
   - [Coverage](#coverage)
   - [Progress Notes](#progress-notes)
   - [Assessments](#assessments)
6. [Incremental Fetching (`since`)](#incremental-fetching-since)
7. [Payer Codes Reference](#payer-codes-reference)
8. [End-to-End Walkthrough](#end-to-end-walkthrough)

---

## Quick Start

```bash
# 1. Verify the service is up
curl https://hackathon.prod.pulsefoundry.ai/health

# 2. List all patients in Facility A
curl "https://hackathon.prod.pulsefoundry.ai/pcc/patients?facility_id=101"

# 3. Fetch diagnoses for the first patient
curl "https://hackathon.prod.pulsefoundry.ai/pcc/diagnoses?patient_id=FA-001"

# 4. Fetch the most recent progress notes for a patient (internal id = 1)
curl "https://hackathon.prod.pulsefoundry.ai/pcc/notes?patient_id=1"
```

---

## Rate Limiting

**Every request has a 30% chance of returning HTTP 429.** This is intentional — it simulates real PCC API throttling. Your code must handle it.

```
HTTP/1.1 429 Too Many Requests
Retry-After: 3
Content-Type: application/json

{"detail": "Rate limit exceeded. Back off and retry."}
```

The `Retry-After` header is a random integer between 1 and 5 (seconds). Implement exponential back-off or simple retry logic on every call.

---

## Data Model Overview

The database contains two layers of patient identity:

| Table | Key field | Description |
|---|---|---|
| `pcc_patient` | `patient_id` (string, e.g. `FA-001`) | External PCC identifier, shared across diagnoses and coverage |
| `patient` | `id` (integer, e.g. `1`) | Internal identifier, used for progress notes and assessments |

You will frequently need to resolve `patient_id` → `id`. The patients endpoint returns both.

```
pcc_patient (FA-001, facility_id=101)
    │
    ├── pcc_diagnosis   (FK: patient_id = "FA-001")
    ├── pcc_coverage    (FK: patient_id = "FA-001")
    │
    └── patient (id=1, pcc_patient_id="FA-001")
            │
            ├── progress_note   (FK: patient_id = 1)
            └── pcc_assessment  (FK: patient_id = 1)
```

---

## Facilities & Patient IDs

There are three mock facilities. Each has 100 patients.

| facility_id | Name | Patient ID prefix | Example |
|---|---|---|---|
| `101` | Facility A | `FA-` | `FA-001` … `FA-100` |
| `102` | Facility B | `FB-` | `FB-001` … `FB-100` |
| `103` | Facility C | `FC-` | `FC-001` … `FC-100` |

---

## Endpoints

---

### Patients

#### `GET /pcc/patients`

Returns all patients for a given facility. Optionally filter to only patients modified after a given timestamp.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `facility_id` | integer | Yes | `101`, `102`, or `103` |
| `since` | ISO 8601 timestamp | No | Return only patients where `last_modified_at` ≥ this value |

**Request:**
```bash
# All patients in Facility A
curl "https://hackathon.prod.pulsefoundry.ai/pcc/patients?facility_id=101"

# Only patients modified in the last week
curl "https://hackathon.prod.pulsefoundry.ai/pcc/patients?facility_id=101&since=2026-05-10T00:00:00"

# Facility B, new admissions filter (post-process in your code)
curl "https://hackathon.prod.pulsefoundry.ai/pcc/patients?facility_id=102"
```

**Response `200`:**
```json
[
  {
    "id": 1,
    "facility_id": 101,
    "patient_id": "FA-001",
    "first_name": "Agnes",
    "last_name": "Dunbar",
    "birth_date": "1942-05-04",
    "gender": "Female",
    "primary_payer_code": "MCB",
    "last_modified_at": "2026-05-17T19:13:00",
    "is_new_admission": true
  },
  {
    "id": 2,
    "facility_id": 101,
    "patient_id": "FA-002",
    "first_name": "Leon",
    "last_name": "Dawson",
    "birth_date": "1943-02-25",
    "gender": "Male",
    "primary_payer_code": "HMO",
    "last_modified_at": "2026-05-15T12:24:00",
    "is_new_admission": true
  }
]
```

**Response schema:**

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Internal patient ID — use this for `/pcc/notes` and `/pcc/assessments` |
| `facility_id` | integer | 101, 102, or 103 |
| `patient_id` | string | PCC identifier — use this for `/pcc/diagnoses` and `/pcc/coverage` |
| `first_name` | string \| null | |
| `last_name` | string \| null | |
| `birth_date` | string \| null | `YYYY-MM-DD` |
| `gender` | string \| null | `"Male"` \| `"Female"` |
| `primary_payer_code` | string \| null | See [Payer Codes](#payer-codes-reference) |
| `last_modified_at` | ISO 8601 \| null | |
| `is_new_admission` | boolean | `true` if admitted in the current sync window |

---

### Diagnoses

#### `GET /pcc/diagnoses`

Returns all ICD-10 diagnoses on record for a patient.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `patient_id` | string | Yes | PCC patient identifier, e.g. `FA-001` |

**Request:**
```bash
curl "https://hackathon.prod.pulsefoundry.ai/pcc/diagnoses?patient_id=FA-001"
```

**Response `200`:**
```json
[
  {
    "id": 1,
    "patient_id": "FA-001",
    "icd10_code": "L89.152",
    "icd10_description": "Pressure ulcer of sacral region, stage 2",
    "clinical_status": "active",
    "onset_date": "2026-04-10",
    "last_modified_at": "2026-05-17T19:13:00"
  },
  {
    "id": 2,
    "patient_id": "FA-001",
    "icd10_code": "E11.9",
    "icd10_description": "Type 2 diabetes mellitus without complications",
    "clinical_status": "active",
    "onset_date": "2015-03-01",
    "last_modified_at": "2026-05-17T19:13:00"
  }
]
```

**Response schema:**

| Field | Type | Notes |
|---|---|---|
| `id` | integer | |
| `patient_id` | string | Matches the `patient_id` you queried |
| `icd10_code` | string \| null | Standard ICD-10-CM code |
| `icd10_description` | string \| null | Human-readable description |
| `clinical_status` | string \| null | `"active"` \| `"resolved"` \| `"inactive"` |
| `onset_date` | string \| null | `YYYY-MM-DD` |
| `last_modified_at` | ISO 8601 \| null | |

---

### Coverage

#### `GET /pcc/coverage`

Returns insurance coverage records for a patient. A patient may have multiple records (e.g. Medicare A as primary, Medicaid as secondary).

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `patient_id` | string | Yes | PCC patient identifier, e.g. `FA-001` |

**Request:**
```bash
curl "https://hackathon.prod.pulsefoundry.ai/pcc/coverage?patient_id=FA-001"
```

**Response `200`:**
```json
[
  {
    "id": 1,
    "patient_id": "FA-001",
    "payer_name": "Medicare Part B",
    "payer_code": "MCB",
    "payer_type": "Medicare B",
    "effective_from": "2020-01-01T00:00:00",
    "effective_to": null,
    "last_modified_at": "2026-05-17T19:13:00"
  }
]
```

**Response schema:**

| Field | Type | Notes |
|---|---|---|
| `id` | integer | |
| `patient_id` | string | |
| `payer_name` | string \| null | Full name of the payer |
| `payer_code` | string \| null | Short code — see [Payer Codes](#payer-codes-reference) |
| `payer_type` | string \| null | `"Medicare B"` \| `"Medicare A"` \| `"Medicaid"` \| `"HMO"` |
| `effective_from` | ISO 8601 \| null | Start of coverage |
| `effective_to` | ISO 8601 \| null | End of coverage — `null` means currently active |
| `last_modified_at` | ISO 8601 \| null | |

---

### Progress Notes

#### `GET /pcc/notes`

Returns clinical progress notes for a patient. Only `is_current = true` records are returned (superseded note versions are excluded). Notes contain free-text wound narratives — this is the primary input for NLP extraction.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `patient_id` | integer | Yes | Internal patient `id` (from the patients response) |
| `since` | ISO 8601 timestamp | No | Return only notes where `effective_date` ≥ this value |

> **Important:** This endpoint takes the **integer** `id` field from the patients response, not the string `patient_id` like `FA-001`.

**Request:**
```bash
# All current notes for patient with internal id=1
curl "https://hackathon.prod.pulsefoundry.ai/pcc/notes?patient_id=1"

# Notes from the last 30 days
curl "https://hackathon.prod.pulsefoundry.ai/pcc/notes?patient_id=1&since=2026-04-28T00:00:00"
```

**Response `200`:**
```json
[
  {
    "id": 1,
    "patient_id": 1,
    "org_id": "ORG-101",
    "pcc_note_id": 10001,
    "note_type": "Wound (SPN)",
    "effective_date": "2026-05-10T09:00:00",
    "note_text": "Wound Assessment Note\nLocation: Sacrum\nWound Type: Pressure Ulcer, Stage 2\nLength: 3.2 cm  Width: 2.1 cm  Depth: 0.4 cm\nDrainage: Moderate serosanguineous\nPeriwound: Intact skin with mild erythema\nTreatment: Foam dressing with moisture barrier...",
    "created_by": "RN Smith",
    "note_label": null,
    "sync_version": 1,
    "is_current": true
  }
]
```

**Response schema:**

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Primary key for this note |
| `patient_id` | integer | Internal patient ID |
| `org_id` | string | Organization identifier |
| `pcc_note_id` | integer \| null | Original PCC note ID |
| `note_type` | string \| null | `"Wound (SPN)"`, `"HP Skin & Wound Note"`, or Envive narrative format |
| `effective_date` | ISO 8601 \| null | Date the note was clinically effective |
| `note_text` | string \| null | Full plaintext of the clinical note — the primary NLP extraction target |
| `created_by` | string \| null | Clinician who authored the note |
| `note_label` | string \| null | NLP-generated smart label; `null` until your pipeline processes it |

**Note formats:** Notes come in two formats you'll encounter in the data:

- **Structured SPN format** — labeled fields (`Location:`, `Wound Type:`, `Length:`, etc.) that are straightforward to parse with regex or an LLM.
- **Envive narrative format** — prose-style clinical narrative where measurements and wound details are embedded in free text. These are harder to parse and trigger `flag_for_review` routing.

---

### Assessments

#### `GET /pcc/assessments`

Returns structured wound assessments for a patient. Only `is_current = true` records are returned. Assessments contain structured JSON with labeled measurement fields.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `patient_id` | integer | Yes | Internal patient `id` (from the patients response) |
| `since` | ISO date | No | Return only assessments where `assessment_date` ≥ this value |

> **Important:** Like `/pcc/notes`, this endpoint takes the **integer** `id`, not the string `patient_id`.

**Request:**
```bash
# All current assessments for patient id=1
curl "https://hackathon.prod.pulsefoundry.ai/pcc/assessments?patient_id=1"

# Assessments from 2026 onward
curl "https://hackathon.prod.pulsefoundry.ai/pcc/assessments?patient_id=1&since=2026-01-01"
```

**Response `200`:**
```json
[
  {
    "id": 1,
    "patient_id": 1,
    "org_id": "ORG-101",
    "pcc_assessment_id": 20001,
    "assessment_type": "Weekly Wound Information Sheet",
    "status": "Complete",
    "assessment_date": "2026-05-10",
    "completion_date": "2026-05-10",
    "template_id": 5,
    "assessment_type_description": "Quarterly",
    "raw_json": "{\"wound_type\": \"pressure_ulcer\", \"stage\": 2, \"location\": \"Sacrum\", \"length_cm\": 3.2, \"width_cm\": 2.1, \"depth_cm\": 0.4, \"drainage_type\": \"serosanguineous\", \"drainage_amount\": \"moderate\"}",
    "sync_version": 1,
    "is_current": true
  }
]
```

**Response schema:**

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Primary key |
| `patient_id` | integer | Internal patient ID |
| `org_id` | string | Organization identifier |
| `pcc_assessment_id` | integer \| null | Original PCC assessment ID |
| `assessment_type` | string \| null | `"Weekly Wound Information Sheet"` \| `"HP Skin & Wound"` |
| `status` | string \| null | `"Complete"` \| `"In-Progress"` |
| `assessment_date` | date \| null | `YYYY-MM-DD` |
| `completion_date` | date \| null | `YYYY-MM-DD` |
| `template_id` | integer \| null | PCC template reference |
| `assessment_type_description` | string \| null | `"Admissions"` \| `"Quarterly"` \| `"Annual"` |
| `raw_json` | string \| null | JSON-encoded structured assessment data — parse this for wound measurements |

**`raw_json` structure (typical fields):**

```json
{
  "wound_type": "pressure_ulcer",
  "stage": 2,
  "location": "Sacrum",
  "length_cm": 3.2,
  "width_cm": 2.1,
  "depth_cm": 0.4,
  "drainage_type": "serosanguineous",
  "drainage_amount": "moderate"
}
```

---

## Incremental Fetching (`since`)

The `since` parameter lets you poll for changes rather than re-fetching entire tables. This mimics how a real integration would sync with PCC.

| Endpoint | `since` filters on |
|---|---|
| `GET /pcc/patients` | `last_modified_at` |
| `GET /pcc/notes` | `effective_date` |
| `GET /pcc/assessments` | `assessment_date` |

---

## Payer Codes Reference

| Code | Full name | Notes |
|---|---|---|
| `MCB` | Medicare Part B | Outpatient coverage — relevant for wound care billing |
| `MCA` | Medicare Part A | Inpatient/SNF coverage |
| `MCD` | Medicaid | State-funded insurance |
| `HMO` | HMO / Managed Care | Bundled payment, typically not separately billable |

Medicare Part B (`MCB`) is the most relevant payer for wound care eligibility — patients with active MCB coverage are the primary target population.

---

## Error Responses

All endpoints return standard HTTP error responses.

| Status | When |
|---|---|
| `429 Too Many Requests` | Rate limit hit (30% chance per request) — retry after `Retry-After` seconds |
| `422 Unprocessable Entity` | Missing or invalid query parameters |
| `500 Internal Server Error` | Unexpected server error — includes stack trace in `detail.error` |

**422 example (missing required parameter):**
```bash
curl "https://hackathon.prod.pulsefoundry.ai/pcc/patients"
# → 422: {"detail": [{"msg": "Field required", "loc": ["query", "facility_id"]}]}
```
