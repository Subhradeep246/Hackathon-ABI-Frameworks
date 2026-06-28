-- ABI Wound Care Eligibility Pipeline — Storage Schema
-- PostgreSQL. Timestamps stored as TIMESTAMPTZ, date-only fields as DATE,
-- JSON payloads as JSONB, boolean flags as BOOLEAN.

-- ============================================================
-- CORE PCC MIRROR TABLES
-- ============================================================

-- patients: bridges the two identifier systems used by the PCC API.
--   patient_internal_id (int)  -> used by /notes and /assessments  (PCC's "id")
--   patient_id (string)        -> used by /diagnoses and /coverage (PCC's "patient_id", e.g. "FA-001")
-- All other tables join through patient_internal_id so a join never
-- breaks because one side used the string id and the other the int id.
CREATE TABLE IF NOT EXISTS patients (
    patient_internal_id   INTEGER PRIMARY KEY,    -- PCC's integer "id"
    patient_id            TEXT NOT NULL UNIQUE,   -- PCC's string "patient_id", e.g. FA-001
    facility_id           INTEGER NOT NULL,
    first_name            TEXT,
    last_name             TEXT,
    birth_date            DATE,
    gender                TEXT,
    primary_payer_code    TEXT,                   -- e.g. 'MCB', 'MCA', 'MCD', 'HMO'
    is_new_admission      BOOLEAN NOT NULL DEFAULT FALSE,
    last_modified_at      TIMESTAMPTZ,            -- when PCC last modified this record
    raw_json              JSONB,                  -- full original API payload, for audit/debug
    last_synced_at        TIMESTAMPTZ NOT NULL,
    sync_run_id           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patients_facility   ON patients(facility_id);
CREATE INDEX IF NOT EXISTS idx_patients_patient_id ON patients(patient_id);


-- diagnoses: ICD-10 codes per patient (keyed by string patient_id at the API level,
-- but stored against patient_internal_id once resolved).
CREATE TABLE IF NOT EXISTS diagnoses (
    diagnosis_pk          SERIAL PRIMARY KEY,
    source_id             INTEGER,                -- API's own "id" for deduplication on re-sync
    patient_internal_id   INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    icd10_code            TEXT,
    icd10_description     TEXT,
    clinical_status       TEXT,                  -- 'active' | 'resolved' | 'inactive'
    onset_date            DATE,
    last_modified_at      TIMESTAMPTZ,
    raw_json              JSONB,
    last_synced_at        TIMESTAMPTZ NOT NULL,
    sync_run_id           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnoses_patient   ON diagnoses(patient_internal_id);
CREATE INDEX IF NOT EXISTS idx_diagnoses_source_id ON diagnoses(source_id);


-- coverage: raw insurance coverage records, one row per coverage period reported by PCC.
CREATE TABLE IF NOT EXISTS coverage (
    coverage_pk           SERIAL PRIMARY KEY,
    source_id             INTEGER,               -- API's own "id" for deduplication on re-sync
    patient_internal_id   INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    payer_type            TEXT,                  -- e.g. 'Medicare B', 'Medicare A', 'Medicaid', 'HMO'
    payer_name            TEXT,
    payer_code            TEXT,                  -- e.g. 'MCB', 'MCA', 'MCD', 'HMO'
    effective_from        TIMESTAMPTZ,
    effective_to          TIMESTAMPTZ,           -- NULL = open-ended / still active
    last_modified_at      TIMESTAMPTZ,
    raw_json              JSONB,
    last_synced_at        TIMESTAMPTZ NOT NULL,
    sync_run_id           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_patient   ON coverage(patient_internal_id);
CREATE INDEX IF NOT EXISTS idx_coverage_source_id ON coverage(source_id);


-- notes: free-text progress notes (Structured SPN and Envive narrative formats all land here
-- as raw text; structured wound extraction happens downstream into wound_extractions).
CREATE TABLE IF NOT EXISTS notes (
    note_pk               SERIAL PRIMARY KEY,
    source_id             INTEGER,               -- API's own "id" for the note row
    patient_internal_id   INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    pcc_note_id           INTEGER,               -- PCC's original note ID (pcc_note_id from API)
    org_id                TEXT,
    note_type             TEXT,                  -- 'Wound (SPN)' | 'HP Skin & Wound Note' | Envive narrative
    effective_date        TIMESTAMPTZ,
    note_text             TEXT NOT NULL,         -- full plaintext of the clinical note (NLP extraction target)
    created_by            TEXT,
    note_label            TEXT,                  -- NLP-generated smart label; NULL until pipeline processes it
    note_format_guess     TEXT,                  -- 'soap' | 'prose' | 'multi_wound' | 'envive' | 'unknown'
    sync_version          INTEGER,
    is_current            BOOLEAN NOT NULL DEFAULT TRUE,
    raw_json              JSONB,
    last_synced_at        TIMESTAMPTZ NOT NULL,
    sync_run_id           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_patient   ON notes(patient_internal_id);
CREATE INDEX IF NOT EXISTS idx_notes_date      ON notes(effective_date);
CREATE INDEX IF NOT EXISTS idx_notes_source_id ON notes(source_id);


-- assessments: structured wound assessment forms (these are the most reliably "known" source).
CREATE TABLE IF NOT EXISTS assessments (
    assessment_pk              SERIAL PRIMARY KEY,
    source_id                  INTEGER,          -- API's own "id" for deduplication on re-sync
    patient_internal_id        INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    pcc_assessment_id          INTEGER,          -- PCC's original assessment ID
    org_id                     TEXT,
    assessment_type            TEXT,             -- 'Weekly Wound Information Sheet' | 'HP Skin & Wound'
    status                     TEXT,             -- 'Complete' | 'In-Progress'
    assessment_date            DATE,
    completion_date            DATE,
    template_id                INTEGER,
    assessment_type_description TEXT,            -- 'Admissions' | 'Quarterly' | 'Annual'
    sync_version               INTEGER,
    is_current                 BOOLEAN NOT NULL DEFAULT TRUE,
    raw_json                   JSONB,            -- structured assessment data; parse for wound measurements
    raw_text                   TEXT,             -- some assessments may still carry free-text fields
    last_synced_at             TIMESTAMPTZ NOT NULL,
    sync_run_id                INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assessments_patient   ON assessments(patient_internal_id);
CREATE INDEX IF NOT EXISTS idx_assessments_date      ON assessments(assessment_date);
CREATE INDEX IF NOT EXISTS idx_assessments_source_id ON assessments(source_id);


-- ============================================================
-- PARSED / STRUCTURED WOUND DATA
-- ============================================================

-- wound_extractions: one row per *wound* found inside a single note or assessment.
-- A note describing two wounds (multi-wound format) produces two rows here,
-- linked by source_table + source_pk, with is_primary marking which one
-- the eligibility engine should use.
CREATE TABLE IF NOT EXISTS wound_extractions (
    extraction_pk             SERIAL PRIMARY KEY,
    patient_internal_id       INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    source_table              TEXT NOT NULL CHECK (source_table IN ('notes', 'assessments')),
    source_pk                 INTEGER NOT NULL,  -- note_pk or assessment_pk
    source_date               DATE,
    wound_index_in_source     INTEGER NOT NULL DEFAULT 0,  -- 0 = first wound in doc, 1 = second, etc.
    is_primary                BOOLEAN NOT NULL DEFAULT FALSE,  -- chosen primary wound for that doc

    -- structured wound fields
    location                  TEXT,
    wound_type                TEXT,   -- pressure_ulcer | diabetic_foot_ulcer | venous_stasis_ulcer |
                                      -- arterial_ulcer | surgical_site_infection | abscess | burn
    stage                     TEXT,   -- '2'|'3'|'4'|'unstageable'|NULL (only meaningful for pressure ulcers)
    length_cm                 NUMERIC(6,2),
    width_cm                  NUMERIC(6,2),
    depth_cm                  NUMERIC(6,2),
    drainage_amount           TEXT,   -- none | light | moderate | heavy

    -- per-field status tracking (unknown-aware parsing)
    -- each status is one of: known | unknown_missing | unknown_unparseable | unknown_conflict | unknown_out_of_range
    location_status           TEXT NOT NULL DEFAULT 'unknown_missing',
    wound_type_status         TEXT NOT NULL DEFAULT 'unknown_missing',
    stage_status              TEXT NOT NULL DEFAULT 'unknown_missing',
    length_cm_status          TEXT NOT NULL DEFAULT 'unknown_missing',
    width_cm_status           TEXT NOT NULL DEFAULT 'unknown_missing',
    depth_cm_status           TEXT NOT NULL DEFAULT 'unknown_missing',
    drainage_amount_status    TEXT NOT NULL DEFAULT 'unknown_missing',

    note_format               TEXT,   -- format detected for this specific source doc
    secondary_wounds_json     JSONB,  -- sibling wound dicts from same doc, for audit
    parser_version            TEXT NOT NULL,
    parsed_at                 TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wound_extractions_patient ON wound_extractions(patient_internal_id);
CREATE INDEX IF NOT EXISTS idx_wound_extractions_source  ON wound_extractions(source_table, source_pk);
CREATE INDEX IF NOT EXISTS idx_wound_extractions_primary ON wound_extractions(patient_internal_id, is_primary);


-- ============================================================
-- DATA QUALITY / UNKNOWN TRACKING
-- ============================================================

-- unknown_flags: a generic, queryable log of every unknown/ambiguous condition encountered,
-- across parsing, coverage, and conflict-detection. This is what powers the
-- Unknown Risk Score and the "what we don't know" patient panel.
CREATE TABLE IF NOT EXISTS unknown_flags (
    flag_pk               SERIAL PRIMARY KEY,
    patient_internal_id   INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    flag_type             TEXT NOT NULL,   -- e.g. 'unknown_missing_field', 'note_assessment_conflict',
                                           -- 'multiple_eligible_wounds', 'unknown_conflicting_payers',
                                           -- 'unknown_missing_dates', 'envive_narrative_only'
    severity              TEXT NOT NULL DEFAULT 'medium',  -- low | medium | high
    source_table          TEXT,            -- 'notes' | 'assessments' | 'coverage' | NULL (patient-level)
    source_pk             INTEGER,
    field_name            TEXT,            -- which structured field this concerns, if applicable
    detail                TEXT,            -- human-readable explanation
    created_at            TIMESTAMPTZ NOT NULL,
    sync_run_id           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_unknown_flags_patient ON unknown_flags(patient_internal_id);
CREATE INDEX IF NOT EXISTS idx_unknown_flags_type    ON unknown_flags(flag_type);


-- ============================================================
-- ELIGIBILITY (final per-patient output row)
-- ============================================================

CREATE TABLE IF NOT EXISTS eligibility (
    patient_internal_id          INTEGER PRIMARY KEY REFERENCES patients(patient_internal_id),
    patient_id                   TEXT NOT NULL,
    facility_id                  INTEGER NOT NULL,

    -- chosen primary wound (resolved across notes + assessments, conflict-aware)
    primary_wound_type           TEXT,
    primary_wound_stage          TEXT,
    primary_wound_location       TEXT,
    length_cm                    NUMERIC(6,2),
    width_cm                     NUMERIC(6,2),
    depth_cm                     NUMERIC(6,2),
    drainage_amount              TEXT,
    primary_wound_source_table   TEXT,    -- which doc type the primary wound came from
    primary_wound_source_pk      INTEGER,

    -- coverage
    has_active_medicare_b        BOOLEAN NOT NULL DEFAULT FALSE,
    other_active_payers_json     JSONB,   -- e.g. ["Medicaid"]
    coverage_unknown_flags_json  JSONB,

    -- decision
    routing_decision             TEXT NOT NULL,  -- auto_accept | flag_for_review | reject
    routing_reason               TEXT NOT NULL,  -- plain-English explanation

    -- unknown risk scoring
    unknown_risk_score           INTEGER NOT NULL DEFAULT 0,
    unknown_risk_tier            TEXT NOT NULL DEFAULT 'green',  -- green | yellow | red
    unknown_flag_count           INTEGER NOT NULL DEFAULT 0,

    -- conflict markers (booleans, mirrored from unknown_flags for fast dashboard filtering)
    note_assessment_conflict     BOOLEAN NOT NULL DEFAULT FALSE,
    multiple_eligible_wounds     BOOLEAN NOT NULL DEFAULT FALSE,
    envive_narrative_only        BOOLEAN NOT NULL DEFAULT FALSE,

    -- biller override / audit
    override_decision            TEXT,
    override_justification       TEXT,
    override_by                  TEXT,
    override_at                  TIMESTAMPTZ,

    computed_at                  TIMESTAMPTZ NOT NULL,
    sync_run_id                  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eligibility_routing  ON eligibility(routing_decision);
CREATE INDEX IF NOT EXISTS idx_eligibility_facility ON eligibility(facility_id);
CREATE INDEX IF NOT EXISTS idx_eligibility_risk     ON eligibility(unknown_risk_tier);


-- override_history: append-only log so every override is auditable, not just the latest one.
CREATE TABLE IF NOT EXISTS override_history (
    override_pk           SERIAL PRIMARY KEY,
    patient_internal_id   INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    previous_decision     TEXT NOT NULL,
    new_decision          TEXT NOT NULL,
    justification         TEXT NOT NULL,
    overridden_by         TEXT NOT NULL,
    overridden_at         TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_override_history_patient ON override_history(patient_internal_id);


-- ============================================================
-- SYNC / INGESTION TRACKING
-- ============================================================

CREATE TABLE IF NOT EXISTS sync_runs (
    sync_run_id   SERIAL PRIMARY KEY,
    sync_type     TEXT NOT NULL,          -- 'full' | 'incremental'
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | complete | partial | failed
    since_param   TEXT,                   -- the 'since' value used, for incremental syncs
    notes         TEXT
);

-- facility_sync_status: per-facility, per-endpoint status for one sync run.
-- Lets you see exactly which facility/endpoint combos failed or were partial,
-- without re-running the whole pipeline to find out.
CREATE TABLE IF NOT EXISTS facility_sync_status (
    status_pk        SERIAL PRIMARY KEY,
    sync_run_id      INTEGER NOT NULL REFERENCES sync_runs(sync_run_id),
    facility_id      INTEGER NOT NULL,
    endpoint         TEXT NOT NULL,       -- 'patients' | 'diagnoses' | 'coverage' | 'notes' | 'assessments'
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | complete | partial | failed
    records_fetched  INTEGER NOT NULL DEFAULT 0,
    error_detail     TEXT,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_facility_sync_status_run ON facility_sync_status(sync_run_id);
