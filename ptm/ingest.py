"""Preview and import historical cases from CSV or JSON. Nothing is written without --write."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from string import Formatter

from . import store
from .config import load_domain

CORE = {"case_id", "subject_id", "decided_at", "actual_outcome", "actual_rationale", "domain", "payload"}


def timestamp(value: str) -> str:
    """ISO 8601; offsets become UTC, naive input is explicitly treated as UTC."""
    return store._bound(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def read(path: Path) -> tuple[list, list]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream)), []
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, list):
        return value, []
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        raise ValueError("JSON must be a case array or an object containing a cases array")
    facts = value.get("facts", [])
    if not isinstance(facts, list):
        raise ValueError("facts must be an array")
    return value["cases"], facts


def import_cases(domain_name: str, rows: list, facts: list | None = None,
                 mapping: dict | None = None, duplicates: str = "error", write: bool = False) -> dict:
    """Validate the entire batch before a single transaction; never silently replace cases."""
    if duplicates not in {"error", "skip"}:
        raise ValueError("duplicates must be error or skip")
    domain = load_domain(domain_name)
    mapping = mapping or {}
    required = {field for _, field, _, _ in Formatter().parse(domain.case_template) if field}
    accepted, rejected, skipped, warnings, fact_rows = [], [], [], [], []
    seen = set()
    with store.conn() as db:
        if write:
            db.execute("BEGIN IMMEDIATE")
        existing = {r["case_id"] for r in db.execute("SELECT case_id FROM cases")}
        for number, raw in enumerate(facts or [], 1):
            try:
                subject = str(raw["subject_id"]).strip()
                key = str(raw["key"]).strip()
                if not subject or not key:
                    raise ValueError("fact subject_id and key are required")
                known = timestamp(raw["known_from"])
                value = str(raw["value"])
                old = db.execute("SELECT value FROM subject_facts WHERE subject_id=? AND key=? AND known_from=?",
                                 (subject, key, known)).fetchone()
                if old and old["value"] != value:
                    raise ValueError("fact conflicts with an existing historical fact")
                candidate = (subject, key, value, known)
                conflicting = [f for f in fact_rows if (f[0], f[1], f[3]) == (subject, key, known)]
                if conflicting and conflicting[0] != candidate:
                    raise ValueError("conflicting facts in this file")
                fact_rows.append(candidate)
            except (KeyError, ValueError, TypeError) as exc:
                rejected.append({"row": number, "kind": "fact", "reason": str(exc)})
        for number, raw in enumerate(rows, 1):
            try:
                if not isinstance(raw, dict):
                    raise ValueError("case must be an object")
                row = dict(raw)
                for target, source in mapping.items():
                    if source not in raw:
                        raise ValueError(f"missing mapped column {source!r}")
                    row[target] = raw[source]
                case_id = str(row.get("case_id") or "").strip()
                subject = str(row.get("subject_id") or "").strip()
                if not case_id or not subject:
                    raise ValueError("case_id and subject_id are required")
                if row.get("domain", domain_name) != domain_name:
                    raise ValueError("case belongs to a different domain")
                if case_id in existing or case_id in seen:
                    if duplicates == "skip":
                        skipped.append({"row": number, "case_id": case_id})
                        continue
                    raise ValueError(f"duplicate case_id {case_id!r}; IDs are globally unique")
                decided = timestamp(row["decided_at"])
                outcome = domain.validate_outcome(row["actual_outcome"])
                payload = row.get("payload", {})
                if isinstance(payload, str):
                    payload = json.loads(payload or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("payload must be a JSON object")
                payload = dict(payload)
                used = set(mapping.values())
                payload.update({k.removeprefix("payload."): v for k, v in row.items()
                                if k not in CORE and (k not in used or k in mapping)})
                payload.setdefault("case_id", case_id)
                hydrated = dict(payload)
                subject_facts = [tuple(f) for f in db.execute(
                    "SELECT subject_id,key,value,known_from FROM subject_facts WHERE subject_id=? AND known_from<=?",
                    (subject, decided))] + [f for f in fact_rows if f[0] == subject and f[3] <= decided]
                for _, key, value, _ in sorted(subject_facts, key=lambda f: f[3]):
                    hydrated[key] = value
                missing = sorted(required - set(hydrated))
                if missing:
                    raise ValueError(f"missing fields used by the case template: {', '.join(missing)}")
                encoded = json.dumps(payload, allow_nan=False)
                if len(str(row["decided_at"])) <= 10 or datetime.fromisoformat(
                        str(row["decided_at"]).replace("Z", "+00:00")).tzinfo is None:
                    warnings.append({"row": number, "reason": "naive date interpreted as UTC"})
                accepted.append((case_id, domain_name, subject, decided, encoded,
                                 outcome, str(row.get("actual_rationale") or "")))
                seen.add(case_id)
            except (KeyError, ValueError, TypeError) as exc:
                rejected.append({"row": number, "kind": "case", "reason": str(exc)})
        if write and not rejected:
            # A competing import causes a constraint failure and rolls back the whole batch.
            db.executemany("INSERT INTO cases VALUES (?,?,?,?,?,?,?)", accepted)
            for subject, key, value, known in fact_rows:
                db.execute("INSERT OR IGNORE INTO subject_facts VALUES (?,?,?,?)", (subject, key, value, known))
    return {"domain": domain_name, "preview": not write, "accepted": len(accepted),
            "facts": len(fact_rows), "written": len(accepted) if write and not rejected else 0,
            "committed": write and not rejected, "skipped": skipped,
            "rejected": rejected, "warnings": warnings,
            "note": "Dates are stored in UTC. No cases or facts are written if any row is rejected. "
                    "Subject IDs must identify the same subject across domains."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ptm.ingest", description=__doc__)
    parser.add_argument("domain")
    parser.add_argument("file", type=Path)
    parser.add_argument("--map", action="append", default=[], metavar="TARGET=SOURCE")
    parser.add_argument("--duplicates", choices=["error", "skip"], default="error")
    parser.add_argument("--write", action="store_true")
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    store.init_db()
    try:
        mapping = dict(item.split("=", 1) for item in args.map)
        rows, facts = read(args.file)
        result = import_cases(args.domain, rows, facts, mapping, args.duplicates, args.write)
        print(json.dumps(result, indent=2))
        return 1 if result["rejected"] else 0
    except (ValueError, LookupError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
