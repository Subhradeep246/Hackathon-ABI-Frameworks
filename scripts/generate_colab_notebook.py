"""Generate one-click Colab notebook with embedded features.csv."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
csv_text = (ROOT / "ml/exports/features.csv").read_text()
train_code = (ROOT / "ml/train_model.py").read_text()
train_inline = train_code.split('if __name__ == "__main__":')[0]

csv_escaped = csv_text.replace('"""', '\\"\\"\\"')

one_cell = f'''# One-click train — no uploads needed
!pip install -q pandas scikit-learn joblib

from pathlib import Path

FEATURES_CSV = """{csv_escaped}"""

Path("features.csv").write_text(FEATURES_CSV)

{train_inline}

summary = train("features.csv", "decision_tree.joblib")

from google.colab import files
files.download("decision_tree.joblib")
files.download("decision_tree.summary.json")
print("Done — save decision_tree.joblib to Pulse/ml/models/")
'''

nb = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "accelerator": "GPU",
    },
    "cells": [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# ABI Wound Care — One-Click Train (A100)\n",
                "\n",
                "**Runtime → A100 GPU**, then **Run this single cell**. No uploads.\n",
            ],
        },
        {
            "cell_type": "code",
            "metadata": {},
            "source": [line + "\n" for line in one_cell.split("\n")],
            "execution_count": None,
            "outputs": [],
        },
    ],
}

out = ROOT / "ml" / "train_colab_oneclick.ipynb"
out.write_text(json.dumps(nb, indent=1))
print(f"Wrote {out}")
