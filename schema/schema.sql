-- ABI Wound Care Eligibility Pipeline — Storage Schema
-- SQLite. Swap to Postgres later by changing the connection string.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS patients (
    patient_internal_id INTEGER PRIMARY KEY,
    patient_id           TEXT NOT NULL UNIQUE,
    facility_id           INTEGER NOT NULL,
    first_name            TEXT,
    last_name             TEXT,
    date_of_birth         TEXT,
    gender                TEXT,
    primary_payer_code    TEXT,
    last_modified_at      TEXT,
    is_new_admission      INTEGER NOT NULL DEFAULT 0,
    raw_json              TEXT,
    last_synced_at        TEXT NOT NULL,
    sync_run_id            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patients_facility ON patients(facility_id);
CREATE INDEX IF NOT EXISTS idx_patients_patient_id ON patients(patient_id);

CREATE TABLE IF NOT EXISTS diagnoses (
    diagnosis_pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_internal_id   INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    source_id              INTEGER NOT NULL,
    icd10_code             TEXT,
    description            TEXT,
    clinical_status        TEXT,
    diagnosed_date          TEXT,
    raw_json                TEXT,
    last_synced_at          TEXT NOT NULL,
    sync_run_id              INTEGER NOT NULL,
    UNIQUE(patient_internal_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_diagnoses_patient ON diagnoses(patient_internal_id);

CREATE TABLE IF NOT EXISTS coverage (
    coverage_pk           INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_internal_id   INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    source_id              INTEGER NOT NULL,
    payer_code              TEXT,
    payer_type              TEXT,
    payer_name               TEXT,
    effective_from            TEXT,
    effective_to              TEXT,
    raw_json                   TEXT,
    last_synced_at              TEXT NOT NULL,
    sync_run_id                  INTEGER NOT NULL,
    UNIQUE(patient_internal_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_coverage_patient ON coverage(patient_internal_id);

CREATE TABLE IF NOT EXISTS notes (
    note_pk                INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_internal_id    INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    source_id                INTEGER NOT NULL,
    note_type                 TEXT,
    effective_date             TEXT,
    is_current                  INTEGER NOT NULL DEFAULT 1,
    note_format_guess          TEXT,
    raw_text                    TEXT NOT NULL,
    raw_json                     TEXT,
    last_synced_at                TEXT NOT NULL,
    sync_run_id                    INTEGER NOT NULL,
    UNIQUE(patient_internal_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_notes_patient ON notes(patient_internal_id);

CREATE TABLE IF NOT EXISTS assessments (
    assessment_pk           INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_internal_id     INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    source_id                 INTEGER NOT NULL,
    assessment_date            TEXT,
    status                      TEXT,
    is_current                   INTEGER NOT NULL DEFAULT 1,
    raw_json                    TEXT,
    raw_text                     TEXT,
    last_synced_at                TEXT NOT NULL,
    sync_run_id                    INTEGER NOT NULL,
    UNIQUE(patient_internal_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_assessments_patient ON assessments(patient_internal_id);

CREATE TABLE IF NOT EXISTS wound_extractions (
    extraction_pk            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_internal_id      INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    source_table               TEXT NOT NULL CHECK (source_table IN ('notes','assessments')),
    source_pk                   INTEGER NOT NULL,
    source_date                  TEXT,
    wound_index_in_source          INTEGER NOT NULL DEFAULT 0,
    is_primary                      INTEGER NOT NULL DEFAULT 0,
    location                          TEXT,
    wound_type                         TEXT,
    stage                               TEXT,
    length_cm                            REAL,
    width_cm                              REAL,
    depth_cm                               REAL,
    drainage_amount                         TEXT,
    location_status                          TEXT NOT NULL DEFAULT 'unknown_missing',
    wound_type_status                         TEXT NOT NULL DEFAULT 'unknown_missing',
    stage_status                               TEXT NOT NULL DEFAULT 'unknown_missing',
    length_cm_status                            TEXT NOT NULL DEFAULT 'unknown_missing',
    width_cm_status                              TEXT NOT NULL DEFAULT 'unknown_missing',
    depth_cm_status                               TEXT NOT NULL DEFAULT 'unknown_missing',
    drainage_amount_status                         TEXT NOT NULL DEFAULT 'unknown_missing',
    note_format                                     TEXT,
    secondary_wounds_json                            TEXT,
    parser_version                                    TEXT NOT NULL,
    parsed_at                                          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wound_extractions_patient ON wound_extractions(patient_internal_id);

CREATE TABLE IF NOT EXISTS unknown_flags (
    flag_pk                 INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_internal_id     INTEGER NOT NULL REFERENCES patients(patient_internal_id),
    flag_type                 TEXT NOT NULL,
    severity                   TEXT NOT NULL DEFAULT 'medium',
    source_table                TEXT,
    source_pk                    INTEGER,
    field_name                    TEXT,
    detail                         TEXT,
    created_at                      TEXT NOT NULL,
    sync_run_id                      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_unknown_flags_patient ON unknown_flags(patient_internal_id);

CREATE TABLE IF NOT EXISTS eligibility (
    patient_internal_id       INTEGER PRIMARY KEY REFERENCES patients(patient_internal_id),
    patient_id                  TEXT NOT NULL,
    facility_id                  INTEGER NOT NULL,
    primary_wound_type             TEXT,
    primary_wound_stage             TEXT,
    primary_wound_location            TEXT,
    length_cm                          REAL,
    width_cm                            REAL,
    depth_cm                             REAL,
    drainage_amount                       TEXT,
    primary_wound_source_table             TEXT,
    primary_wound_source_pk                 INTEGER,
    has_active_medicare_b                     INTEGER NOT NULL DEFAULT 0,
    other_active_payers_json                   TEXT,
    routing_decision                              TEXT NOT NULL,
    routing_reason                                  TEXT NOT NULL,
    unknown_risk_score                                INTEGER NOT NULL DEFAULT 0,
    unknown_risk_tier                                  TEXT NOT NULL DEFAULT 'green',
    unknown_flag_count                                  INTEGER NOT NULL DEFAULT 0,
    note_assessment_conflict                              INTEGER NOT NULL DEFAULT 0,
    multiple_eligible_wounds                               INTEGER NOT NULL DEFAULT 0,
    envive_narrative_only                                   INTEGER NOT NULL DEFAULT 0,
    override_decision                                         TEXT,
    override_justification                                     TEXT,
    computed_at                                                   TEXT NOT NULL,
    sync_run_id                                                    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eligibility_routing ON eligibility(routing_decision);
CREATE INDEX IF NOT EXISTS idx_eligibility_facility ON eligibility(facility_id);
CREATE INDEX IF NOT EXISTS idx_eligibility_risk_score ON eligibility(unknown_risk_score DESC);
CREATE INDEX IF NOT EXISTS idx_eligibility_risk_tier ON eligibility(unknown_risk_tier);
CREATE INDEX IF NOT EXISTS idx_eligibility_facility_decision ON eligibility(facility_id, routing_decision);

CREATE TABLE IF NOT EXISTS sync_runs (
    sync_run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type              TEXT NOT NULL,
    started_at               TEXT NOT NULL,
    finished_at                TEXT,
    status                       TEXT NOT NULL DEFAULT 'running',
    since_param                   TEXT,
    notes                           TEXT
);

CREATE TABLE IF NOT EXISTS facility_sync_status (
    status_pk             INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id              INTEGER NOT NULL REFERENCES sync_runs(sync_run_id),
    facility_id                INTEGER NOT NULL,
    endpoint                     TEXT NOT NULL,
    status                         TEXT NOT NULL DEFAULT 'pending',
    records_fetched                  INTEGER NOT NULL DEFAULT 0,
    error_detail                       TEXT,
    started_at                           TEXT,
    finished_at                            TEXT
);

CREATE TABLE IF NOT EXISTS sync_watermarks (
    facility_id     INTEGER NOT NULL,
    endpoint          TEXT NOT NULL,
    last_success_at     TEXT,
    PRIMARY KEY (facility_id, endpoint)
);

CREATE TABLE IF NOT EXISTS extraction_cache (
    content_hash      TEXT PRIMARY KEY,
    extracted_json      TEXT NOT NULL,
    parser_version        TEXT NOT NULL,
    created_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_insights (
    patient_internal_id       INTEGER PRIMARY KEY REFERENCES patients(patient_internal_id),
    model_suggestion              TEXT,
    model_probability               REAL,
    rule_agrees                      INTEGER,
    feature_importances_json           TEXT,
    computed_at                         TEXT NOT NULL
);
