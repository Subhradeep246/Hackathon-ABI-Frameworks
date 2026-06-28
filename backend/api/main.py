"""FastAPI application."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

from backend.cache import get_data_version, get_stats_cache, invalidate_stats_cache, set_stats_cache
from backend.db.database import get_db, init_db
from backend.jobs import get_pipeline_status, start_auto_sync_on_boot, start_pipeline, subscribe, unsubscribe
from backend.llm import chat_answer, close_client, patient_summary

FRONTEND = ROOT / "frontend"
MODEL_PATH = ROOT / "ml" / "models" / "decision_tree.joblib"
STATS_TTL = int(os.getenv("STATS_CACHE_SECONDS", "15"))
VALID_DECISIONS = {"auto_accept", "flag_for_review", "reject"}
VALID_RISK = {"green", "yellow", "red"}

app = FastAPI(title="ABI Wound Care Eligibility API", version="1.2.0")
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_model = None
_sse_queues: list[asyncio.Queue] = []


def _load_model():
    global _model
    if _model is None and MODEL_PATH.exists():
        import joblib

        _model = joblib.load(MODEL_PATH)
    return _model


def _build_stats(db: Session) -> dict[str, Any]:
    rows = db.execute(
        text("SELECT routing_decision, COUNT(*) AS cnt FROM eligibility GROUP BY routing_decision")
    ).mappings().all()
    by_decision = {r["routing_decision"]: r["cnt"] for r in rows}
    facility_rows = db.execute(
        text("SELECT facility_id, routing_decision, COUNT(*) AS cnt FROM eligibility GROUP BY facility_id, routing_decision")
    ).mappings().all()
    by_facility: dict[int, dict] = {}
    for r in facility_rows:
        by_facility.setdefault(r["facility_id"], {})[r["routing_decision"]] = r["cnt"]

    sync = db.execute(
        text("SELECT sync_run_id, status, started_at, finished_at FROM sync_runs ORDER BY sync_run_id DESC LIMIT 1")
    ).mappings().first()
    total = db.execute(text("SELECT COUNT(*) FROM patients")).scalar() or 0
    return {
        "total_patients": total,
        "by_decision": by_decision,
        "by_facility": by_facility,
        "last_sync": dict(sync) if sync else None,
        "pipeline": get_pipeline_status(),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": get_data_version(),
    }


def _patient_filters(
    facility_id: int | None,
    routing_decision: str | None,
    risk_tier: str | None,
    search: str | None,
    has_medicare_b: int | None = None,
) -> tuple[list[str], dict[str, Any]]:
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if facility_id is not None:
        clauses.append("e.facility_id = :facility_id")
        params["facility_id"] = facility_id
    if routing_decision:
        if routing_decision not in VALID_DECISIONS:
            raise HTTPException(400, f"Invalid routing_decision: {routing_decision}")
        clauses.append("e.routing_decision = :routing_decision")
        params["routing_decision"] = routing_decision
    if risk_tier:
        if risk_tier not in VALID_RISK:
            raise HTTPException(400, f"Invalid risk_tier: {risk_tier}")
        clauses.append("e.unknown_risk_tier = :risk_tier")
        params["risk_tier"] = risk_tier
    if has_medicare_b is not None:
        if has_medicare_b not in (0, 1):
            raise HTTPException(400, "has_medicare_b must be 0 or 1")
        clauses.append("e.has_active_medicare_b = :has_mcb")
        params["has_mcb"] = has_medicare_b
    if search:
        safe = search.strip()[:80]
        if safe:
            clauses.append("(e.patient_id LIKE :search OR p.first_name LIKE :search OR p.last_name LIKE :search)")
            params["search"] = f"%{safe}%"
    return clauses, params


def _fetch_patients(db: Session, clauses: list[str], params: dict, limit: int, offset: int) -> tuple[list[dict], int]:
    where = " AND ".join(clauses)
    qparams = {**params, "limit": limit, "offset": offset}
    rows = db.execute(
        text(
            f"""
            SELECT e.patient_id, e.facility_id, e.primary_wound_type, e.primary_wound_stage,
                   e.has_active_medicare_b, e.routing_decision, e.unknown_risk_tier, e.unknown_risk_score,
                   p.first_name, p.last_name
            FROM eligibility e
            JOIN patients p ON p.patient_internal_id = e.patient_internal_id
            WHERE {where}
            ORDER BY e.unknown_risk_score DESC, e.patient_id
            LIMIT :limit OFFSET :offset
            """
        ),
        qparams,
    ).mappings().all()
    total = db.execute(
        text(
            f"""
            SELECT COUNT(*) FROM eligibility e
            JOIN patients p ON p.patient_internal_id = e.patient_internal_id
            WHERE {where}
            """
        ),
        params,
    ).scalar() or 0
    return [dict(r) for r in rows], total


def _get_patient_detail(patient_id: str, db: Session, notes_limit: int = 5) -> dict:
    row = db.execute(
        text(
            """
            SELECT e.*, p.first_name, p.last_name, p.gender, p.date_of_birth, p.primary_payer_code
            FROM eligibility e
            JOIN patients p ON p.patient_internal_id = e.patient_internal_id
            WHERE e.patient_id = :pid
            """
        ),
        {"pid": patient_id},
    ).mappings().first()
    if not row:
        raise HTTPException(404, "Patient not found")

    pi = row["patient_internal_id"]
    notes = db.execute(
        text("SELECT note_pk, note_type, effective_date, raw_text FROM notes WHERE patient_internal_id=:pi ORDER BY effective_date DESC LIMIT :lim"),
        {"pi": pi, "lim": notes_limit},
    ).mappings().all()
    assessments = db.execute(
        text("SELECT assessment_pk, assessment_date, status, raw_text FROM assessments WHERE patient_internal_id=:pi ORDER BY assessment_date DESC LIMIT :lim"),
        {"pi": pi, "lim": notes_limit},
    ).mappings().all()
    wounds = db.execute(text("SELECT location, wound_type, stage, length_cm, width_cm, depth_cm, drainage_amount, source_table FROM wound_extractions WHERE patient_internal_id=:pi"), {"pi": pi}).mappings().all()
    flags = db.execute(text("SELECT flag_type, severity, detail FROM unknown_flags WHERE patient_internal_id=:pi ORDER BY created_at DESC LIMIT 20"), {"pi": pi}).mappings().all()
    diagnoses = db.execute(text("SELECT icd10_code, description, clinical_status FROM diagnoses WHERE patient_internal_id=:pi"), {"pi": pi}).mappings().all()
    coverage = db.execute(text("SELECT payer_code, payer_type, payer_name, effective_from, effective_to FROM coverage WHERE patient_internal_id=:pi"), {"pi": pi}).mappings().all()
    model = db.execute(text("SELECT model_suggestion, model_probability, rule_agrees FROM model_insights WHERE patient_internal_id=:pi"), {"pi": pi}).mappings().first()

    def _trim_note(n: dict) -> dict:
        d = dict(n)
        t = d.get("raw_text") or ""
        if len(t) > 500:
            d["raw_text"] = t[:500] + "…"
        return d

    return {
        "patient": dict(row),
        "notes": [_trim_note(n) for n in notes],
        "assessments": [dict(a) for a in assessments],
        "wound_extractions": [dict(w) for w in wounds],
        "unknown_flags": _aggregate_flags([dict(f) for f in flags]),
        "diagnoses": [dict(d) for d in diagnoses],
        "coverage": [dict(c) for c in coverage],
        "model_insight": dict(model) if model else None,
    }


def _aggregate_flags(flags: list) -> list[dict]:
    """Collapse duplicate flag rows (e.g. one per Envive note) into readable groups."""
    grouped: dict[str, dict] = {}
    for f in flags:
        ft = f["flag_type"]
        if ft not in grouped:
            detail = f.get("detail") or f.get("severity") or ""
            if ft == "envive_narrative_only":
                detail = "Wound details may only exist in free-text narrative, not structured fields"
            grouped[ft] = {
                "flag_type": ft,
                "severity": f.get("severity"),
                "detail": detail,
                "count": 0,
            }
        grouped[ft]["count"] += 1
    return list(grouped.values())


def _broadcast_sse() -> None:
    for q in list(_sse_queues):
        try:
            q.put_nowait(get_data_version())
        except Exception:
            pass


@app.on_event("startup")
def startup():
    init_db()
    subscribe(_broadcast_sse)
    start_auto_sync_on_boot()


@app.on_event("shutdown")
async def shutdown():
    unsubscribe(_broadcast_sse)
    await close_client()


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    from backend.ingestion.client import get_client_stats

    total = db.execute(text("SELECT COUNT(*) FROM patients")).scalar() or 0
    return {
        "status": "ok",
        "patients": total,
        "baseten_configured": bool(os.getenv("BASETEN_API_KEY")),
        "pipeline": get_pipeline_status(),
        "api_stats": get_client_stats(),
    }


@app.get("/api/events")
async def events():
    """SSE stream — pushes when data version changes (sync complete, etc.)."""
    queue: asyncio.Queue = asyncio.Queue()
    _sse_queues.append(queue)

    async def stream():
        try:
            last = -1
            # Immediate hello so clients leave "Connecting…" quickly
            ver = get_data_version()
            payload = {"version": ver, "pipeline": get_pipeline_status()}
            yield f"data: {json.dumps(payload)}\n\n"
            last = ver
            while True:
                try:
                    ver = await asyncio.wait_for(queue.get(), timeout=8.0)
                except asyncio.TimeoutError:
                    ver = get_data_version()
                if ver != last:
                    payload = {"version": ver, "pipeline": get_pipeline_status()}
                    yield f"data: {json.dumps(payload)}\n\n"
                    last = ver
                else:
                    yield ": keepalive\n\n"
        finally:
            if queue in _sse_queues:
                _sse_queues.remove(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    cached = get_stats_cache(STATS_TTL)
    if cached is not None:
        return cached
    result = _build_stats(db)
    set_stats_cache(result)
    return result


@app.get("/api/dashboard")
def dashboard(
    db: Session = Depends(get_db),
    facility_id: int | None = None,
    routing_decision: str | None = None,
    risk_tier: str | None = None,
    has_medicare_b: int | None = Query(None, ge=0, le=1),
    search: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Single fast payload: stats + patient page."""
    stats_data = _build_stats(db)
    clauses, params = _patient_filters(facility_id, routing_decision, risk_tier, search, has_medicare_b)
    items, total = _fetch_patients(db, clauses, params, limit, offset)
    return {
        "stats": stats_data,
        "patients": {"items": items, "total": total, "limit": limit, "offset": offset},
        "version": get_data_version(),
    }


