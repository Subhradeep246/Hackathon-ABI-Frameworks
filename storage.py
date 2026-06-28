"""
storage.py — Storage layer for the ABI wound-care eligibility pipeline.

This module owns the SQLite database: schema creation, connection handling,
and write helpers for every table. Ingestion code, parsers, and the
eligibility engine all go through these functions rather than writing
raw SQL inline, so the storage contract stays in one place.

Design notes
------------
- Every "raw" table (patients, diagnoses, coverage, notes, assessments)
  keeps the full original API payload in a `raw_json` column. Even if a
  parser bug loses information, you can always re-derive from raw_json
  without re-hitting the API.
- All writes are idempotent upserts keyed on natural identifiers, so
  re-running ingestion (e.g. retrying after a partial sync) never creates
  duplicate rows.
- patient_internal_id is the join key everywhere except where the PCC API
  itself requires the string patient_id (diagnoses, coverage lookups).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).parent / "abi_pipeline.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH, schema_path: Path = SCHEMA_PATH) -> None:
    """Create all tables if they don't already exist. Safe to call every run."""
    conn = get_connection(db_path)
    try:
        with open(schema_path, "r") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_session(db_path: Path | str = DEFAULT_DB_PATH):
    """Context manager that commits on success and rolls back on exception."""
    conn = get_connection(db_path)
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

def start_sync_run(conn: sqlite3.Connection, sync_type: str, since_param: Optional[str] = None) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs (sync_type, started_at, status, since_param) VALUES (?, ?, 'running', ?)",
        (sync_type, now_iso(), since_param),
    )
    return cur.lastrowid


def finish_sync_run(conn: sqlite3.Connection, sync_run_id: int, status: str, notes: str = "") -> None:
    conn.execute(
        "UPDATE sync_runs SET finished_at = ?, status = ?, notes = ? WHERE sync_run_id = ?",
        (now_iso(), status, notes, sync_run_id),
    )


