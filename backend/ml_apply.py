"""Apply trained decision tree model to model_insights table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
from sqlalchemy import text

from backend.db.database import SessionLocal, init_db

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "ml" / "models" / "decision_tree.joblib"


def apply_model() -> None:
    if not MODEL_PATH.exists():
        print(f"Model not found at {MODEL_PATH}. Train in Colab first.")
        return

    init_db()
    artifact = joblib.load(MODEL_PATH)
    pipeline = artifact["pipeline"]
    feature_cols = artifact["feature_columns"]

    db = SessionLocal()
    rows = db.execute(
        text(
            """
            SELECT
                e.patient_internal_id,
                e.facility_id,
                e.has_active_medicare_b,
                e.unknown_risk_score,
                e.unknown_flag_count,
                e.note_assessment_conflict,
                e.multiple_eligible_wounds,
                e.envive_narrative_only,
                e.length_cm, e.width_cm, e.depth_cm,
                e.routing_decision,
                (SELECT COUNT(*) FROM diagnoses d WHERE d.patient_internal_id=e.patient_internal_id AND d.clinical_status='active') AS active_dx_count,
                (SELECT COUNT(*) FROM notes n WHERE n.patient_internal_id=e.patient_internal_id) AS note_count,
                (SELECT COUNT(*) FROM assessments a WHERE a.patient_internal_id=e.patient_internal_id) AS assessment_count
            FROM eligibility e
            """
        )
    ).mappings().all()

    import pandas as pd

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        print("No eligibility rows.")
        return

    X = df[feature_cols]
    preds = pipeline.predict(X)
    probas = pipeline.predict_proba(X)[:, 1] if hasattr(pipeline, "predict_proba") else preds

    clf = pipeline.named_steps.get("clf")
    importances = dict(zip(feature_cols, clf.feature_importances_.tolist())) if clf and hasattr(clf, "feature_importances_") else {}

    now = datetime.now(timezone.utc).isoformat()
    for i, row in df.iterrows():
        suggestion = "auto_accept" if preds[i] == 1 else "not_auto_accept"
        rule_agrees = int(
            (suggestion == "auto_accept" and row["routing_decision"] == "auto_accept")
            or (suggestion != "auto_accept" and row["routing_decision"] != "auto_accept")
        )
        db.execute(
            text(
                """
                INSERT INTO model_insights (patient_internal_id, model_suggestion, model_probability, rule_agrees, feature_importances_json, computed_at)
                VALUES (:pi, :ms, :mp, :ra, :fi, :ca)
                ON CONFLICT(patient_internal_id) DO UPDATE SET
                    model_suggestion=excluded.model_suggestion,
                    model_probability=excluded.model_probability,
                    rule_agrees=excluded.rule_agrees,
                    feature_importances_json=excluded.feature_importances_json,
                    computed_at=excluded.computed_at
                """
            ),
            {
                "pi": int(row["patient_internal_id"]),
                "ms": suggestion,
                "mp": float(probas[i]),
                "ra": rule_agrees,
                "fi": json.dumps(importances),
                "ca": now,
            },
        )
    db.commit()
    db.close()
    print(f"Applied model to {len(df)} patients")
