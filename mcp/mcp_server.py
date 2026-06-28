"""FastMCP server: extraction + decision tools for wound-care billing triage.

These five tools are the only code that reads raw note/assessment text. The
pipeline (built by the rest of the team) calls them and stores the structured
result. No LLM is required for the core implementation.

Each tool is implemented as a plain, pure `_*_impl` function (directly
importable and unit-testable) and exposed as an MCP tool via FastMCP. Run the
stdio server with:  python mcp_server.py
"""

import json
import re

from fastmcp import FastMCP

mcp = FastMCP("wound-care-triage")

# Required fields for a "complete" extraction (per PRD: L/W/D + drainage_amount).
REQUIRED_FIELDS = ["length_cm", "width_cm", "depth_cm", "drainage_amount"]

# Phase 3 diagnosis gate: ICD-10 prefixes that denote a billable wound.
WOUND_ICD10_PREFIXES = (
    "L89",      # pressure ulcer
    "L97",      # non-pressure chronic ulcer of lower limb
    "L98",      # other ulcer of skin
    "I83.0",    # varicose veins with ulcer
    "I83.2",    # varicose veins with ulcer + inflammation
    "T81.4",    # infection following a procedure
    "T81.3",    # disruption of wound
    "L02",      # cutaneous abscess
    "E11.621",  # type 2 diabetes w/ foot ulcer
    "E10.621",  # type 1 diabetes w/ foot ulcer
)
# Burns: ICD-10 T20–T32. We match the leading "T" + 2-digit block numerically.
BURN_RANGE = range(20, 33)  # T20 .. T32 inclusive

# Phase 3 description-keyword fallback.
WOUND_DESC_KEYWORDS = ("ulcer", "wound", "abscess", "burn", "surgical site")

# Drainage keyword -> normalized amount.
DRAINAGE_SYNONYMS = {
    "none": "none",
    "dry": "none",
    "no drainage": "none",
    "absent": "none",
    "scant": "light",
    "light": "light",
    "minimal": "light",
    "small": "light",
    "trace": "light",
    "moderate": "moderate",
    "medium": "moderate",
    "heavy": "heavy",
    "large": "heavy",
    "copious": "heavy",
    "profuse": "heavy",
}

# Wound-type classification keywords (longest/most-specific first).
WOUND_TYPE_KEYWORDS = [
    ("diabetic foot ulcer", ["diabetic foot", "dfu", "diabetic ulcer"]),
    ("pressure_ulcer", ["pressure ulcer", "pressure injury", "decubitus", "bedsore"]),
    ("venous_ulcer", ["venous ulcer", "venous stasis", "stasis ulcer"]),
    ("arterial_ulcer", ["arterial ulcer", "ischemic ulcer"]),
    ("surgical_site_infection", ["surgical site", "ssi", "incision", "post-op wound"]),
    ("abscess", ["abscess"]),
    ("burn", ["burn"]),
]

