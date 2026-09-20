#!/usr/bin/env python3
"""Work with your own history: import, replay, inspect coverage, or export snapshots.

Examples:
  python manage.py import expenses examples/expenses.csv
  python manage.py import expenses examples/expenses.csv --write
  python manage.py replay expenses v2
  python manage.py coverage expenses v2
  python manage.py snapshots expenses v2 -o evidence.json

Use --db PATH before the command to select a separate database.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("PTM_DB", str(root / "include/history.db")))
    parser.add_argument("--include", default=os.environ.get("PTM_INCLUDE_DIR", str(root / "include")))
    parser.add_argument("command", choices=["import", "replay", "coverage", "snapshots"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    module = {"import": "ptm.ingest", "replay": "ptm.replay",
              "coverage": "ptm.provenance", "snapshots": "ptm.provenance"}[args.command]
    environment = {**os.environ, "PTM_DB": str(Path(args.db).resolve()),
                   "PTM_INCLUDE_DIR": str(Path(args.include).resolve())}
    return subprocess.run([sys.executable, "-m", module, *args.arguments,
                           *(["--snapshots"] if args.command == "snapshots" else [])],
                          env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
