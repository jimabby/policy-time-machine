"""Get the precedent set out of the database, and back into another one.

Precedents are the only durable output of this project. Everything else - every
verdict, flip, aggregate, curve and cost figure - is recomputable from the cases
and the policy, which is why :func:`ptm.store.clear_domain_results` drops all of
it and keeps these. Rulings are the thing a re-seed must survive, the thing
retention must never touch, and the thing the gate is built to protect.

And there was no way to move them. A team that wanted to carry its regression
suite from a staging environment to production, or to check it into version
control beside the policy it guards, had one option: copy the whole SQLite file,
cases and cache and all. ``ptm.report --csv`` exports flips, which are the
recomputable half.

    python -m ptm.precedents expenses -o rulings.json
    python -m ptm.precedents expenses --import rulings.json
    python -m ptm.precedents expenses --import rulings.json --replace

**The archive travels with them.** A ruling that replaced an earlier one is only
half the record; ``precedent_history`` holds what it replaced and why, and an
export that dropped it would move a regression suite while losing the reason it
says what it says.

**Importing does not overwrite by default.** A ruling already on file was made
by somebody here, and silently replacing it with one made somewhere else is the
one operation this project has spent its whole design refusing to do quietly.
``--replace`` is the flag that means it, and even then the ruling it replaces is
archived rather than lost, because the import goes through
:func:`ptm.store.save_precedent` like every other write.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime

from . import cli, store
from .config import available_domains, load_domain
from .models import Precedent

#: Stamped into every export and checked on the way back in. A file whose shape
#: this cannot read is refused by name rather than half-imported: a partial
#: precedent set is a regression suite that passes for the wrong reason.
FORMAT = "ptm-precedents/1"


def export_precedents(domain_name: str) -> dict:
    """Every ruling for a domain, and every ruling a later one replaced."""
    load_domain(domain_name)  # LookupError-shaped refusal for an unknown domain
    precedents = store.load_precedents(domain_name)
    history = store.precedent_history(domain_name)
    return {
        "format": FORMAT,
        "domain": domain_name,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "precedents": [p.model_dump(mode="json") for p in precedents],
        "superseded": history,
        "note": "The only durable output of a Policy Time Machine. Each entry is one "
                "case a named person looked at and settled, with the policy version "
                "they were shown and the verdict they were overturning. 'superseded' "
                "holds rulings a later ruling on the same case replaced - kept because "
                "whether an earlier answer still holds is a judgement somebody made, "
                "not a fact the gate can recompute.",
    }


def import_precedents(domain_name: str, payload: dict,
                      replace: bool = False) -> dict:
    """Merge an exported precedent set into this database.

    Returns what happened per case rather than a count, because the interesting
    entries are the ones that did *not* apply: a ruling already on file that
    disagrees with the incoming one is a conflict between two humans in two
    environments, and it is settled by them rather than by whichever import ran
    last.
    """
    if payload.get("format") != FORMAT:
        raise LookupError(
            f"this file says its format is {payload.get('format')!r}, not {FORMAT!r}. "
            f"Refusing to guess: a precedent set read wrongly is a regression suite "
            f"that passes for the wrong reason.")
    came_from = payload.get("domain", "")
    if came_from and came_from != domain_name:
        raise LookupError(
            f"this file holds {came_from!r} rulings and you asked to import them into "
            f"{domain_name!r}. Case ids are per domain, so these would be filed against "
            f"cases that are not the ones they were made about.")

    domain = load_domain(domain_name)
    existing = {p.case_id: p for p in store.load_precedents(domain_name)}
    on_file = {row["case_id"] for row in store.query(
        "SELECT case_id FROM cases WHERE domain=?", (domain_name,))}

    added, replaced, agreed, conflicted, unknown_case, rejected = [], [], [], [], [], []
    for row in payload.get("precedents", []):
        try:
            incoming = Precedent(**{**row, "domain": domain_name})
            domain.validate_outcome(incoming.correct_outcome)
        except (TypeError, ValueError) as exc:
            rejected.append({"case_id": row.get("case_id", "?"), "why": str(exc)})
            continue
        if incoming.case_id not in on_file:
            # Imported anyway: the ruling is a fact about a case, and a database
            # that has not been seeded yet is the normal way to receive one.
            # Named, because the gate refuses to run against a precedent whose
            # case it cannot load, and finding that out there is worse.
            unknown_case.append(incoming.case_id)
        current = existing.get(incoming.case_id)
        if current is None:
            store.save_precedent(incoming)
            added.append(incoming.case_id)
            continue
        if current.correct_outcome == incoming.correct_outcome:
            agreed.append(incoming.case_id)
            continue
        if not replace:
            conflicted.append({
                "case_id": incoming.case_id,
                "here": current.correct_outcome, "here_ruled_by": current.ruled_by,
                "incoming": incoming.correct_outcome,
                "incoming_ruled_by": incoming.ruled_by,
            })
            continue
        store.save_precedent(incoming)
        replaced.append(incoming.case_id)

    history = [{**row, "domain": domain_name} for row in payload.get("superseded", [])]
    archived = store.import_precedent_history(history)
    return {
        "domain": domain_name,
        "added": sorted(added),
        "replaced": sorted(replaced),
        # Already on file and saying the same thing. Not an error and not a
        # change: re-importing an export is meant to be a no-op.
        "already_agreed": sorted(agreed),
        "conflicted": sorted(conflicted, key=lambda r: r["case_id"]),
        "no_case_on_file": sorted(unknown_case),
        "rejected": rejected,
        "archived_rulings": archived,
    }


def describe_export(payload: dict, path: str = "") -> str:
    where = f" to {path}" if path else ""
    return (f"exported {len(payload['precedents'])} ruling(s) and "
            f"{len(payload['superseded'])} superseded one(s) for "
            f"{payload['domain']}{where}")


def describe_import(result: dict) -> str:
    """What an import did, with the part that did not apply first."""
    lines = []
    if result["conflicted"]:
        lines.append(f"{len(result['conflicted'])} ruling(s) NOT imported - this database "
                     f"already has a different answer for the same case, and two people "
                     f"disagreeing is settled by them rather than by whichever import "
                     f"ran last:")
        for row in result["conflicted"]:
            lines.append(f"  {row['case_id']}: here {row['here']!r} "
                         f"({row['here_ruled_by']}), incoming {row['incoming']!r} "
                         f"({row['incoming_ruled_by']}). --replace takes the incoming "
                         f"one and archives this one.")
    for row in result["rejected"]:
        lines.append(f"  REJECTED {row['case_id']}: {row['why']}")
    lines.append(f"imported {len(result['added'])} new ruling(s), replaced "
                 f"{len(result['replaced'])}, left {len(result['already_agreed'])} that "
                 f"already said the same thing, and merged {result['archived_rulings']} "
                 f"superseded one(s)")
    if result["no_case_on_file"]:
        lines.append(f"  {len(result['no_case_on_file'])} ruling(s) have no case in this "
                     f"database: {result['no_case_on_file'][:10]}. They are recorded, but "
                     f"the gate refuses to run until the cases they are about are loaded.")
    return "\n".join(lines)


USAGE = """usage:
  python -m ptm.precedents <domain> [-o FILE]
        Export every human ruling for a domain, with the rulings a later one
        replaced. Writes to stdout unless -o names a file.

  python -m ptm.precedents <domain> --import FILE [--replace]
        Merge an exported set in. A case this database has already ruled on
        differently is reported and skipped; --replace takes the incoming
        answer and archives the one it replaced.

