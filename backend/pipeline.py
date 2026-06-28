"""Pipeline orchestration: sync, extract, decide."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from backend.db.database import SessionLocal, init_db
from backend.eligibility.rules import decide
from backend.extraction.parser import (
    PARSER_VERSION,
    detect_note_format,
    extract_from_assessment_json,
    extract_from_text,
    now_iso,
)
from backend.ingestion.client import (
    APIError,
    bind_api_semaphore,
    get_assessments,
    get_client_stats,
    get_coverage,
    get_diagnoses,
    get_notes,
    get_patients,
    reset_client_stats,
)

BATCH_COMMIT = int(os.getenv("SYNC_BATCH_SIZE", "25"))
PATIENT_RETRIES = int(os.getenv("SYNC_PATIENT_RETRIES", "3"))
PROGRESS_EVERY = int(os.getenv("SYNC_PROGRESS_EVERY", "10"))


def mark_stale_sync_runs(reason: str = "Process restart") -> int:
    """Close sync_runs left 'running' after a crash or hot reload."""
    db = SessionLocal()
    try:
        result = db.execute(
            text(
                "UPDATE sync_runs SET status='interrupted', finished_at=:f, "
                "notes=COALESCE(notes, '') || :n WHERE status='running'"
            ),
            {"f": _now(), "n": f" [{reason}]"},
        )
        db.commit()
        return result.rowcount or 0
    finally:
        db.close()


def get_facilities() -> list[int]:
    raw = os.getenv("FACILITY_IDS", "101,102,103")
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def get_incremental_since() -> str | None:
    """Earliest watermark across facilities for incremental sync."""
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT MIN(last_success_at) FROM sync_watermarks WHERE endpoint='patients'")
        ).scalar()
        return row
    finally:
        db.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_sync_run(db, sync_type: str = "full", since: str | None = None) -> int:
    db.execute(
        text(
            "INSERT INTO sync_runs (sync_type, started_at, status, since_param) "
            "VALUES (:t, :s, 'running', :since)"
        ),
        {"t": sync_type, "s": _now(), "since": since},
    )
    db.commit()
    return db.execute(text("SELECT last_insert_rowid()")).scalar()


def _finish_sync_run(db, sync_run_id: int, status: str, notes: str | None = None) -> None:
    db.execute(
        text("UPDATE sync_runs SET finished_at=:f, status=:st, notes=:n WHERE sync_run_id=:id"),
        {"f": _now(), "st": status, "id": sync_run_id, "n": notes},
    )
    db.commit()


def _track_endpoint(db, sync_run_id: int, facility_id: int, endpoint: str, status: str, count: int, err: str | None = None):
    db.execute(
        text(
            "INSERT INTO facility_sync_status "
            "(sync_run_id, facility_id, endpoint, status, records_fetched, error_detail, started_at, finished_at) "
            "VALUES (:sr, :fid, :ep, :st, :cnt, :err, :s, :f)"
        ),
        {"sr": sync_run_id, "fid": facility_id, "ep": endpoint, "st": status, "cnt": count, "err": err, "s": _now(), "f": _now()},
    )
    db.commit()


def _upsert_patient(db, p: dict, sync_run_id: int) -> None:
    db.execute(
        text(
            """
            INSERT INTO patients (
                patient_internal_id, patient_id, facility_id, first_name, last_name,
                date_of_birth, gender, primary_payer_code, last_modified_at, is_new_admission,
                raw_json, last_synced_at, sync_run_id
            ) VALUES (
                :id, :pid, :fid, :fn, :ln, :dob, :gender, :ppc, :lma, :ina,
                :raw, :ls, :sr
            )
            ON CONFLICT(patient_internal_id) DO UPDATE SET
                patient_id=excluded.patient_id, facility_id=excluded.facility_id,
                first_name=excluded.first_name, last_name=excluded.last_name,
                date_of_birth=excluded.date_of_birth, gender=excluded.gender,
                primary_payer_code=excluded.primary_payer_code, last_modified_at=excluded.last_modified_at,
                is_new_admission=excluded.is_new_admission, raw_json=excluded.raw_json,
                last_synced_at=excluded.last_synced_at, sync_run_id=excluded.sync_run_id
            """
        ),
        {
            "id": p["id"],
            "pid": p["patient_id"],
            "fid": p["facility_id"],
            "fn": p.get("first_name"),
            "ln": p.get("last_name"),
            "dob": p.get("birth_date"),
            "gender": p.get("gender"),
            "ppc": p.get("primary_payer_code"),
            "lma": p.get("last_modified_at"),
            "ina": 1 if p.get("is_new_admission") else 0,
            "raw": json.dumps(p),
            "ls": _now(),
            "sr": sync_run_id,
        },
    )


async def _fetch_patient_api_data(client, patient: dict):
    pid = patient["patient_id"]
    internal_id = patient["id"]
    diagnoses, coverage, notes, assessments = await asyncio.gather(
        get_diagnoses(client, pid),
        get_coverage(client, pid),
        get_notes(client, internal_id),
        get_assessments(client, internal_id),
    )
    return diagnoses, coverage, notes, assessments


def _save_patient_details(patient: dict, sync_run_id: int, data: tuple) -> None:
    diagnoses, coverage, notes, assessments = data
    internal_id = patient["id"]
    db = SessionLocal()
    try:
        for d in diagnoses:
            db.execute(
                text(
                    """
                    INSERT INTO diagnoses (
                        patient_internal_id, source_id, icd10_code, description,
                        clinical_status, diagnosed_date, raw_json, last_synced_at, sync_run_id
                    ) VALUES (:pi, :sid, :code, :desc, :cs, :od, :raw, :ls, :sr)
                    ON CONFLICT(patient_internal_id, source_id) DO UPDATE SET
                        icd10_code=excluded.icd10_code, description=excluded.description,
                        clinical_status=excluded.clinical_status, diagnosed_date=excluded.diagnosed_date,
                        raw_json=excluded.raw_json, last_synced_at=excluded.last_synced_at
                    """
                ),
                {
                    "pi": internal_id,
                    "sid": d["id"],
                    "code": d.get("icd10_code"),
                    "desc": d.get("icd10_description"),
                    "cs": d.get("clinical_status"),
                    "od": d.get("onset_date"),
                    "raw": json.dumps(d),
                    "ls": _now(),
                    "sr": sync_run_id,
                },
            )

        for c in coverage:
            db.execute(
                text(
                    """
                    INSERT INTO coverage (
                        patient_internal_id, source_id, payer_code, payer_type, payer_name,
                        effective_from, effective_to, raw_json, last_synced_at, sync_run_id
                    ) VALUES (:pi, :sid, :pc, :pt, :pn, :ef, :et, :raw, :ls, :sr)
                    ON CONFLICT(patient_internal_id, source_id) DO UPDATE SET
                        payer_code=excluded.payer_code, payer_type=excluded.payer_type,
                        payer_name=excluded.payer_name, effective_from=excluded.effective_from,
                        effective_to=excluded.effective_to, raw_json=excluded.raw_json,
                        last_synced_at=excluded.last_synced_at
                    """
                ),
                {
                    "pi": internal_id,
                    "sid": c["id"],
                    "pc": c.get("payer_code"),
                    "pt": c.get("payer_type"),
                    "pn": c.get("payer_name"),
                    "ef": c.get("effective_from"),
                    "et": c.get("effective_to"),
                    "raw": json.dumps(c),
                    "ls": _now(),
                    "sr": sync_run_id,
                },
            )

        for n in notes:
            fmt = detect_note_format(n.get("note_text") or "")
            db.execute(
                text(
                    """
                    INSERT INTO notes (
                        patient_internal_id, source_id, note_type, effective_date, is_current,
                        note_format_guess, raw_text, raw_json, last_synced_at, sync_run_id
                    ) VALUES (:pi, :sid, :nt, :ed, :ic, :fmt, :txt, :raw, :ls, :sr)
                    ON CONFLICT(patient_internal_id, source_id) DO UPDATE SET
                        note_type=excluded.note_type, effective_date=excluded.effective_date,
                        is_current=excluded.is_current, note_format_guess=excluded.note_format_guess,
                        raw_text=excluded.raw_text, raw_json=excluded.raw_json,
                        last_synced_at=excluded.last_synced_at
                    """
                ),
                {
                    "pi": internal_id,
                    "sid": n["id"],
                    "nt": n.get("note_type"),
                    "ed": n.get("effective_date"),
                    "ic": 1 if n.get("is_current", True) else 0,
                    "fmt": fmt,
                    "txt": n.get("note_text") or "",
                    "raw": json.dumps(n),
                    "ls": _now(),
                    "sr": sync_run_id,
                },
            )

        for a in assessments:
            db.execute(
                text(
                    """
                    INSERT INTO assessments (
                        patient_internal_id, source_id, assessment_date, status, is_current,
                        raw_json, raw_text, last_synced_at, sync_run_id
                    ) VALUES (:pi, :sid, :ad, :st, :ic, :raw, :txt, :ls, :sr)
                    ON CONFLICT(patient_internal_id, source_id) DO UPDATE SET
                        assessment_date=excluded.assessment_date, status=excluded.status,
                        is_current=excluded.is_current, raw_json=excluded.raw_json,
                        raw_text=excluded.raw_text, last_synced_at=excluded.last_synced_at
                    """
                ),
                {
                    "pi": internal_id,
                    "sid": a["id"],
                    "ad": a.get("assessment_date"),
                    "st": a.get("status"),
                    "ic": 1 if a.get("is_current", True) else 0,
                    "raw": a.get("raw_json") if isinstance(a.get("raw_json"), str) else json.dumps(a.get("raw_json") or {}),
                    "txt": a.get("note_text"),
                    "ls": _now(),
                    "sr": sync_run_id,
                },
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _sync_patient_details(client, patient: dict, sync_run_id: int, sem: asyncio.Semaphore):
    try:
        async with sem:
            data = await _fetch_patient_api_data(client, patient)
        await asyncio.to_thread(_save_patient_details, patient, sync_run_id, data)
        return True, None
    except APIError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


async def run_sync(since: str | None = None, on_progress=None, ignore_watermarks: bool = False) -> None:
    init_db()
    reset_client_stats()
    bind_api_semaphore()
    db = SessionLocal()
    sync_run_id = _start_sync_run(db, "incremental" if since else "full", since)
    sem = asyncio.Semaphore(int(os.getenv("SYNC_CONCURRENCY", "3")))
    partial = False
    facilities = get_facilities()
    total_patients = 0
    synced_patients = 0

    def _report(msg: str, pct: int) -> None:
        if on_progress:
            on_progress(msg, pct)

    async with httpx.AsyncClient(base_url=os.getenv("PCC_BASE_URL", "https://hackathon.prod.pulsefoundry.ai"), timeout=90.0) as client:
        for fi, facility_id in enumerate(facilities, 1):
            try:
                facility_since = since
                if not ignore_watermarks and not facility_since:
                    row = db.execute(
                        text(
                            "SELECT last_success_at FROM sync_watermarks "
                            "WHERE facility_id=:fid AND endpoint='patients'"
                        ),
                        {"fid": facility_id},
                    ).scalar()
                    facility_since = row

                _report(f"Facility {facility_id}: fetching patient list…", 5 + fi * 5)
                patients = await get_patients(client, facility_id, facility_since)
                total_patients += len(patients)
                for i, p in enumerate(patients, 1):
                    _upsert_patient(db, p, sync_run_id)
                    if i % BATCH_COMMIT == 0:
                        db.commit()
                db.commit()
                _track_endpoint(db, sync_run_id, facility_id, "patients", "complete", len(patients))

                # Release main session before long async work so API handlers aren't blocked.
                db.close()
                db = None

                pending = list(patients)
                done_in_facility = 0
                facility_partial = False
                for attempt in range(PATIENT_RETRIES + 1):
                    if not pending:
                        break
                    failed: list[dict] = []
                    for start in range(0, len(pending), PROGRESS_EVERY):
                        chunk = pending[start : start + PROGRESS_EVERY]
                        tasks = [_sync_patient_details(client, p, sync_run_id, sem) for p in chunk]
                        results = await asyncio.gather(*tasks)
                        for i, (ok, _) in enumerate(results):
                            if ok:
                                done_in_facility += 1
                                synced_patients += 1
                            else:
                                failed.append(chunk[i])
                        pct = 10 + int(40 * synced_patients / max(total_patients, 1))
                        stats = get_client_stats()
                        _report(
                            f"Facility {facility_id}: {done_in_facility}/{len(patients)} patients "
                            f"({stats['rate_limited']} rate-limited, {stats['retries']} retries)…",
                            min(pct, 50),
                        )
                    if not failed:
                        pending = []
                        break
                    if attempt < PATIENT_RETRIES:
                        await asyncio.sleep(1.0 + attempt)
                        pending = failed
                    else:
                        facility_partial = True
                        partial = True
                        err = "patient detail sync failed after retries"
                        db = SessionLocal()
                        _track_endpoint(
                            db, sync_run_id, facility_id, "patient_details", "partial",
                            len(patients) - len(failed), err,
                        )

                db = SessionLocal()
                if not facility_partial:
                    _track_endpoint(db, sync_run_id, facility_id, "patient_details", "complete", len(patients))
                    db.execute(
                        text(
                            "INSERT INTO sync_watermarks (facility_id, endpoint, last_success_at) "
                            "VALUES (:fid, 'patients', :ts) "
                            "ON CONFLICT(facility_id, endpoint) DO UPDATE SET last_success_at=excluded.last_success_at"
                        ),
                        {"fid": facility_id, "ts": _now()},
                    )
                    db.commit()
            except Exception as e:
                partial = True
                if db is None:
                    db = SessionLocal()
                _track_endpoint(db, sync_run_id, facility_id, "patients", "failed", 0, str(e))

    if db is None:
        db = SessionLocal()
    stats = get_client_stats()
    notes = f"API stats: {stats['requests']} requests, {stats['rate_limited']} rate-limited, {stats['retries']} retries"
    _finish_sync_run(db, sync_run_id, "partial" if partial else "complete", notes)
    db.close()
    _report("Sync complete — parsing wounds…", 52)
    print(f"Sync complete (run_id={sync_run_id}, status={'partial' if partial else 'complete'}, {notes})")


def run_extract() -> None:
    init_db()
    db = SessionLocal()
    sync_run_id = db.execute(text("SELECT MAX(sync_run_id) FROM sync_runs")).scalar() or 1

    db.execute(text("DELETE FROM wound_extractions"))
    db.execute(text("DELETE FROM unknown_flags WHERE flag_type LIKE 'parse_%'"))
    db.commit()

    notes = db.execute(text("SELECT note_pk, patient_internal_id, raw_text, note_format_guess, effective_date FROM notes WHERE is_current=1")).mappings().all()
    for note in notes:
        wound = extract_from_text(note["raw_text"] or "")
        db.execute(
            text(
                """
                INSERT INTO wound_extractions (
                    patient_internal_id, source_table, source_pk, source_date, wound_index_in_source, is_primary,
                    location, wound_type, stage, length_cm, width_cm, depth_cm, drainage_amount,
                    location_status, wound_type_status, stage_status, length_cm_status, width_cm_status,
                    depth_cm_status, drainage_amount_status, note_format, parser_version, parsed_at
                ) VALUES (
                    :pi, 'notes', :spk, :sd, 0, 1,
                    :loc, :wt, :st, :l, :w, :d, :dr,
                    :ls, :wts, :sts, :lns, :wns, :dns, :das,
                    :fmt, :pv, :pa
                )
                """
            ),
            {
                "pi": note["patient_internal_id"], "spk": note["note_pk"], "sd": note["effective_date"],
                "loc": wound.location, "wt": wound.wound_type, "st": wound.stage,
                "l": wound.length_cm, "w": wound.width_cm, "d": wound.depth_cm, "dr": wound.drainage_amount,
                "ls": wound.location_status, "wts": wound.wound_type_status, "sts": wound.stage_status,
                "lns": wound.length_cm_status, "wns": wound.width_cm_status, "dns": wound.depth_cm_status,
                "das": wound.drainage_amount_status, "fmt": wound.note_format, "pv": PARSER_VERSION, "pa": now_iso(),
            },
        )
        if wound.note_format == "envive":
            db.execute(
                text(
                    "INSERT INTO unknown_flags (patient_internal_id, flag_type, severity, source_table, source_pk, detail, created_at, sync_run_id) "
                    "VALUES (:pi, 'envive_narrative_only', 'medium', 'notes', :spk, 'Envive-style narrative detected', :ca, :sr)"
                ),
                {"pi": note["patient_internal_id"], "spk": note["note_pk"], "ca": now_iso(), "sr": sync_run_id},
            )

    assessments = db.execute(
        text("SELECT assessment_pk, patient_internal_id, raw_json, assessment_date FROM assessments WHERE is_current=1 AND (status='Complete' OR status IS NULL)")
    ).mappings().all()
    for a in assessments:
        if not a["raw_json"]:
            continue
        wound = extract_from_assessment_json(a["raw_json"])
        db.execute(
            text(
                """
                INSERT INTO wound_extractions (
                    patient_internal_id, source_table, source_pk, source_date, wound_index_in_source, is_primary,
                    location, wound_type, stage, length_cm, width_cm, depth_cm, drainage_amount,
                    location_status, wound_type_status, stage_status, length_cm_status, width_cm_status,
                    depth_cm_status, drainage_amount_status, note_format, parser_version, parsed_at
                ) VALUES (
                    :pi, 'assessments', :spk, :sd, 0, 1,
                    :loc, :wt, :st, :l, :w, :d, :dr,
                    :ls, :wts, :sts, :lns, :wns, :dns, :das,
                    :fmt, :pv, :pa
                )
                """
            ),
            {
                "pi": a["patient_internal_id"], "spk": a["assessment_pk"], "sd": a["assessment_date"],
                "loc": wound.location, "wt": wound.wound_type, "st": wound.stage,
                "l": wound.length_cm, "w": wound.width_cm, "d": wound.depth_cm, "dr": wound.drainage_amount,
                "ls": wound.location_status, "wts": wound.wound_type_status, "sts": wound.stage_status,
                "lns": wound.length_cm_status, "wns": wound.width_cm_status, "dns": wound.depth_cm_status,
                "das": wound.drainage_amount_status, "fmt": wound.note_format, "pv": PARSER_VERSION, "pa": now_iso(),
            },
        )
    db.commit()
    db.close()
    print("Extraction complete")


def run_decide() -> None:
    init_db()
    db = SessionLocal()
    sync_run_id = db.execute(text("SELECT MAX(sync_run_id) FROM sync_runs")).scalar() or 1

    patients = db.execute(text("SELECT patient_internal_id, patient_id, facility_id FROM patients")).mappings().all()
    for i, p in enumerate(patients, 1):
        pi = p["patient_internal_id"]
        diagnoses = [dict(r) for r in db.execute(text("SELECT * FROM diagnoses WHERE patient_internal_id=:pi"), {"pi": pi}).mappings().all()]
        coverage = [dict(r) for r in db.execute(text("SELECT payer_code, payer_type, payer_name, effective_from, effective_to FROM coverage WHERE patient_internal_id=:pi"), {"pi": pi}).mappings().all()]
        wounds = [dict(r) for r in db.execute(text("SELECT * FROM wound_extractions WHERE patient_internal_id=:pi"), {"pi": pi}).mappings().all()]
        flags = [dict(r) for r in db.execute(text("SELECT * FROM unknown_flags WHERE patient_internal_id=:pi"), {"pi": pi}).mappings().all()]

        note_wounds = [w for w in wounds if w["source_table"] == "notes"]
        assess_wounds = [w for w in wounds if w["source_table"] == "assessments"]
        conflict = False
        if note_wounds and assess_wounds:
            n, a = note_wounds[-1], assess_wounds[-1]
            if n.get("stage") and a.get("stage") and str(n["stage"]) != str(a["stage"]):
                conflict = True
                db.execute(
                    text(
                        "INSERT INTO unknown_flags (patient_internal_id, flag_type, severity, detail, created_at, sync_run_id) "
                        "VALUES (:pi, 'note_assessment_conflict', 'high', :d, :ca, :sr)"
                    ),
                    {"pi": pi, "d": f"Note stage {n['stage']} vs assessment stage {a['stage']}", "ca": now_iso(), "sr": sync_run_id},
                )

        envive_only = bool(note_wounds) and not assess_wounds and any(w.get("note_format") == "envive" for w in note_wounds)
        if len(coverage) > 1:
            db.execute(
                text(
                    "INSERT INTO unknown_flags (patient_internal_id, flag_type, severity, detail, created_at, sync_run_id) "
                    "VALUES (:pi, 'multiple_active_payers', 'medium', 'Multiple coverage records on file', :ca, :sr)"
                ),
                {"pi": pi, "ca": now_iso(), "sr": sync_run_id},
            )
            flags.append({"severity": "medium"})

        result = decide(diagnoses, coverage, wounds, flags, envive_only=envive_only, note_assessment_conflict=conflict)
        pw = result.primary_wound or {}

        db.execute(
            text("DELETE FROM eligibility WHERE patient_internal_id=:pi"),
            {"pi": pi},
        )
        db.execute(
            text(
                """
                INSERT INTO eligibility (
                    patient_internal_id, patient_id, facility_id,
                    primary_wound_type, primary_wound_stage, primary_wound_location,
                    length_cm, width_cm, depth_cm, drainage_amount,
                    primary_wound_source_table, primary_wound_source_pk,
                    has_active_medicare_b, other_active_payers_json,
                    routing_decision, routing_reason,
                    unknown_risk_score, unknown_risk_tier, unknown_flag_count,
                    note_assessment_conflict, multiple_eligible_wounds, envive_narrative_only,
                    computed_at, sync_run_id
                ) VALUES (
                    :pi, :pid, :fid,
                    :pwt, :pws, :pwl, :l, :w, :d, :dr,
                    :pst, :psp,
                    :mcb, :oap,
                    :rd, :rr,
                    :urs, :urt, :ufc,
                    :nac, :mew, :eno,
                    :ca, :sr
                )
                """
            ),
            {
                "pi": pi, "pid": p["patient_id"], "fid": p["facility_id"],
                "pwt": pw.get("wound_type"), "pws": pw.get("stage"), "pwl": pw.get("location"),
                "l": pw.get("length_cm"), "w": pw.get("width_cm"), "d": pw.get("depth_cm"), "dr": pw.get("drainage_amount"),
                "pst": pw.get("source_table"), "psp": pw.get("source_pk"),
                "mcb": 1 if result.has_active_medicare_b else 0,
                "oap": json.dumps(result.other_active_payers),
                "rd": result.routing_decision, "rr": result.routing_reason,
                "urs": result.unknown_risk_score, "urt": result.unknown_risk_tier, "ufc": result.unknown_flag_count,
                "nac": 1 if result.note_assessment_conflict else 0,
                "mew": 1 if result.multiple_eligible_wounds else 0,
                "eno": 1 if result.envive_narrative_only else 0,
                "ca": now_iso(), "sr": sync_run_id,
            },
        )
        if i % BATCH_COMMIT == 0:
            db.commit()
    db.commit()
    db.close()
    print("Eligibility decisions complete")


def export_features_csv(path: str) -> None:
    import csv
    from pathlib import Path

    init_db()
    db = SessionLocal()
    rows = db.execute(
        text(
            """
            SELECT
                e.patient_internal_id,
                e.patient_id,
                e.facility_id,
                e.has_active_medicare_b,
                e.routing_decision,
                e.unknown_risk_score,
                e.unknown_flag_count,
                e.note_assessment_conflict,
                e.multiple_eligible_wounds,
                e.envive_narrative_only,
                e.length_cm, e.width_cm, e.depth_cm,
                p.gender,
                p.primary_payer_code,
                (SELECT COUNT(*) FROM diagnoses d WHERE d.patient_internal_id=e.patient_internal_id AND d.clinical_status='active') AS active_dx_count,
                (SELECT COUNT(*) FROM notes n WHERE n.patient_internal_id=e.patient_internal_id) AS note_count,
                (SELECT COUNT(*) FROM assessments a WHERE a.patient_internal_id=e.patient_internal_id) AS assessment_count
            FROM eligibility e
            JOIN patients p ON p.patient_internal_id = e.patient_internal_id
            """
        )
    ).mappings().all()
    db.close()

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        print("No eligibility rows to export. Run sync/extract/decide first.")
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])
    print(f"Exported {len(rows)} rows to {path}")
