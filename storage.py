"""
storage.py — Storage layer for the ABI wound-care eligibility pipeline.

PostgreSQL backend via psycopg2. Schema is in schema.sql.
Set DATABASE_URL in your environment (or .env) to configure the connection.

Design notes
------------
- Every "raw" table (patients, diagnoses, coverage, notes, assessments)
  keeps the full original API payload in a `raw_json` column. Even if a
  parser bug loses information, you can always re-derive from raw_json
  without re-hitting the API.
- All writes are idempotent upserts keyed on natural identifiers (source_id
  from the API where available), so re-running ingestion never creates
  duplicate rows.
- patient_internal_id is the join key everywhere except where the PCC API
  itself requires the string patient_id (diagnoses, coverage lookups).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/abi_pipeline")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(database_url: str | None = None) -> psycopg2.extensions.connection:
    url = database_url or DEFAULT_DATABASE_URL
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db(database_url: str | None = None, schema_path: Path = SCHEMA_PATH) -> None:
    """Create all tables if they don't already exist. Safe to call every run."""
    conn = get_connection(database_url)
    try:
        sql = schema_path.read_text()
        cur = conn.cursor()
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_session(database_url: str | None = None):
    """Context manager that commits on success and rolls back on exception."""
    conn = get_connection(database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# SYNC RUN TRACKING
# ============================================================

def start_sync_run(conn, sync_type: str, since_param: Optional[str] = None) -> int:
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sync_runs (sync_type, started_at, status, since_param)
           VALUES (%s, %s, 'running', %s)
           RETURNING sync_run_id""",
        (sync_type, now_iso(), since_param),
    )
    return cur.fetchone()["sync_run_id"]


def finish_sync_run(conn, sync_run_id: int, status: str, notes: str = "") -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE sync_runs SET finished_at = %s, status = %s, notes = %s WHERE sync_run_id = %s",
        (now_iso(), status, notes, sync_run_id),
    )


def upsert_facility_sync_status(
    conn,
    sync_run_id: int,
    facility_id: int,
    endpoint: str,
    status: str,
    records_fetched: int = 0,
    error_detail: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """SELECT status_pk FROM facility_sync_status
           WHERE sync_run_id = %s AND facility_id = %s AND endpoint = %s""",
        (sync_run_id, facility_id, endpoint),
    )
    existing = cur.fetchone()
    if existing:
        cur.execute(
            """UPDATE facility_sync_status
               SET status = %s, records_fetched = %s, error_detail = %s, finished_at = %s
               WHERE status_pk = %s""",
            (status, records_fetched, error_detail, finished_at or now_iso(), existing["status_pk"]),
        )
    else:
        cur.execute(
            """INSERT INTO facility_sync_status
               (sync_run_id, facility_id, endpoint, status, records_fetched,
                error_detail, started_at, finished_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (sync_run_id, facility_id, endpoint, status, records_fetched,
             error_detail, started_at or now_iso(), finished_at),
        )


# ============================================================
# PATIENTS
# ============================================================

def upsert_patient(
    conn,
    *,
    patient_internal_id: int,
    patient_id: str,
    facility_id: int,
    first_name: Optional[str],
    last_name: Optional[str],
    birth_date: Optional[str],
    gender: Optional[str],
    primary_payer_code: Optional[str],
    is_new_admission: bool,
    last_modified_at: Optional[str],
    raw_record: dict,
    sync_run_id: int,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO patients
               (patient_internal_id, patient_id, facility_id, first_name, last_name,
                birth_date, gender, primary_payer_code, is_new_admission, last_modified_at,
                raw_json, last_synced_at, sync_run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT(patient_internal_id) DO UPDATE SET
               patient_id        = EXCLUDED.patient_id,
               facility_id       = EXCLUDED.facility_id,
               first_name        = EXCLUDED.first_name,
               last_name         = EXCLUDED.last_name,
               birth_date        = EXCLUDED.birth_date,
               gender            = EXCLUDED.gender,
               primary_payer_code = EXCLUDED.primary_payer_code,
               is_new_admission  = EXCLUDED.is_new_admission,
               last_modified_at  = EXCLUDED.last_modified_at,
               raw_json          = EXCLUDED.raw_json,
               last_synced_at    = EXCLUDED.last_synced_at,
               sync_run_id       = EXCLUDED.sync_run_id""",
        (patient_internal_id, patient_id, facility_id, first_name, last_name,
         birth_date, gender, primary_payer_code, is_new_admission, last_modified_at,
         json.dumps(raw_record), now_iso(), sync_run_id),
    )


# ============================================================
# DIAGNOSES
# ============================================================

def insert_diagnosis(
    conn,
    *,
    patient_internal_id: int,
    source_id: Optional[int],
    icd10_code: Optional[str],
    icd10_description: Optional[str],
    clinical_status: Optional[str],
    onset_date: Optional[str],
    last_modified_at: Optional[str],
    raw_record: dict,
    sync_run_id: int,
) -> None:
    cur = conn.cursor()
    if source_id is not None:
        cur.execute("SELECT diagnosis_pk FROM diagnoses WHERE source_id = %s", (source_id,))
    else:
        cur.execute(
            """SELECT diagnosis_pk FROM diagnoses
               WHERE patient_internal_id = %s AND icd10_code = %s AND onset_date = %s""",
            (patient_internal_id, icd10_code, onset_date),
        )
    if cur.fetchone():
        return
    cur.execute(
        """INSERT INTO diagnoses
               (source_id, patient_internal_id, icd10_code, icd10_description,
                clinical_status, onset_date, last_modified_at, raw_json, last_synced_at, sync_run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (source_id, patient_internal_id, icd10_code, icd10_description,
         clinical_status, onset_date, last_modified_at,
         json.dumps(raw_record), now_iso(), sync_run_id),
    )


# ============================================================
# COVERAGE
# ============================================================

def insert_coverage(
    conn,
    *,
    patient_internal_id: int,
    source_id: Optional[int],
    payer_type: Optional[str],
    payer_name: Optional[str],
    payer_code: Optional[str],
    effective_from: Optional[str],
    effective_to: Optional[str],
    last_modified_at: Optional[str],
    raw_record: dict,
    sync_run_id: int,
) -> None:
    cur = conn.cursor()
    if source_id is not None:
        cur.execute("SELECT coverage_pk FROM coverage WHERE source_id = %s", (source_id,))
    else:
        cur.execute(
            """SELECT coverage_pk FROM coverage
               WHERE patient_internal_id = %s AND payer_type = %s AND effective_from = %s""",
            (patient_internal_id, payer_type, effective_from),
        )
    if cur.fetchone():
        return
    cur.execute(
        """INSERT INTO coverage
               (source_id, patient_internal_id, payer_type, payer_name, payer_code,
                effective_from, effective_to, last_modified_at, raw_json, last_synced_at, sync_run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (source_id, patient_internal_id, payer_type, payer_name, payer_code,
         effective_from, effective_to, last_modified_at,
         json.dumps(raw_record), now_iso(), sync_run_id),
    )


# ============================================================
# NOTES
# ============================================================

def insert_note(
    conn,
    *,
    patient_internal_id: int,
    source_id: Optional[int],
    pcc_note_id: Optional[int],
    org_id: Optional[str],
    note_type: Optional[str],
    effective_date: Optional[str],
    note_text: str,
    created_by: Optional[str],
    note_label: Optional[str],
    note_format_guess: str,
    sync_version: Optional[int],
    is_current: bool,
    raw_record: dict,
    sync_run_id: int,
) -> int:
    cur = conn.cursor()
    if source_id is not None:
        cur.execute("SELECT note_pk FROM notes WHERE source_id = %s", (source_id,))
        existing = cur.fetchone()
        if existing:
            return existing["note_pk"]
    cur.execute(
        """INSERT INTO notes
               (source_id, patient_internal_id, pcc_note_id, org_id, note_type,
                effective_date, note_text, created_by, note_label, note_format_guess,
                sync_version, is_current, raw_json, last_synced_at, sync_run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING note_pk""",
        (source_id, patient_internal_id, pcc_note_id, org_id, note_type,
         effective_date, note_text, created_by, note_label, note_format_guess,
         sync_version, is_current, json.dumps(raw_record), now_iso(), sync_run_id),
    )
    return cur.fetchone()["note_pk"]


# ============================================================
# ASSESSMENTS
# ============================================================

def insert_assessment(
    conn,
    *,
    patient_internal_id: int,
    source_id: Optional[int],
    pcc_assessment_id: Optional[int],
    org_id: Optional[str],
    assessment_type: Optional[str],
    status: Optional[str],
    assessment_date: Optional[str],
    completion_date: Optional[str],
    template_id: Optional[int],
    assessment_type_description: Optional[str],
    sync_version: Optional[int],
    is_current: bool,
    raw_record: dict,
    raw_text: Optional[str],
    sync_run_id: int,
) -> int:
    cur = conn.cursor()
    if source_id is not None:
        cur.execute("SELECT assessment_pk FROM assessments WHERE source_id = %s", (source_id,))
        existing = cur.fetchone()
        if existing:
            return existing["assessment_pk"]
    cur.execute(
        """INSERT INTO assessments
               (source_id, patient_internal_id, pcc_assessment_id, org_id, assessment_type,
                status, assessment_date, completion_date, template_id, assessment_type_description,
                sync_version, is_current, raw_json, raw_text, last_synced_at, sync_run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING assessment_pk""",
        (source_id, patient_internal_id, pcc_assessment_id, org_id, assessment_type,
         status, assessment_date, completion_date, template_id, assessment_type_description,
         sync_version, is_current, json.dumps(raw_record), raw_text, now_iso(), sync_run_id),
    )
    return cur.fetchone()["assessment_pk"]


# ============================================================
# WOUND EXTRACTIONS (parsed structured wound data)
# ============================================================

@dataclass
class WoundField:
    """A single extracted field plus its unknown-aware status."""
    value: Any = None
    status: str = "unknown_missing"  # known | unknown_missing | unknown_unparseable | unknown_conflict | unknown_out_of_range


@dataclass
class ExtractedWound:
    location: WoundField = field(default_factory=WoundField)
    wound_type: WoundField = field(default_factory=WoundField)
    stage: WoundField = field(default_factory=WoundField)
    length_cm: WoundField = field(default_factory=WoundField)
    width_cm: WoundField = field(default_factory=WoundField)
    depth_cm: WoundField = field(default_factory=WoundField)
    drainage_amount: WoundField = field(default_factory=WoundField)


def insert_wound_extraction(
    conn,
    *,
    patient_internal_id: int,
    source_table: str,
    source_pk: int,
    source_date: Optional[str],
    wound_index_in_source: int,
    is_primary: bool,
    wound: ExtractedWound,
    note_format: Optional[str],
    secondary_wounds: Optional[list[dict]],
    parser_version: str,
) -> int:
    cur = conn.cursor()
    cur.execute(
        """SELECT extraction_pk FROM wound_extractions
           WHERE source_table = %s AND source_pk = %s AND wound_index_in_source = %s""",
        (source_table, source_pk, wound_index_in_source),
    )
    existing = cur.fetchone()
    if existing:
        cur.execute(
            "DELETE FROM wound_extractions WHERE extraction_pk = %s",
            (existing["extraction_pk"],),
        )

    cur.execute(
        """INSERT INTO wound_extractions (
               patient_internal_id, source_table, source_pk, source_date,
               wound_index_in_source, is_primary,
               location, wound_type, stage, length_cm, width_cm, depth_cm, drainage_amount,
               location_status, wound_type_status, stage_status,
               length_cm_status, width_cm_status, depth_cm_status, drainage_amount_status,
               note_format, secondary_wounds_json, parser_version, parsed_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING extraction_pk""",
        (
            patient_internal_id, source_table, source_pk, source_date,
            wound_index_in_source, is_primary,
            wound.location.value, wound.wound_type.value, wound.stage.value,
            wound.length_cm.value, wound.width_cm.value, wound.depth_cm.value, wound.drainage_amount.value,
            wound.location.status, wound.wound_type.status, wound.stage.status,
            wound.length_cm.status, wound.width_cm.status, wound.depth_cm.status, wound.drainage_amount.status,
            note_format, json.dumps(secondary_wounds or []), parser_version, now_iso(),
        ),
    )
    return cur.fetchone()["extraction_pk"]


# ============================================================
# UNKNOWN FLAGS
# ============================================================

def add_unknown_flag(
    conn,
    *,
    patient_internal_id: int,
    flag_type: str,
    severity: str = "medium",
    source_table: Optional[str] = None,
    source_pk: Optional[int] = None,
    field_name: Optional[str] = None,
    detail: str = "",
    sync_run_id: int,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO unknown_flags
               (patient_internal_id, flag_type, severity, source_table, source_pk,
                field_name, detail, created_at, sync_run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (patient_internal_id, flag_type, severity, source_table, source_pk,
         field_name, detail, now_iso(), sync_run_id),
    )


def get_unknown_flags_for_patient(conn, patient_internal_id: int) -> list:
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM unknown_flags WHERE patient_internal_id = %s ORDER BY created_at",
        (patient_internal_id,),
    )
    return cur.fetchall()


# ============================================================
# ELIGIBILITY (final output row)
# ============================================================

def upsert_eligibility(conn, row: dict) -> None:
    """
    `row` should contain all eligibility columns.
    Pass JSON-serializable Python objects for *_json fields; this function
    will json.dumps them.
    """
    json_fields = ("other_active_payers_json", "coverage_unknown_flags_json")
    row = dict(row)
    for jf in json_fields:
        if jf in row and not isinstance(row[jf], str):
            row[jf] = json.dumps(row[jf])
    row["computed_at"] = now_iso()

    columns = list(row.keys())
    placeholders = ", ".join("%s" for _ in columns)
    update_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in columns if c != "patient_internal_id"
    )
    sql = f"""INSERT INTO eligibility ({", ".join(columns)})
              VALUES ({placeholders})
              ON CONFLICT(patient_internal_id) DO UPDATE SET {update_clause}"""
    cur = conn.cursor()
    cur.execute(sql, [row[c] for c in columns])


def record_override(
    conn,
    *,
    patient_internal_id: int,
    previous_decision: str,
    new_decision: str,
    justification: str,
    overridden_by: str,
) -> None:
    ts = now_iso()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO override_history
               (patient_internal_id, previous_decision, new_decision, justification,
                overridden_by, overridden_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (patient_internal_id, previous_decision, new_decision, justification, overridden_by, ts),
    )
    cur.execute(
        """UPDATE eligibility
           SET override_decision = %s, override_justification = %s,
               override_by = %s, override_at = %s
           WHERE patient_internal_id = %s""",
        (new_decision, justification, overridden_by, ts, patient_internal_id),
    )


# ============================================================
# QUERY HELPERS (used by dashboard / presentation layer)
# ============================================================

def get_dashboard_rows(conn, facility_id: Optional[int] = None) -> list:
    cur = conn.cursor()
    sql = """
        SELECT e.*, p.first_name, p.last_name, p.facility_id AS p_facility_id
        FROM eligibility e
        JOIN patients p ON p.patient_internal_id = e.patient_internal_id
    """
    params: list = []
    if facility_id is not None:
        sql += " WHERE p.facility_id = %s"
        params = [facility_id]
    sql += " ORDER BY e.unknown_risk_score DESC, e.routing_decision"
    cur.execute(sql, params)
    return cur.fetchall()


def get_patient_detail(conn, patient_internal_id: int) -> dict:
    cur = conn.cursor()

    cur.execute("SELECT * FROM patients WHERE patient_internal_id = %s", (patient_internal_id,))
    patient = cur.fetchone()

    cur.execute("SELECT * FROM eligibility WHERE patient_internal_id = %s", (patient_internal_id,))
    eligibility = cur.fetchone()

    cur.execute(
        "SELECT * FROM notes WHERE patient_internal_id = %s ORDER BY effective_date",
        (patient_internal_id,),
    )
    notes = cur.fetchall()

    cur.execute(
        "SELECT * FROM assessments WHERE patient_internal_id = %s ORDER BY assessment_date",
        (patient_internal_id,),
    )
    assessments = cur.fetchall()

    cur.execute(
        "SELECT * FROM wound_extractions WHERE patient_internal_id = %s ORDER BY source_date",
        (patient_internal_id,),
    )
    wound_extractions = cur.fetchall()

    flags = get_unknown_flags_for_patient(conn, patient_internal_id)

    cur.execute(
        "SELECT * FROM override_history WHERE patient_internal_id = %s ORDER BY overridden_at",
        (patient_internal_id,),
    )
    overrides = cur.fetchall()

    return {
        "patient": dict(patient) if patient else None,
        "eligibility": dict(eligibility) if eligibility else None,
        "notes": [dict(r) for r in notes],
        "assessments": [dict(r) for r in assessments],
        "wound_extractions": [dict(r) for r in wound_extractions],
        "unknown_flags": [dict(r) for r in flags],
        "overrides": [dict(r) for r in overrides],
    }


if __name__ == "__main__":
    init_db()
    print(f"Initialized schema at {DEFAULT_DATABASE_URL}")