Precedent is the only output here that cannot be recomputed, which is why it is
the only one worth moving between databases - and why nothing is overwritten
without being asked for twice."""


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0

    replace = "--replace" in args
    importing = _flag(args, "--import")
    # Not `_flag(-o) or _flag(--out)`: "" means the flag was given with nothing
    # after it, and `or` collapses that into the second lookup and then into
    # None, which means stdout. See the same fix in ptm.report.
    out_path = _flag(args, "-o")
    if out_path is None:
        out_path = _flag(args, "--out")
    if out_path == "":
        print(f"ERROR -o needs a file to write to\n\n{USAGE}", file=sys.stderr)
        return 2

    flags = {"--replace", "--import", "-o", "--out"}
    positional, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in flags:
            skip = arg != "--replace"
            continue
        if arg.startswith("-"):
            print(f"ERROR unknown option {arg!r}\n\n{USAGE}", file=sys.stderr)
            return 2
        positional.append(arg)

    if not positional:
        print(f"ERROR name a domain; have {available_domains()}\n\n{USAGE}",
              file=sys.stderr)
        return 2
    domain_name = positional[0]

    store.init_db()
    try:
        load_domain(domain_name)
    except FileNotFoundError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    if importing is not None:
        if not importing:
            print(f"ERROR --import needs a file to read\n\n{USAGE}", file=sys.stderr)
            return 2
        try:
            payload = json.loads(pathlib.Path(importing).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"ERROR cannot read {importing}: {exc}", file=sys.stderr)
            return 2
        try:
            result = import_precedents(domain_name, payload, replace=replace)
        except LookupError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(describe_import(result))
        # Non-zero on an unresolved conflict: an import that silently left half
        # the file on the floor is the failure this whole module is shaped
        # around, and a shell that checks the status should see it.
        return 1 if result["conflicted"] or result["rejected"] else 0

    payload = export_precedents(domain_name)
    body = json.dumps(payload, indent=2, default=str)
    if out_path:
        pathlib.Path(out_path).write_text(body, encoding="utf-8")
        print(describe_export(payload, out_path), file=sys.stderr)
    else:
        print(body)
    return 0


def _flag(args: list[str], name: str) -> str | None:
    """The value after ``--name``, or None. Empty string if the flag ends the line."""
    if name not in args:
        return None
    index = args.index(name)
    value = args[index + 1] if index + 1 < len(args) else ""
    return "" if value.startswith("-") else value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
