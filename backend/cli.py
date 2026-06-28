#!/usr/bin/env python3
"""CLI entrypoint for the wound care pipeline."""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from backend.pipeline import export_features_csv, run_decide, run_extract, run_sync  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="ABI Wound Care Pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Initialize database schema")
    p_sync = sub.add_parser("sync", help="Sync data from PCC API")
    p_sync.add_argument("--since", default=None, help="Incremental sync timestamp")

    sub.add_parser("extract", help="Extract wound data from notes/assessments")
    sub.add_parser("decide", help="Run eligibility rules")
    sub.add_parser("pipeline", help="Run sync + extract + decide")

    p_export = sub.add_parser("export-features", help="Export ML feature CSV")
    p_export.add_argument("--out", default="ml/exports/features.csv")

    sub.add_parser("apply-model", help="Apply trained decision tree to model_insights")

    args = parser.parse_args()

    if args.cmd == "init-db":
        from backend.db.database import init_db

        init_db()
        print("Database initialized")
    elif args.cmd == "sync":
        asyncio.run(run_sync(since=args.since))
    elif args.cmd == "extract":
        run_extract()
    elif args.cmd == "decide":
        run_decide()
    elif args.cmd == "pipeline":
        from backend.pipeline import get_incremental_since

        since = get_incremental_since()
        asyncio.run(run_sync(since=since))
        run_extract()
        run_decide()
    elif args.cmd == "export-features":
        export_features_csv(args.out)
    elif args.cmd == "apply-model":
        from backend.ml_apply import apply_model

        apply_model()


if __name__ == "__main__":
    main()