def upsert_facility_sync_status(
    conn: sqlite3.Connection,
    sync_run_id: int,
    facility_id: int,
    endpoint: str,
    status: str,
    records_fetched: int = 0,
    error_detail: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> None:
    existing = conn.execute(
        "SELECT status_pk FROM facility_sync_status WHERE sync_run_id = ? AND facility_id = ? AND endpoint = ?",
        (sync_run_id, facility_id, endpoint),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE facility_sync_status
               SET status = ?, records_fetched = ?, error_detail = ?, finished_at = ?
               WHERE status_pk = ?""",
            (status, records_fetched, error_detail, finished_at or now_iso(), existing["status_pk"]),
        )
    else:
        conn.execute(
            """INSERT INTO facility_sync_status
               (sync_run_id, facility_id, endpoint, status, records_fetched, error_detail, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sync_run_id, facility_id, endpoint, status, records_fetched, error_detail,
             started_at or now_iso(), finished_at),
        )


# ============================================================
# PATIENTS
# ============================================================

def upsert_patient(
    conn: sqlite3.Connection,
    *,
    patient_internal_id: int,
    patient_id: str,
    facility_id: int,
    first_name: Optional[str],
    last_name: Optional[str],
    date_of_birth: Optional[str],
    raw_record: dict,
    sync_run_id: int,
) -> None:
    conn.execute(
        """INSERT INTO patients
               (patient_internal_id, patient_id, facility_id, first_name, last_name,
                date_of_birth, raw_json, last_synced_at, sync_run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(patient_internal_id) DO UPDATE SET
               patient_id = excluded.patient_id,
               facility_id = excluded.facility_id,
               first_name = excluded.first_name,
               last_name = excluded.last_name,
               date_of_birth = excluded.date_of_birth,
               raw_json = excluded.raw_json,
               last_synced_at = excluded.last_synced_at,
               sync_run_id = excluded.sync_run_id
        """,
        (patient_internal_id, patient_id, facility_id, first_name, last_name,
         date_of_birth, json.dumps(raw_record), now_iso(), sync_run_id),
    )


# ============================================================
# DIAGNOSES
# ============================================================

def insert_diagnosis(
    conn: sqlite3.Connection,
    *,
    patient_internal_id: int,
    icd10_code: Optional[str],
    description: Optional[str],
    diagnosed_date: Optional[str],
    raw_record: dict,
    sync_run_id: int,
) -> None:
    # Diagnoses don't have a clean natural key in the source data, so we
    # de-dupe on (patient, code, date) to keep re-syncs idempotent.
    existing = conn.execute(
        """SELECT diagnosis_pk FROM diagnoses
           WHERE patient_internal_id = ? AND icd10_code = ? AND diagnosed_date = ?""",
        (patient_internal_id, icd10_code, diagnosed_date),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """INSERT INTO diagnoses
               (patient_internal_id, icd10_code, description, diagnosed_date,
                raw_json, last_synced_at, sync_run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (patient_internal_id, icd10_code, description, diagnosed_date,
         json.dumps(raw_record), now_iso(), sync_run_id),
    )


# ============================================================
# COVERAGE
# ============================================================

def insert_coverage(
    conn: sqlite3.Connection,
    *,
    patient_internal_id: int,
    payer_type: Optional[str],
    payer_name: Optional[str],
    effective_from: Optional[str],
    effective_to: Optional[str],
    raw_record: dict,
    sync_run_id: int,
) -> None:
    existing = conn.execute(
        """SELECT coverage_pk FROM coverage
           WHERE patient_internal_id = ? AND payer_type = ? AND effective_from = ?""",
        (patient_internal_id, payer_type, effective_from),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """INSERT INTO coverage
               (patient_internal_id, payer_type, payer_name, effective_from, effective_to,
                raw_json, last_synced_at, sync_run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_internal_id, payer_type, payer_name, effective_from, effective_to,
         json.dumps(raw_record), now_iso(), sync_run_id),
    )


# ============================================================
# NOTES
# ============================================================

def insert_note(
    conn: sqlite3.Connection,
    *,
    patient_internal_id: int,
    note_source_id: Optional[str],
    note_date: Optional[str],
    note_format_guess: str,
    raw_text: str,
    raw_record: dict,
    sync_run_id: int,
) -> int:
    if note_source_id is not None:
        existing = conn.execute(
            "SELECT note_pk FROM notes WHERE patient_internal_id = ? AND note_source_id = ?",
            (patient_internal_id, note_source_id),
        ).fetchone()
        if existing:
            return existing["note_pk"]
    cur = conn.execute(
        """INSERT INTO notes
               (patient_internal_id, note_source_id, note_date, note_format_guess,
                raw_text, raw_json, last_synced_at, sync_run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_internal_id, note_source_id, note_date, note_format_guess,
         raw_text, json.dumps(raw_record), now_iso(), sync_run_id),
    )
    return cur.lastrowid


# ============================================================
# ASSESSMENTS
# ============================================================

def insert_assessment(
    conn: sqlite3.Connection,
    *,
    patient_internal_id: int,
    assessment_source_id: Optional[str],
    assessment_date: Optional[str],
    raw_record: dict,
    raw_text: Optional[str],
    sync_run_id: int,
) -> int:
    if assessment_source_id is not None:
        existing = conn.execute(
            "SELECT assessment_pk FROM assessments WHERE patient_internal_id = ? AND assessment_source_id = ?",
            (patient_internal_id, assessment_source_id),
        ).fetchone()
        if existing:
            return existing["assessment_pk"]
    cur = conn.execute(
        """INSERT INTO assessments
               (patient_internal_id, assessment_source_id, assessment_date, raw_json, raw_text,
                last_synced_at, sync_run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (patient_internal_id, assessment_source_id, assessment_date,
         json.dumps(raw_record), raw_text, now_iso(), sync_run_id),
    )
    return cur.lastrowid


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
    conn: sqlite3.Connection,
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
    existing = conn.execute(
        """SELECT extraction_pk FROM wound_extractions
           WHERE source_table = ? AND source_pk = ? AND wound_index_in_source = ?""",
        (source_table, source_pk, wound_index_in_source),
    ).fetchone()
    if existing:
        extraction_pk = existing["extraction_pk"]
        conn.execute("DELETE FROM wound_extractions WHERE extraction_pk = ?", (extraction_pk,))

    cur = conn.execute(
        """INSERT INTO wound_extractions (
               patient_internal_id, source_table, source_pk, source_date,
               wound_index_in_source, is_primary,
               location, wound_type, stage, length_cm, width_cm, depth_cm, drainage_amount,
               location_status, wound_type_status, stage_status,
               length_cm_status, width_cm_status, depth_cm_status, drainage_amount_status,
               note_format, secondary_wounds_json, parser_version, parsed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            patient_internal_id, source_table, source_pk, source_date,
            wound_index_in_source, int(is_primary),
            wound.location.value, wound.wound_type.value, wound.stage.value,
            wound.length_cm.value, wound.width_cm.value, wound.depth_cm.value, wound.drainage_amount.value,
            wound.location.status, wound.wound_type.status, wound.stage.status,
            wound.length_cm.status, wound.width_cm.status, wound.depth_cm.status, wound.drainage_amount.status,
            note_format, json.dumps(secondary_wounds or []), parser_version, now_iso(),
        ),
    )
    return cur.lastrowid


# ============================================================
# UNKNOWN FLAGS
# ============================================================

def add_unknown_flag(
    conn: sqlite3.Connection,
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
    conn.execute(
        """INSERT INTO unknown_flags
               (patient_internal_id, flag_type, severity, source_table, source_pk,
                field_name, detail, created_at, sync_run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_internal_id, flag_type, severity, source_table, source_pk,
         field_name, detail, now_iso(), sync_run_id),
    )


def get_unknown_flags_for_patient(conn: sqlite3.Connection, patient_internal_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM unknown_flags WHERE patient_internal_id = ? ORDER BY created_at",
        (patient_internal_id,),
    ).fetchall()


# ============================================================
# ELIGIBILITY (final output row)
# ============================================================

def upsert_eligibility(conn: sqlite3.Connection, row: dict) -> None:
    """
    `row` should contain all eligibility columns except autoincrement concerns
    (patient_internal_id is the PK, supplied explicitly).
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
    placeholders = ", ".join("?" for _ in columns)
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "patient_internal_id")

    sql = f"""INSERT INTO eligibility ({", ".join(columns)})
              VALUES ({placeholders})
              ON CONFLICT(patient_internal_id) DO UPDATE SET {update_clause}"""
    conn.execute(sql, [row[c] for c in columns])


def record_override(
    conn: sqlite3.Connection,
    *,
    patient_internal_id: int,
    previous_decision: str,
    new_decision: str,
    justification: str,
    overridden_by: str,
) -> None:
    ts = now_iso()
    conn.execute(
        """INSERT INTO override_history
               (patient_internal_id, previous_decision, new_decision, justification, overridden_by, overridden_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (patient_internal_id, previous_decision, new_decision, justification, overridden_by, ts),
    )
    conn.execute(
        """UPDATE eligibility
           SET override_decision = ?, override_justification = ?, override_by = ?, override_at = ?
           WHERE patient_internal_id = ?""",
        (new_decision, justification, overridden_by, ts, patient_internal_id),
    )


# ============================================================
# QUERY HELPERS (used by dashboard / presentation layer)
# ============================================================

def get_dashboard_rows(conn: sqlite3.Connection, facility_id: Optional[int] = None) -> list[sqlite3.Row]:
    sql = """
        SELECT e.*, p.first_name, p.last_name, p.facility_id as p_facility_id
        FROM eligibility e
        JOIN patients p ON p.patient_internal_id = e.patient_internal_id
    """
    params: tuple = ()
    if facility_id is not None:
        sql += " WHERE p.facility_id = ?"
        params = (facility_id,)
    sql += " ORDER BY e.unknown_risk_score DESC, e.routing_decision"
    return conn.execute(sql, params).fetchall()


def get_patient_detail(conn: sqlite3.Connection, patient_internal_id: int) -> dict:
    patient = conn.execute(
        "SELECT * FROM patients WHERE patient_internal_id = ?", (patient_internal_id,)
    ).fetchone()
    eligibility = conn.execute(
        "SELECT * FROM eligibility WHERE patient_internal_id = ?", (patient_internal_id,)
    ).fetchone()
    notes = conn.execute(
        "SELECT * FROM notes WHERE patient_internal_id = ? ORDER BY note_date", (patient_internal_id,)
    ).fetchall()
    assessments = conn.execute(
        "SELECT * FROM assessments WHERE patient_internal_id = ? ORDER BY assessment_date", (patient_internal_id,)
    ).fetchall()
    wound_extractions = conn.execute(
        "SELECT * FROM wound_extractions WHERE patient_internal_id = ? ORDER BY source_date",
        (patient_internal_id,),
    ).fetchall()
    flags = get_unknown_flags_for_patient(conn, patient_internal_id)
    overrides = conn.execute(
        "SELECT * FROM override_history WHERE patient_internal_id = ? ORDER BY overridden_at",
        (patient_internal_id,),
    ).fetchall()
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
    print(f"Initialized schema at {DEFAULT_DB_PATH}")