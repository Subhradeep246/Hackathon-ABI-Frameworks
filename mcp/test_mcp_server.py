"""Unit tests for the MCP tool logic (pure _impl functions)."""

import json

import mcp_server as m


# --- extract_from_assessment -------------------------------------------------
def test_assessment_complete_is_high_confidence():
    raw = json.dumps({
        "wound_type": "pressure_ulcer", "stage": 2, "location": "Sacrum",
        "length_cm": 3.2, "width_cm": 2.1, "depth_cm": 0.4,
        "drainage_type": "serosanguineous", "drainage_amount": "moderate",
    })
    r = m._extract_from_assessment_impl(raw)
    assert r["source"] == "assessment"
    assert r["confidence"] == "high"
    assert r["missing"] == []
    assert r["length_cm"] == 3.2 and r["drainage_amount"] == "moderate"


def test_assessment_missing_depth_is_medium():
    raw = json.dumps({
        "wound_type": "abscess", "length_cm": 1.0, "width_cm": 1.0,
        "drainage_amount": "light",
    })
    r = m._extract_from_assessment_impl(raw)
    assert r["confidence"] == "medium"
    assert "depth_cm" in r["missing"]


def test_assessment_bad_json_is_all_missing():
    r = m._extract_from_assessment_impl("{not json")
    assert r["confidence"] == "medium"
    assert set(m.REQUIRED_FIELDS).issubset(set(r["missing"]))


# --- extract_from_note -------------------------------------------------------
def test_note_labeled_measurements():
    text = ("Wound Type: Pressure Ulcer, Stage 2\nLocation: Sacrum\n"
            "Length: 3.2 cm  Width: 2.1 cm  Depth: 0.4 cm\nDrainage: Moderate")
    r = m._extract_from_note_impl(text)
    assert r["source"] == "note"
    assert (r["length_cm"], r["width_cm"], r["depth_cm"]) == (3.2, 2.1, 0.4)
    assert r["drainage_amount"] == "moderate"
    assert r["confidence"] == "medium"
    assert r["missing"] == []


def test_note_shorthand_measurements():
    text = "DFU on heel. Meas 4.2x3.1x1.5 cm, scant drainage."
    r = m._extract_from_note_impl(text)
    assert (r["length_cm"], r["width_cm"], r["depth_cm"]) == (4.2, 3.1, 1.5)
    assert r["drainage_amount"] == "light"
    assert r["wound_type"] == "diabetic foot ulcer"


def test_note_incomplete_is_low_confidence():
    r = m._extract_from_note_impl("Patient seen for wound check. No measurements.")
    assert r["confidence"] == "low"
    assert r["missing"]


def test_note_multiwound_picks_more_severe():
    text = ("Two wounds noted. Stage 4 pressure ulcer on sacrum. "
            "Also a small abscess. Length: 5 Width: 4 Depth: 2. Drainage: heavy")
    r = m._extract_from_note_impl(text)
    assert r["wound_type"] == "pressure_ulcer"  # stage 4 outranks abscess


# --- check_part_b ------------------------------------------------------------
def test_part_b_active():
    recs = [{"payer_code": "MCB", "payer_type": "Medicare B", "effective_to": None}]
    assert m._check_part_b_impl(recs) == {"has_active_part_b": True, "payer": "MCB"}


def test_part_b_terminated_is_false():
    recs = [{"payer_code": "MCB", "effective_to": "2025-01-01T00:00:00"}]
    assert m._check_part_b_impl(recs)["has_active_part_b"] is False


def test_part_b_other_payer_is_false():
    recs = [{"payer_code": "HMO", "effective_to": None}]
    assert m._check_part_b_impl(recs)["has_active_part_b"] is False


# --- check_active_wound_dx ---------------------------------------------------
def test_wound_dx_by_prefix():
    dx = [{"icd10_code": "L89.152", "clinical_status": "active",
           "icd10_description": "Pressure ulcer of sacral region, stage 2"}]
    assert m._check_active_wound_dx_impl(dx)["has_active_wound"] is True


def test_wound_dx_by_burn_range():
    dx = [{"icd10_code": "T24.30", "clinical_status": "active",
           "icd10_description": "Burn of unspecified degree of lower limb"}]
    assert m._check_active_wound_dx_impl(dx)["has_active_wound"] is True


def test_wound_dx_by_description_keyword():
    dx = [{"icd10_code": "Z99.9", "clinical_status": "active",
           "icd10_description": "Chronic venous ulcer, left leg"}]
    assert m._check_active_wound_dx_impl(dx)["has_active_wound"] is True


def test_wound_dx_resolved_is_false():
    dx = [{"icd10_code": "L89.152", "clinical_status": "resolved",
           "icd10_description": "Pressure ulcer"}]
    assert m._check_active_wound_dx_impl(dx)["has_active_wound"] is False


def test_wound_dx_non_wound_is_false():
    dx = [{"icd10_code": "E11.9", "clinical_status": "active",
           "icd10_description": "Type 2 diabetes without complications"}]
    assert m._check_active_wound_dx_impl(dx)["has_active_wound"] is False


# --- decide_eligibility (rule table) -----------------------------------------
COMPLETE = {
    "length_cm": 3.2, "width_cm": 2.1, "depth_cm": 0.4,
    "drainage_amount": "moderate", "confidence": "high", "missing": [],
}


def test_decide_rule1_no_part_b():
    r = m._decide_eligibility_impl(COMPLETE, False, True)
    assert r["decision"] == "reject" and r["rule_fired"] == "no_part_b"


def test_decide_rule2_no_wound():
    r = m._decide_eligibility_impl(COMPLETE, True, False)
    assert r["decision"] == "reject" and r["rule_fired"] == "no_active_wound"


def test_decide_rule3_missing_flags_not_rejects():
    wf = dict(COMPLETE, depth_cm=None, missing=["depth_cm"], confidence="medium")
    r = m._decide_eligibility_impl(wf, True, True)
    assert r["decision"] == "flag_for_review"
    assert r["rule_fired"] == "incomplete_measurements"


def test_decide_rule4_low_confidence_flags():
    wf = dict(COMPLETE, confidence="low")
    r = m._decide_eligibility_impl(wf, True, True)
    assert r["decision"] == "flag_for_review" and r["rule_fired"] == "low_confidence"


def test_decide_rule5_auto_accept():
    r = m._decide_eligibility_impl(COMPLETE, True, True)
    assert r["decision"] == "auto_accept" and r["rule_fired"] == "complete"
    assert "Safe to bill" in r["reason"]
