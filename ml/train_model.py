"""
ABI Wound Care — Decision Tree Training Script (Colab-ready)

Usage in Google Colab:
  1. Upload `features.csv` (export via: python backend/cli.py export-features)
  2. Run all cells /: python ml/train_model.py --features features.csv --out decision_tree.joblib
  3. Download decision_tree.joblib and place in ml/models/
  4. Restart the API to load model insights

This script trains a shallow decision tree on rule-derived labels.
The tree is advisory only — it does not override hard eligibility rules.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.tree import DecisionTreeClassifier

FEATURE_COLUMNS = [
    "facility_id",
    "has_active_medicare_b",
    "unknown_risk_score",
    "unknown_flag_count",
    "note_assessment_conflict",
    "multiple_eligible_wounds",
    "envive_narrative_only",
    "length_cm",
    "width_cm",
    "depth_cm",
    "active_dx_count",
    "note_count",
    "assessment_count",
]

TARGET_COLUMN = "target_auto_accept"


def build_target(df: pd.DataFrame) -> pd.Series:
    """Binary target: 1 if rules engine said auto_accept, else 0."""
    return (df["routing_decision"] == "auto_accept").astype(int)


def train(features_path: str, out_path: str) -> dict:
    df = pd.read_csv(features_path)
    if df.empty:
        raise ValueError("Feature CSV is empty. Run sync → extract → decide → export-features first.")

    df[TARGET_COLUMN] = build_target(df)

    X = df[FEATURE_COLUMNS].copy()
    y = df[TARGET_COLUMN]

    numeric_features = [
        "facility_id",
        "has_active_medicare_b",
        "unknown_risk_score",
        "unknown_flag_count",
        "note_assessment_conflict",
        "multiple_eligible_wounds",
        "envive_narrative_only",
        "length_cm",
        "width_cm",
        "depth_cm",
        "active_dx_count",
        "note_count",
        "assessment_count",
    ]

    preprocessor = ColumnTransformer(
        transformers=[
            (
                "num",
                Pipeline([
                    ("imputer", SimpleImputer(strategy="median")),
                ]),
                numeric_features,
            )
        ]
    )

    tree = Pipeline([
        ("prep", preprocessor),
        (
            "clf",
            DecisionTreeClassifier(
                max_depth=4,
                min_samples_leaf=15,
                class_weight="balanced",
                random_state=42,
            ),
        ),
    ])

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y if y.nunique() > 1 else None)

    cv_scores = cross_val_score(tree, X, y, cv=min(5, len(df)), scoring="accuracy")
    tree.fit(X_train, y_train)
    y_pred = tree.predict(X_test)

    report = classification_report(y_test, y_pred, output_dict=True, zero_division=0)

    # Logistic baseline
    log_reg = Pipeline([
        ("prep", preprocessor),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])
    log_reg.fit(X_train, y_train)
    log_acc = log_reg.score(X_test, y_test)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "pipeline": tree,
        "feature_columns": FEATURE_COLUMNS,
        "cv_accuracy_mean": float(cv_scores.mean()),
        "cv_accuracy_std": float(cv_scores.std()),
        "test_accuracy": float(tree.score(X_test, y_test)),
        "logistic_baseline_accuracy": float(log_acc),
        "classification_report": report,
    }
    joblib.dump(artifact, out_path)

    # Feature importances from fitted tree
    clf: DecisionTreeClassifier = tree.named_steps["clf"]
    importances = dict(zip(FEATURE_COLUMNS, clf.feature_importances_.tolist()))

    summary = {
        "model_path": out_path,
        "rows": len(df),
        "cv_accuracy": artifact["cv_accuracy_mean"],
        "test_accuracy": artifact["test_accuracy"],
        "logistic_baseline": artifact["logistic_baseline_accuracy"],
        "feature_importances": importances,
    }
    summary_path = Path(out_path).with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", default="ml/exports/features.csv")
    parser.add_argument("--out", default="ml/models/decision_tree.joblib")
    args = parser.parse_args()
    train(args.features, args.out)


if __name__ == "__main__":
    main()
