#!/usr/bin/env python3
"""Work with your own history: import, replay, inspect, export, or reclaim disk.

Examples:
  python manage.py import expenses examples/expenses.csv
  python manage.py import expenses examples/expenses.csv --write
  python manage.py replay expenses v2
  python manage.py coverage expenses v2
  python manage.py snapshots expenses v2 -o evidence.json
  python manage.py export expenses v2 -o bundle.json
  python manage.py gate expenses v2 --introduced-only
  python manage.py rulings expenses -o rulings.json
  python manage.py rule expenses exp-0042 deny --by finance.lead --note "..."
  python manage.py evidence expenses v2 --export -o evidence-set.json
  python manage.py evidence expenses v2 --import evidence-set.json
  python manage.py resolve expenses
  python manage.py prune --dry-run

Use --db PATH before the command to select a separate database. It defaults to
include/history.db, which is deliberately not the synthetic demo's
include/ptm.db - see the README.

Everything after the command is passed through to the module behind it, so
`python manage.py export --help` is that module's own help.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

#: command -> (module, flags the command implies). One table, because the
#: alternative was a dict of modules and a conditional per special case, and
#: the second command that needed a flag is what made that obvious.
#:
#: ``export``, ``rulings``, ``resolve`` and ``prune`` are here because the
#: engine has always had them and a real-history database had no supported way
#: to reach them: every Make target hardcodes the demo's PTM_DB, so somebody
#: who imported their own cases could measure them and then not get the bundle
#: out, move the rulings, clear a stranded run or reclaim the disk without
#: setting an environment variable by hand.
COMMANDS: dict[str, tuple[str, list[str]]] = {
    "import": ("ptm.ingest", []),
    "replay": ("ptm.replay", []),
    "coverage": ("ptm.provenance", []),
    "snapshots": ("ptm.provenance", ["--snapshots"]),
    "resolve": ("ptm.provenance", ["--resolve"]),
    # A raw passthrough to the same module, for the two flags that take their
    # own argument: `evidence <domain> <version> --export -o f.json` and
    # `evidence <domain> <version> --import f.json`. The aliases above each
    # imply a flag, which is why the round-trip pair cannot be two more of them.
    "evidence": ("ptm.provenance", []),
    "export": ("ptm.report", []),
    "rulings": ("ptm.precedents", []),
    # Recording a ruling, as against exporting the ones already on file. The
    # implied flag is what lets the command read as `rule <domain> <case>
    # <outcome>` rather than making the caller repeat the module's own switch.
    "rule": ("ptm.precedents", ["--rule"]),
    # The measurement the whole loop is built toward, and the last one that a
    # real-history database could not reach from here: `gate` was reachable
    # only by setting PTM_DB by hand or by triggering the DAG, while every
    # other step of the same workflow - import, replay, coverage, export - had
    # a command. A regression suite you can record rulings into and then not
    # run is half a gate.
    "gate": ("ptm.gate", []),
    "prune": ("ptm.prune", []),
}


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("PTM_DB", str(root / "include/history.db")))
    parser.add_argument("--include", default=os.environ.get("PTM_INCLUDE_DIR", str(root / "include")))
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    module, implied = COMMANDS[args.command]
    environment = {**os.environ, "PTM_DB": str(Path(args.db).resolve()),
                   "PTM_INCLUDE_DIR": str(Path(args.include).resolve())}
    return subprocess.run([sys.executable, "-m", module, *args.arguments, *implied],
                          env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
