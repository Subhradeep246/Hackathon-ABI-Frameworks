"""Eligibility rules and routing decisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

WOUND_ICD_PREFIXES = (
    "L89.",
    "E11.621",
    "E10.621",
    "I83.0",
    "I70.",
    "T81.4",
    "T81.3",
    "L02.",
    "T20",
    "T21",
    "T22",
    "T23",
    "T24",
    "T25",
    "T26",
    "T27",
    "T28",
    "T29",
    "T30",
    "T31",
    "T32",
)

STAGE_RANK = {"2": 2, "3": 3, "4": 4, "unstageable": 5}


@dataclass
class EligibilityResult:
    routing_decision: str = "flag_for_review"
    routing_reason: str = ""
    has_active_medicare_b: bool = False
    other_active_payers: list[str] = field(default_factory=list)
    unknown_risk_score: int = 0
    unknown_risk_tier: str = "green"
    unknown_flag_count: int = 0
    note_assessment_conflict: bool = False
    multiple_eligible_wounds: bool = False
    envive_narrative_only: bool = False
    primary_wound: dict | None = None


def is_wound_icd(code: str | None) -> bool:
    if not code:
        return False
    return any(code.upper().startswith(p.upper()) for p in WOUND_ICD_PREFIXES)


def has_active_medicare_b(coverage_rows: list[dict]) -> tuple[bool, list[str]]:
    today = datetime.now(timezone.utc).date()
    has_mcb = False
    others: list[str] = []
    for row in coverage_rows:
        payer_code = (row.get("payer_code") or "").upper()
        effective_to = row.get("effective_to")
        active = effective_to is None or effective_to == ""
        if not active:
            try:
                end = datetime.fromisoformat(str(effective_to).replace("Z", "+00:00")).date()
                active = end >= today
            except ValueError:
                active = False
        if not active:
            continue
        if payer_code == "MCB":
            has_mcb = True
        else:
            name = row.get("payer_name") or row.get("payer_code") or "unknown"
            others.append(str(name))
    return has_mcb, others


def has_active_wound_diagnosis(diagnoses: list[dict]) -> bool:
    for d in diagnoses:
        if (d.get("clinical_status") or "").lower() != "active":
            continue
        if is_wound_icd(d.get("icd10_code")):
            return True
    return False


def wound_score(w: dict) -> tuple[int, int]:
    stage = str(w.get("stage") or "")
    rank = STAGE_RANK.get(stage.lower().replace("stage", "").strip(), 0)
    completeness = sum(
        1
        for k in ("length_cm", "width_cm", "depth_cm", "drainage_amount", "wound_type", "location")
        if w.get(k) is not None
    )
    return rank, completeness


def pick_primary_wound(wounds: list[dict]) -> tuple[dict | None, bool]:
    if not wounds:
        return None, False
    scored = sorted(wounds, key=lambda w: wound_score(w), reverse=True)
    top = scored[0]
    multiple = len(scored) > 1 and wound_score(scored[0]) == wound_score(scored[1])
    return top, multiple


def compute_unknown_risk(flags: list[dict], wound: dict | None) -> tuple[int, str]:
    score = 0
    weights = {"low": 5, "medium": 15, "high": 25}
    for f in flags:
        score += weights.get(f.get("severity", "medium"), 15)
    if wound:
        for field in (
            "location_status",
            "wound_type_status",
            "stage_status",
            "length_cm_status",
            "width_cm_status",
            "depth_cm_status",
            "drainage_amount_status",
        ):
            st = wound.get(field, "unknown_missing")
            if st == "unknown_unparseable":
                score += 10
            elif st == "unknown_conflict":
                score += 20
            elif st == "unknown_missing":
                score += 5
    score = min(score, 100)
    if score >= 50:
        tier = "red"
    elif score >= 20:
        tier = "yellow"
    else:
        tier = "green"
    return score, tier


def critical_fields_known(wound: dict | None) -> bool:
    if not wound:
        return False
    critical = (
        "wound_type_status",
        "length_cm_status",
        "width_cm_status",
        "depth_cm_status",
        "drainage_amount_status",
    )
    return all(wound.get(f) == "known" for f in critical)


def decide(
    diagnoses: list[dict],
    coverage: list[dict],
    wounds: list[dict],
    flags: list[dict],
    envive_only: bool = False,
    note_assessment_conflict: bool = False,
) -> EligibilityResult:
    mcb, others = has_active_medicare_b(coverage)
    active_wound_dx = has_active_wound_diagnosis(diagnoses)
    primary, multiple = pick_primary_wound(wounds)
    risk_score, risk_tier = compute_unknown_risk(flags, primary)

    result = EligibilityResult(
        has_active_medicare_b=mcb,
        other_active_payers=others,
        unknown_risk_score=risk_score,
        unknown_risk_tier=risk_tier,
        unknown_flag_count=len(flags),
        note_assessment_conflict=note_assessment_conflict,
        multiple_eligible_wounds=multiple,
        envive_narrative_only=envive_only,
        primary_wound=primary,
    )

    if not mcb:
        result.routing_decision = "reject"
        result.routing_reason = "No active Medicare Part B coverage."
        return result

    if not active_wound_dx and not primary:
        result.routing_decision = "reject"
        result.routing_reason = "No active wound diagnosis or documented wound in notes/assessments."
        return result

    if primary:
        missing = []
        if primary.get("length_cm") is None or primary.get("width_cm") is None:
            missing.append("measurements")
        if primary.get("depth_cm") is None:
            missing.append("depth")
        if not primary.get("drainage_amount"):
            missing.append("drainage")
        if missing:
            result.routing_decision = "flag_for_review"
            result.routing_reason = (
                f"Active wound documented but missing {', '.join(missing)} — clinician should verify."
            )
            return result

    if note_assessment_conflict or multiple or envive_only or risk_tier == "red":
        result.routing_decision = "flag_for_review"
        reasons = []
        if note_assessment_conflict:
            reasons.append("note vs assessment conflict")
        if multiple:
            reasons.append("multiple eligible wounds")
        if envive_only:
            reasons.append("Envive narrative only")
        if risk_tier == "red":
            reasons.append("high unknown risk")
        result.routing_reason = "Flagged for review: " + "; ".join(reasons) + "."
        return result

    if primary and critical_fields_known(primary):
        wt = (primary.get("wound_type") or "wound").replace("_", " ")
        loc = primary.get("location") or "unspecified location"
        stage = primary.get("stage")
        stage_txt = f" stage {stage}" if stage else ""
        result.routing_decision = "auto_accept"
        result.routing_reason = (
            f"Active {wt}{stage_txt} at {loc} with complete measurements and active Medicare B coverage."
        )
        return result

    result.routing_decision = "flag_for_review"
    result.routing_reason = "Clinical criteria appear met but some fields are incomplete or ambiguous."
    return result
