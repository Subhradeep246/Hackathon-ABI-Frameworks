"""Wound extraction from notes and assessments."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

PARSER_VERSION = "1.0.0"

KNOWN = "known"
UNKNOWN_MISSING = "unknown_missing"
UNKNOWN_UNPARSEABLE = "unknown_unparseable"

WOUND_KEYWORDS = {
    "pressure_ulcer": [r"pressure\s+ulcer", r"pressure\s+injury", r"decubitus"],
    "diabetic_foot_ulcer": [r"diabetic\s+foot\s+ulcer", r"\bdfu\b"],
    "venous_stasis_ulcer": [r"venous\s+stasis\s+ulcer", r"venous\s+ulcer"],
    "arterial_ulcer": [r"arterial\s+ulcer"],
    "surgical_site_infection": [r"surgical\s+site\s+infection", r"ssi\b"],
    "abscess": [r"abscess"],
    "burn": [r"\bburn\b"],
}

DRAINAGE_MAP = {
    "none": "none",
    "no drainage": "none",
    "scant": "light",
    "light": "light",
    "moderate": "moderate",
    "heavy": "heavy",
}


@dataclass
class WoundRecord:
    location: str | None = None
    wound_type: str | None = None
    stage: str | None = None
    length_cm: float | None = None
    width_cm: float | None = None
    depth_cm: float | None = None
    drainage_amount: str | None = None
    location_status: str = UNKNOWN_MISSING
    wound_type_status: str = UNKNOWN_MISSING
    stage_status: str = UNKNOWN_MISSING
    length_cm_status: str = UNKNOWN_MISSING
    width_cm_status: str = UNKNOWN_MISSING
    depth_cm_status: str = UNKNOWN_MISSING
    drainage_amount_status: str = UNKNOWN_MISSING
    note_format: str = "unknown"
    secondary_wounds: list[dict] = field(default_factory=list)


def _status(value, parsed: bool, unparseable: bool = False) -> str:
    if value is not None and parsed:
        return KNOWN
    if unparseable:
        return UNKNOWN_UNPARSEABLE
    return UNKNOWN_MISSING


def detect_note_format(text: str) -> str:
    lower = text.lower()
    if "envive" in lower or "care conference review" in lower:
        return "envive"
    if re.search(r"location\s*:", text, re.I) and re.search(r"wound\s+type\s*:", text, re.I):
        return "soap"
    if re.search(r"\d+(?:\.\d+)?\s*x\s*\d+(?:\.\d+)?", lower):
        return "prose"
    if len(re.findall(r"stage\s*[234]|pressure\s+ulcer", lower)) > 1:
        return "multi_wound"
    return "unknown"


def _parse_stage(text: str) -> str | None:
    m = re.search(r"stage\s*[:.]?\s*(?:stage\s*)?(\d+|unstageable)", text, re.I)
    return m.group(1).lower() if m else None


def _parse_drainage(text: str) -> str | None:
    lower = text.lower()
    for key, val in DRAINAGE_MAP.items():
        if key in lower:
            return val
    m = re.search(r"drainage\s*[:.]?\s*(\w+)", lower)
    if m:
        word = m.group(1).lower()
        return DRAINAGE_MAP.get(word, word if word in {"none", "light", "moderate", "heavy"} else None)
    return None


def _parse_measurements(text: str) -> tuple[float | None, float | None, float | None]:
    # 2.9 cm x 2.8 cm or 4.2x3.1x1.5cm
    m3 = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:cm)?\s*x\s*(\d+(?:\.\d+)?)\s*(?:cm)?\s*x\s*(\d+(?:\.\d+)?)",
        text,
        re.I,
    )
    if m3:
        return float(m3.group(1)), float(m3.group(2)), float(m3.group(3))
    m2 = re.search(r"(\d+(?:\.\d+)?)\s*cm\s*x\s*(\d+(?:\.\d+)?)\s*cm", text, re.I)
    if m2:
        return float(m2.group(1)), float(m2.group(2)), None
    m2b = re.search(r"measures?\s+(\d+(?:\.\d+)?)\s*cm\s*x\s*(\d+(?:\.\d+)?)", text, re.I)
    if m2b:
        return float(m2b.group(1)), float(m2b.group(2)), None
    return None, None, None


def _parse_location(text: str) -> str | None:
    m = re.search(r"location\s*:\s*([^\n,]+)", text, re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(
        r"(?:pressure\s+ulcer|wound)\s+(?:to|on|at)\s+([a-z\s]+?)(?:\s*/|\s+measures|\s+stage|$)",
        text,
        re.I,
    )
    return m2.group(1).strip() if m2 else None


def _parse_wound_type(text: str) -> str | None:
    lower = text.lower()
    for wtype, patterns in WOUND_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, lower):
                return wtype
    m = re.search(r"wound\s+type\s*:\s*([^\n,]+)", text, re.I)
    if m:
        label = m.group(1).strip().lower()
        for wtype, patterns in WOUND_KEYWORDS.items():
            for pat in patterns:
                if re.search(pat, label):
                    return wtype
    return None


def extract_from_text(text: str) -> WoundRecord:
    fmt = detect_note_format(text)
    w = WoundRecord(note_format=fmt)
    w.location = _parse_location(text)
    w.wound_type = _parse_wound_type(text)
    w.stage = _parse_stage(text)
    w.length_cm, w.width_cm, w.depth_cm = _parse_measurements(text)
    w.drainage_amount = _parse_drainage(text)

    w.location_status = _status(w.location, w.location is not None, fmt == "envive" and not w.location)
    w.wound_type_status = _status(w.wound_type, w.wound_type is not None)
    w.stage_status = _status(w.stage, w.stage is not None)
    w.length_cm_status = _status(w.length_cm, w.length_cm is not None)
    w.width_cm_status = _status(w.width_cm, w.width_cm is not None)
    w.depth_cm_status = _status(w.depth_cm, w.depth_cm is not None)
    w.drainage_amount_status = _status(w.drainage_amount, w.drainage_amount is not None)
    return w


def extract_from_assessment_json(raw_json: str | dict) -> WoundRecord:
    data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    w = WoundRecord(note_format="structured_json")

    def get(*keys: str):
        for k in keys:
            if k in data and data[k] is not None:
                return data[k]
        return None

    w.location = get("location", "wound_location")
    w.wound_type = get("wound_type")
    if w.wound_type:
        w.wound_type = w.wound_type.lower().replace(" ", "_")
    w.stage = str(get("stage", "wound_stage") or "") or None
    if w.stage:
        w.stage = re.sub(r"[^0-9a-z]", "", w.stage.lower().replace("stage", "")) or w.stage
    w.length_cm = _to_float(get("length_cm", "length"))
    w.width_cm = _to_float(get("width_cm", "width"))
    w.depth_cm = _to_float(get("depth_cm", "depth"))
    w.drainage_amount = get("drainage_amount", "drainage")
    if w.drainage_amount:
        w.drainage_amount = str(w.drainage_amount).lower()

    for attr in ("location", "wound_type", "stage", "length_cm", "width_cm", "depth_cm", "drainage_amount"):
        val = getattr(w, attr)
        setattr(w, f"{attr}_status", KNOWN if val is not None else UNKNOWN_MISSING)
    return w


def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