# Severity order for multi-wound primary selection (higher = more severe).
SEVERITY_ORDER = [
    "burn",
    "abscess",
    "surgical_site_infection",
    "arterial_ulcer",
    "venous_ulcer",
    "diabetic foot ulcer",
    "stage 2",
    "stage 3",
    "unstageable",
    "stage 4",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _missing_required(fields):
    """Return the list of required fields that are absent/None in `fields`."""
    return [f for f in REQUIRED_FIELDS if fields.get(f) in (None, "")]


def _to_float(value):
    """Best-effort float conversion; return None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_drainage(text):
    """Map a free-text drainage descriptor to none/light/moderate/heavy."""
    if not text:
        return None
    low = text.lower()
    # Longest synonyms first so "no drainage" wins over "drainage".
    for syn in sorted(DRAINAGE_SYNONYMS, key=len, reverse=True):
        if syn in low:
            return DRAINAGE_SYNONYMS[syn]
    return None


def _classify_wound_type(text):
    """Classify wound type from free text by keyword; None if no match."""
    low = text.lower()
    for wound_type, keywords in WOUND_TYPE_KEYWORDS:
        if any(kw in low for kw in keywords):
            return wound_type
    return None


def _severity_rank(wound_type, stage):
    """Rank a wound for primary selection. Higher rank = more severe/primary."""
    if stage:
        key = f"stage {stage}".lower() if str(stage).isdigit() else str(stage).lower()
        if key in SEVERITY_ORDER:
            return SEVERITY_ORDER.index(key)
    if wound_type in SEVERITY_ORDER:
        return SEVERITY_ORDER.index(wound_type)
    return -1


# --------------------------------------------------------------------------- #
# Tool implementations (pure functions)
# --------------------------------------------------------------------------- #
def _extract_from_assessment_impl(raw_json):
    """Parse a structured assessment JSON string into wound fields."""
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else dict(raw_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        data = {}

    fields = {
        "wound_type": data.get("wound_type"),
        "stage": data.get("stage"),
        "location": data.get("location"),
        "length_cm": _to_float(data.get("length_cm")),
        "width_cm": _to_float(data.get("width_cm")),
        "depth_cm": _to_float(data.get("depth_cm")),
        "drainage_amount": data.get("drainage_amount"),
        "drainage_type": data.get("drainage_type"),
        "source": "assessment",
    }
    missing = _missing_required(fields)
    fields["confidence"] = "high" if not missing else "medium"
    fields["missing"] = missing
    return fields


def _extract_measurements_from_text(text):
    """Return (length, width, depth) floats parsed from free text, or Nones."""
    length = width = depth = None

    # Labeled form: "Length: 3.2 cm  Width: 2.1 cm  Depth: 0.4 cm"
    for label, key in (("length", "l"), ("width", "w"), ("depth", "d")):
        m = re.search(rf"{label}\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
        if m:
            val = float(m.group(1))
            if key == "l":
                length = val
            elif key == "w":
                width = val
            else:
                depth = val

    # Shorthand form: "4.2x3.1x1.5" (optionally with cm / spaces).
    if length is None or width is None or depth is None:
        m = re.search(
            r"([0-9]+(?:\.[0-9]+)?)\s*[xX*]\s*([0-9]+(?:\.[0-9]+)?)"
            r"\s*[xX*]\s*([0-9]+(?:\.[0-9]+)?)",
            text,
        )
        if m:
            length = length if length is not None else float(m.group(1))
            width = width if width is not None else float(m.group(2))
            depth = depth if depth is not None else float(m.group(3))

    return length, width, depth


def _find_wounds_in_text(text):
    """Find all distinct wound descriptions in text for multi-wound handling.

    Returns a list of (wound_type, stage) candidates. A single wound yields a
    one-element list.
    """
    low = text.lower()
    found = []
    for wound_type, keywords in WOUND_TYPE_KEYWORDS:
        for kw in keywords:
            if kw in low:
                stage = None
                # Look for a stage near a pressure-ulcer mention.
                sm = re.search(r"stage\s*([0-9]+)", low)
                if sm:
                    stage = int(sm.group(1))
                elif "unstageable" in low:
                    stage = "unstageable"
                found.append((wound_type, stage))
                break
    return found


def _extract_from_note_impl(note_text):
    """Regex-extract wound fields from a free-text progress note."""
    text = note_text or ""

    length, width, depth = _extract_measurements_from_text(text)
    drainage_amount = None
    dm = re.search(r"drainage\s*[:=]?\s*([a-z ]+)", text, re.I)
    if dm:
        drainage_amount = _normalize_drainage(dm.group(1))
    if drainage_amount is None:
        drainage_amount = _normalize_drainage(text)

    # Multi-wound primary selection: severity, then surface area, then order.
    wounds = _find_wounds_in_text(text)
    if wounds:
        primary = max(
            wounds,
            key=lambda w: (
                _severity_rank(w[0], w[1]),
                (length or 0) * (width or 0),
            ),
        )
        wound_type, stage = primary
    else:
        wound_type = _classify_wound_type(text)
        sm = re.search(r"stage\s*([0-9]+)", text, re.I)
        stage = int(sm.group(1)) if sm else None

    loc_m = re.search(r"location\s*[:=]?\s*([A-Za-z ]+)", text, re.I)
    location = loc_m.group(1).strip() if loc_m else None

    fields = {
        "wound_type": wound_type,
        "stage": stage,
        "location": location,
        "length_cm": length,
        "width_cm": width,
        "depth_cm": depth,
        "drainage_amount": drainage_amount,
        "drainage_type": None,
        "source": "note",
    }
    missing = _missing_required(fields)
    fields["confidence"] = "medium" if not missing else "low"
    fields["missing"] = missing
    return fields


def _check_part_b_impl(coverage_records):
    """True if any coverage record is active Medicare Part B."""
    for rec in coverage_records or []:
        is_mcb = rec.get("payer_code") == "MCB" or rec.get("payer_type") == "Medicare B"
        if is_mcb and rec.get("effective_to") is None:
            return {"has_active_part_b": True, "payer": rec.get("payer_code") or "MCB"}
    return {"has_active_part_b": False, "payer": ""}


def _matches_wound_dx(diag):
    """True if a diagnosis row matches a wound by prefix OR description keyword."""
    code = (diag.get("icd10_code") or "").strip()
    desc = (diag.get("icd10_description") or "").lower()

    if any(code.startswith(p) for p in WOUND_ICD10_PREFIXES):
        return True
    # Burns T20–T32.
    bm = re.match(r"T(\d{2})", code)
    if bm and int(bm.group(1)) in BURN_RANGE:
        return True
    if any(kw in desc for kw in WOUND_DESC_KEYWORDS):
        return True
    return False


def _check_active_wound_dx_impl(diagnoses):
    """True if any active diagnosis matches a wound (prefix or keyword)."""
    for diag in diagnoses or []:
        if diag.get("clinical_status") == "active" and _matches_wound_dx(diag):
            return {"has_active_wound": True, "icd10_code": diag.get("icd10_code") or ""}
    return {"has_active_wound": False, "icd10_code": ""}


def _fmt_measurements(wf):
    """Format 'LxWxD cm, <drainage> drainage' for the auto_accept reason."""
    l, w, d = wf.get("length_cm"), wf.get("width_cm"), wf.get("depth_cm")
    drainage = wf.get("drainage_amount") or "unknown"
    return f"{l}×{w}×{d} cm, {drainage} drainage"


def _decide_eligibility_impl(wound_fields, has_active_part_b, has_active_wound):
    """Deterministic routing decision. Returns decision + reason + rule_fired."""
    wf = dict(wound_fields or {})
    result = dict(wf)  # decision spreads the wound fields back out

    # Rule 1: no Part B.
    if not has_active_part_b:
        result.update(
            decision="reject",
            reason="Not Medicare Part B — not billable.",
            rule_fired="no_part_b",
        )
        return result

    # Rule 2: no active wound diagnosis.
    if not has_active_wound:
        result.update(
            decision="reject",
            reason="No active wound diagnosis on record.",
            rule_fired="no_active_wound",
        )
        return result

    # Recompute missing from the required fields (don't trust caller blindly).
    missing = wf.get("missing")
    if missing is None:
        missing = _missing_required(wf)

    # Rule 3: active wound + Part B, but measurements/drainage incomplete.
    if missing:
        result.update(
            decision="flag_for_review",
            reason=(
                "Active wound + Part B confirmed, but measurements/drainage "
                f"incomplete (missing: {', '.join(missing)}). Biller should verify."
            ),
            rule_fired="incomplete_measurements",
        )
        return result

    # Rule 4: all fields present but low-confidence extraction.
    if wf.get("confidence") == "low":
        result.update(
            decision="flag_for_review",
            reason="All fields present but extraction confidence is low. "
            "Biller should verify.",
            rule_fired="low_confidence",
        )
        return result

    # Rule 5: complete and confident -> auto-accept.
    result.update(
        decision="auto_accept",
        reason=(
            "Active Part B, active wound, complete measurements "
            f"({_fmt_measurements(wf)}). Safe to bill."
        ),
        rule_fired="complete",
    )
    return result


# --------------------------------------------------------------------------- #
# MCP tool wrappers
# --------------------------------------------------------------------------- #
@mcp.tool
def extract_from_assessment(raw_json: str) -> dict:
    """Parse a structured assessment JSON string into wound fields."""
    return _extract_from_assessment_impl(raw_json)


@mcp.tool
def extract_from_note(note_text: str) -> dict:
    """Regex-extract wound fields from a free-text progress note."""
    return _extract_from_note_impl(note_text)


@mcp.tool
def check_part_b(coverage_records: list[dict]) -> dict:
    """Return whether the patient has active Medicare Part B coverage."""
    return _check_part_b_impl(coverage_records)


@mcp.tool
def check_active_wound_dx(diagnoses: list[dict]) -> dict:
    """Return whether the patient has an active wound diagnosis."""
    return _check_active_wound_dx_impl(diagnoses)


@mcp.tool
def decide_eligibility(
    wound_fields: dict, has_active_part_b: bool, has_active_wound: bool
) -> dict:
    """Deterministic billing-triage decision with a plain-English reason."""
    return _decide_eligibility_impl(wound_fields, has_active_part_b, has_active_wound)


if __name__ == "__main__":
    mcp.run()