@app.get("/api/sync/status")
def sync_status():
    return get_pipeline_status()


@app.post("/api/sync")
def trigger_sync(incremental: bool = True):
    if not start_pipeline(incremental=incremental):
        raise HTTPException(409, "Pipeline already running")
    return {"ok": True, "message": "Pipeline started"}


@app.get("/api/patients")
def list_patients(
    db: Session = Depends(get_db),
    facility_id: int | None = None,
    routing_decision: str | None = None,
    risk_tier: str | None = None,
    has_medicare_b: int | None = Query(None, ge=0, le=1),
    search: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    clauses, params = _patient_filters(facility_id, routing_decision, risk_tier, search, has_medicare_b)
    items, total = _fetch_patients(db, clauses, params, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/patients/{patient_id}")
async def get_patient(
    patient_id: str,
    db: Session = Depends(get_db),
    notes_limit: int = Query(5, ge=1, le=20),
    with_summary: bool = Query(True),
):
    if not patient_id or len(patient_id) > 64:
        raise HTTPException(400, "Invalid patient_id")
    detail = _get_patient_detail(patient_id, db, notes_limit)
    if with_summary:
        summary = await patient_summary(
            detail["patient"],
            detail["unknown_flags"],
            detail["diagnoses"],
            detail["coverage"],
            detail["model_insight"],
        )
        detail["ai_summary"] = summary
    return detail


@app.post("/api/chat")
async def chat(payload: dict, db: Session = Depends(get_db)):
    question = (payload.get("question") or "").strip()
    patient_id = payload.get("patient_id")
    if not question:
        raise HTTPException(400, "question required")
    if len(question) > 2000:
        raise HTTPException(400, "question too long")

    if patient_id:
        detail = _get_patient_detail(patient_id, db)
        context = {
            "patient": detail["patient"],
            "unknown_flags": detail["unknown_flags"],
            "diagnoses": detail["diagnoses"],
            "coverage": detail["coverage"],
            "wound_extractions": detail["wound_extractions"],
            "model_insight": detail["model_insight"],
        }
    else:
        context = _build_stats(db)

    result = await chat_answer(question, context)
    _, _, model = (os.getenv("BASETEN_API_KEY"), os.getenv("BASETEN_BASE_URL"), os.getenv("BASETEN_MODEL", "zai-org/GLM-5.2"))
    return {**result, "model": model if result["source"] == "baseten" else None}


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-cache"})
