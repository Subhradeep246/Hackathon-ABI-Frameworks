"""
ingest.py — Data ingestion client for the ABI wound-care eligibility pipeline.

Fetches patients, diagnoses, coverage, notes, and assessments from the mock
PCC API and stores them in PostgreSQL via storage.py.

Usage:
    python ingest.py                                        # full sync, all facilities
    python ingest.py --since 2026-06-01T00:00:00            # incremental
    python ingest.py --facility 101                         # single facility
    python ingest.py --facility 101 --since 2026-06-01T00:00:00
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import time
from typing import Optional

import requests
from dotenv import load_dotenv

import storage

load_dotenv()

BASE_URL = os.getenv("API_BASE_URL", "https://hackathon.prod.pulsefoundry.ai")
DEFAULT_FACILITY_IDS = [101, 102, 103]
MAX_RETRIES = 5

log = logging.getLogger(__name__)


# ============================================================
# HTTP CLIENT
# ============================================================

def fetch_with_retry(
    session: requests.Session,
    url: str,
    params: dict | None = None,
    max_retries: int = MAX_RETRIES,
) -> list:
    """GET a URL, returning parsed JSON. Retries on 429 and 5xx with backoff."""
    backoff = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=10)

            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 3)) + random.uniform(0, 1)
                log.warning("429 rate-limited %s (attempt %d/%d) — retrying in %.1fs",
                            url, attempt, max_retries, wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = backoff + random.uniform(-0.5, 0.5)
                log.warning("%d server error on %s (attempt %d/%d) — retrying in %.1fs",
                            resp.status_code, url, attempt, max_retries, wait)
                time.sleep(wait)
                backoff *= 2
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.Timeout:
            wait = backoff + random.uniform(-0.5, 0.5)
            log.warning("Timeout on %s (attempt %d/%d) — retrying in %.1fs",
                        url, attempt, max_retries, wait)
            time.sleep(wait)
            backoff *= 2

    raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts")


# ============================================================
# PER-ENDPOINT STORAGE HELPERS
# ============================================================

def _store_diagnoses(conn, patient_internal_id: int, records: list, sync_run_id: int) -> None:
    for r in records:
        storage.insert_diagnosis(
            conn,
            patient_internal_id=patient_internal_id,
            source_id=r.get("id"),
            icd10_code=r.get("icd10_code"),
            icd10_description=r.get("icd10_description"),
            clinical_status=r.get("clinical_status"),
            onset_date=r.get("onset_date"),
            last_modified_at=r.get("last_modified_at"),
            raw_record=r,
            sync_run_id=sync_run_id,
        )


def _store_coverage(conn, patient_internal_id: int, records: list, sync_run_id: int) -> None:
    for r in records:
        storage.insert_coverage(
            conn,
            patient_internal_id=patient_internal_id,
            source_id=r.get("id"),
            payer_type=r.get("payer_type"),
            payer_name=r.get("payer_name"),
            payer_code=r.get("payer_code"),
            effective_from=r.get("effective_from"),
            effective_to=r.get("effective_to"),
            last_modified_at=r.get("last_modified_at"),
            raw_record=r,
            sync_run_id=sync_run_id,
        )


def _store_notes(conn, patient_internal_id: int, records: list, sync_run_id: int) -> None:
    for r in records:
        storage.insert_note(
            conn,
            patient_internal_id=patient_internal_id,
            source_id=r.get("id"),
            pcc_note_id=r.get("pcc_note_id"),
            org_id=r.get("org_id"),
            note_type=r.get("note_type"),
            effective_date=r.get("effective_date"),
            note_text=r.get("note_text") or "",
            created_by=r.get("created_by"),
            note_label=r.get("note_label"),
            note_format_guess="unknown",  # downstream NLP parser sets this
            sync_version=r.get("sync_version"),
            is_current=bool(r.get("is_current", True)),
            raw_record=r,
            sync_run_id=sync_run_id,
        )


def _store_assessments(conn, patient_internal_id: int, records: list, sync_run_id: int) -> None:
    for r in records:
        storage.insert_assessment(
            conn,
            patient_internal_id=patient_internal_id,
            source_id=r.get("id"),
            pcc_assessment_id=r.get("pcc_assessment_id"),
            org_id=r.get("org_id"),
            assessment_type=r.get("assessment_type"),
            status=r.get("status"),
            assessment_date=r.get("assessment_date"),
            completion_date=r.get("completion_date"),
            template_id=r.get("template_id"),
            assessment_type_description=r.get("assessment_type_description"),
            sync_version=r.get("sync_version"),
            is_current=bool(r.get("is_current", True)),
            raw_record=r,
            raw_text=None,
            sync_run_id=sync_run_id,
        )


# ============================================================
# INGESTION ORCHESTRATION
# ============================================================

def _fetch_and_store(
    session: requests.Session,
    conn,
    url: str,
    params: dict,
    store_fn,
    facility_id: int,
    sync_run_id: int,
    endpoint_name: str,
) -> int:
    """Fetch one endpoint, store results, update facility_sync_status. Returns record count."""
    started = storage.now_iso()
    records = fetch_with_retry(session, url, params)
    store_fn(records)
    storage.upsert_facility_sync_status(
        conn, sync_run_id, facility_id, endpoint_name, "complete",
        records_fetched=len(records), started_at=started, finished_at=storage.now_iso(),
    )
    return len(records)


def ingest_facility(
    session: requests.Session,
    database_url: Optional[str],
    facility_id: int,
    sync_run_id: int,
    since: Optional[str],
) -> None:
    """Fetch and store all data for one facility."""
    log.info("Facility %d — fetching patients …", facility_id)

    # --- patients (own transaction so per-patient loop has a valid list even if one endpoint fails) ---
    patient_params: dict = {"facility_id": facility_id}
    if since:
        patient_params["since"] = since

    started = storage.now_iso()
    try:
        patients = fetch_with_retry(session, f"{BASE_URL}/pcc/patients", patient_params)
    except Exception as exc:
        log.error("Facility %d patients fetch failed: %s", facility_id, exc)
        with storage.db_session(database_url) as conn:
            storage.upsert_facility_sync_status(
                conn, sync_run_id, facility_id, "patients", "failed",
                error_detail=str(exc), started_at=started,
            )
        return

    with storage.db_session(database_url) as conn:
        for p in patients:
            storage.upsert_patient(
                conn,
                patient_internal_id=p["id"],
                patient_id=p["patient_id"],
                facility_id=p["facility_id"],
                first_name=p.get("first_name"),
                last_name=p.get("last_name"),
                birth_date=p.get("birth_date"),
                gender=p.get("gender"),
                primary_payer_code=p.get("primary_payer_code"),
                is_new_admission=bool(p.get("is_new_admission", False)),
                last_modified_at=p.get("last_modified_at"),
                raw_record=p,
                sync_run_id=sync_run_id,
            )
        storage.upsert_facility_sync_status(
            conn, sync_run_id, facility_id, "patients", "complete",
            records_fetched=len(patients), started_at=started, finished_at=storage.now_iso(),
        )
    log.info("Facility %d — stored %d patients", facility_id, len(patients))

    # --- per-patient endpoints ---
    for p in patients:
        patient_internal_id: int = p["id"]
        patient_id: str = p["patient_id"]

        note_params: dict = {"patient_id": patient_internal_id}
        assess_params: dict = {"patient_id": patient_internal_id}
        if since:
            note_params["since"] = since
            assess_params["since"] = since

        per_endpoint = [
            ("diagnoses",   f"{BASE_URL}/pcc/diagnoses",   {"patient_id": patient_id},    _store_diagnoses),
            ("coverage",    f"{BASE_URL}/pcc/coverage",    {"patient_id": patient_id},    _store_coverage),
            ("notes",       f"{BASE_URL}/pcc/notes",       note_params,                   _store_notes),
            ("assessments", f"{BASE_URL}/pcc/assessments", assess_params,                 _store_assessments),
        ]

        for endpoint_name, url, params, store_fn in per_endpoint:
            ep_started = storage.now_iso()
            try:
                with storage.db_session(database_url) as conn:
                    count = _fetch_and_store(
                        session, conn, url, params,
                        lambda records, fn=store_fn: fn(conn, patient_internal_id, records, sync_run_id),
                        facility_id, sync_run_id, endpoint_name,
                    )
                log.debug(
                    "Facility %d patient %s %s — %d records",
                    facility_id, patient_id, endpoint_name, count,
                )
            except Exception as exc:
                log.error(
                    "Facility %d patient %s %s failed: %s",
                    facility_id, patient_id, endpoint_name, exc,
                )
                with storage.db_session(database_url) as conn:
                    storage.upsert_facility_sync_status(
                        conn, sync_run_id, facility_id, endpoint_name, "failed",
                        error_detail=f"patient {patient_id}: {exc}",
                        started_at=ep_started,
                    )


def run_sync(
    facility_ids: list[int] = DEFAULT_FACILITY_IDS,
    since: Optional[str] = None,
    database_url: Optional[str] = None,
) -> None:
    sync_type = "incremental" if since else "full"
    log.info("Starting %s sync (facilities=%s, since=%s)", sync_type, facility_ids, since)

    storage.init_db(database_url)

    with storage.db_session(database_url) as conn:
        sync_run_id = storage.start_sync_run(conn, sync_type, since)
    log.info("sync_run_id=%d", sync_run_id)

    session = requests.Session()
    session.headers["Accept"] = "application/json"

    failed = False
    try:
        for facility_id in facility_ids:
            ingest_facility(session, database_url, facility_id, sync_run_id, since)
    except Exception as exc:
        log.error("Unexpected error during sync: %s", exc)
        failed = True

    final_status = "failed" if failed else "complete"
    with storage.db_session(database_url) as conn:
        storage.finish_sync_run(conn, sync_run_id, final_status)
    log.info("Sync finished — status=%s sync_run_id=%d", final_status, sync_run_id)


# ============================================================
# CLI
# ============================================================

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Ingest PCC API data into PostgreSQL")
    parser.add_argument(
        "--since",
        metavar="ISO_TIMESTAMP",
        help="Only fetch records modified at or after this timestamp (e.g. 2026-06-01T00:00:00)",
    )
    parser.add_argument(
        "--facility",
        type=int,
        metavar="ID",
        help="Restrict ingestion to a single facility ID (101, 102, or 103)",
    )
    parser.add_argument(
        "--db",
        metavar="DATABASE_URL",
        help="PostgreSQL connection string (overrides DATABASE_URL env var)",
    )
    args = parser.parse_args()

    facility_ids = [args.facility] if args.facility else DEFAULT_FACILITY_IDS
    run_sync(
        facility_ids=facility_ids,
        since=args.since,
        database_url=args.db or None,
    )


if __name__ == "__main__":
    main()
