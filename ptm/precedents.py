"""Record a human ruling, get the set out of the database, and back into another.

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

**And no way to make one, either.** The only routes into
:func:`ptm.store.save_precedent` were the HITL task in
:mod:`ptm_dags.adjudicate`, the simulated reviewer in :mod:`ptm.selftest`, and
the import below - which can only file a judgement somebody else already made.
A team could import two years of their own decisions, replay them, measure the
coverage, export the bundle and export the rulings held against it, and not
record a single ruling without standing up a scheduler. See
:func:`record_precedent`.

    python -m ptm.precedents expenses --rule exp-0042 deny --by finance.lead
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

from . import cli, store
from .config import available_domains, load_domain
from .models import Precedent

#: Stamped into every export and checked on the way back in. A file whose shape
#: this cannot read is refused by name rather than half-imported: a partial
#: precedent set is a regression suite that passes for the wrong reason.
FORMAT = "ptm-precedents/1"


def record_precedent(domain_name: str, case_id: str, outcome: str, ruled_by: str,
                     note: str = "", version: str = "",
                     replace: bool = False) -> dict:
    """Record one human ruling, the way the ``adjudicate`` DAG records a queue of them.

    Until this existed, :func:`ptm.store.save_precedent` had three callers: the
    HITL task in :mod:`ptm_dags.adjudicate`, the simulated reviewer in
    :mod:`ptm.selftest`, and :func:`import_precedents` - which can only file a
    ruling somebody else already made. So the whole non-Airflow path was a
    half-loop. ``manage.py`` will import your history, replay it, check its
    coverage and export a bundle, and ``make history-rulings`` will export the
    rulings held against it, all of which CI exercises; and there was no way to
    *make* one without standing up a scheduler. The claim this project is built
    on is that a human ruling becomes the check the next proposal must face, so
    the act of making one is the last thing that should have needed Airflow.

    **It captures the circumstances, not just the answer.** A ruling that
    records only "finance.lead said deny" cannot be re-read later - see
    :class:`ptm.models.Precedent` and :func:`ptm.diff.stale_precedents`. The DAG
    has the candidate version and its verdict to hand because it just showed
    them to the reviewer; here they are looked up from the recorded flip, so a
    ruling typed at a shell carries the same record as one made in the UI rather
    than a thinner one that quietly reads as stale.

    **It refuses an unknown case.** :func:`import_precedents` deliberately
    accepts rulings for cases this database has not loaded, because receiving a
    regression suite before the history is the normal way round. Making a
    first-hand ruling is the opposite: nobody settles a case they cannot read,
    so a typo in a case id is a mistake rather than an ordering.

    **And it does not overwrite by default**, for the reason the import path
    does not: an answer already on file was given by somebody, and ``--replace``
    is how you say you mean it. The earlier ruling is archived either way.
    """
    domain = load_domain(domain_name)
    ruled_by = (ruled_by or "").strip()
    if not ruled_by:
        raise LookupError(
            "a ruling needs the name of the person who made it. Precedent is the one "
            "output here that cannot be recomputed, and an unattributed one cannot be "
            "asked about later.")
    try:
        outcome = domain.validate_outcome(outcome)
    except ValueError as exc:
        raise LookupError(str(exc)) from exc

    on_file = store.query("SELECT case_id FROM cases WHERE domain=? AND case_id=?",
                          (domain_name, case_id))
    if not on_file:
        raise LookupError(
            f"no case {case_id!r} in {domain_name}. A ruling is a judgement about a "
            f"case somebody read; import the history first, or check the id.")

    # The candidate the reviewer was looking at, and what it gave. Taken from the
    # most recent recorded flip for this case unless the caller named a version,
    # which is what makes a ruling typed here as re-readable as one made in the UI.
    rows = store.query(
        """SELECT policy_version, new_outcome, policy_clause FROM current_flips
           WHERE domain=? AND case_id=?""" + (" AND policy_version=?" if version else "")
        + " ORDER BY rowid DESC LIMIT 1",
        (domain_name, case_id, version) if version else (domain_name, case_id))
    seen = rows[0] if rows else {}
    if version and not rows:
        raise LookupError(
            f"{version} has no recorded change for {case_id}, so there is nothing this "
            f"ruling would be overturning. Replay it first, or drop --version to rule "
            f"on the case as it stands.")

    current = next((p for p in store.load_precedents(domain_name)
                    if p.case_id == case_id), None)
    if current and not replace:
        raise LookupError(
            f"{case_id} has already been ruled {current.correct_outcome!r} by "
            f"{current.ruled_by}. Two people disagreeing is settled by them rather than "
            f"by whoever typed last; --replace takes this answer and archives that one.")

    store.save_precedent(Precedent(
        case_id=case_id, domain=domain_name, correct_outcome=outcome,
        ruled_by=ruled_by, note=note,
        established_at=store.now_utc(),
        # Named the way the DAG names its own runs, so the provenance of a
        # ruling says which door it came in by rather than going blank.
        established_by_run="cli::ptm.precedents",
        policy_version=seen.get("policy_version", "") or version,
        judged_outcome=seen.get("new_outcome", ""),
        judged_clause=seen.get("policy_clause", ""),
    ))
    # The same bookkeeping the DAG does after a ruling: a case that has been
    # settled leaves the review queue, so the next adjudication run does not
    # offer it again.
    if seen.get("policy_version"):
        store.mark_reviewed(domain_name, seen["policy_version"], [case_id])
    return {
        "domain": domain_name, "case_id": case_id, "outcome": outcome,
        "ruled_by": ruled_by, "note": note,
        "replaced": current.correct_outcome if current else "",
        "policy_version": seen.get("policy_version", "") or version,
        "judged_outcome": seen.get("new_outcome", ""),
        "judged_clause": seen.get("policy_clause", ""),
    }


def describe_ruling(result: dict) -> str:
    """One recorded ruling, as the CLI reports it."""
    lines = []
    if result["replaced"]:
        lines.append(f"replaced the earlier ruling of {result['replaced']!r} on "
                     f"{result['case_id']}, which is archived rather than lost")
    against = ""
    if result["policy_version"]:
        against = (f" against {result['policy_version']}, which gave "
                   f"{result['judged_outcome'] or 'no recorded verdict'!r}"
                   + (f" citing clause {result['judged_clause']}"
                      if result["judged_clause"] else ""))
    lines.append(f"{result['ruled_by']} ruled {result['case_id']} "
                 f"{result['outcome']!r}{against}")
    if not result["policy_version"]:
        lines.append("  no replayed version has a recorded change for this case, so the "
                     "ruling carries no verdict it was overturning. It still binds the "
                     "gate; it simply cannot be checked for staleness later.")
    return "\n".join(lines)


def export_precedents(domain_name: str) -> dict:
    """Every ruling for a domain, and every ruling a later one replaced."""
    load_domain(domain_name)  # LookupError-shaped refusal for an unknown domain
    precedents = store.load_precedents(domain_name)
    history = store.precedent_history(domain_name)
    return {
        "format": FORMAT,
        "domain": domain_name,
        # Aware UTC, offset and all. Every timestamp *column* in this project is
        # naive UTC because SQLite sorts them as text and one zone had to be
        # picked; this is not a column, it is a line in a file that leaves the
        # system, and a bare "2026-09-20T03:34:47" in a ruling export is a time
        # nobody downstream can place. It was the machine's local time, too.
        "exported_at": store.now_utc().isoformat(timespec="seconds"),
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

  python -m ptm.precedents <domain> --rule <case_id> <outcome> --by NAME
                                    [--note TEXT] [--version V] [--replace]
        Record one human ruling, without Airflow. The candidate version the
        ruling is against and the verdict it overturns are read from the
        recorded flip, so a ruling typed here carries the same circumstances
        as one made in the review UI. --version pins which candidate; without
        it the most recent recorded change for the case is used.

Precedent is the only output here that cannot be recomputed, which is why it is
the only one worth moving between databases - and why nothing is overwritten
without being asked for twice."""


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0

    # Which arguments are a flag's *value* rather than a flag. Computed first
    # because --note is free text a reviewer wrote: `--note "--replace"` is a
    # legitimate, if odd, note, and a membership test over the raw list would
    # read it as the switch that overwrites somebody else's ruling.
    consumed = _consumed_values(args)
    switches = {arg for index, arg in enumerate(args) if index not in consumed}

    replace = "--replace" in switches
    ruling = "--rule" in switches
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

    # ``--note`` is read with a lookup that takes the next argument whatever it
    # looks like: a reviewer's reason legitimately starts with a dash ("-ve
    # margin on this one"), and _flag treats a leading dash as a missing value.
    note = _value_after(args, "--note")
    ruled_by = _flag(args, "--by")
    for name, value in (("--by", ruled_by), ("--note", note)):
        if value == "":
            print(f"ERROR {name} needs a value\n\n{USAGE}", file=sys.stderr)
            return 2
    pinned = _flag(args, "--version")
    if pinned == "":
        print(f"ERROR --version needs a policy version\n\n{USAGE}", file=sys.stderr)
        return 2

    #: The flags that stand alone. Everything else in ``flags`` swallows the
    #: argument after it, which is what ``skip`` below is counting.
    bare = {"--replace", "--rule"}
    flags = bare | {"--import", "-o", "--out", "--by", "--note", "--version"}
    positional, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in flags:
            skip = arg not in bare
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

    if ruling:
        if importing is not None:
            print(f"ERROR --rule records one ruling and --import merges a file of them; "
                  f"they are different acts\n\n{USAGE}", file=sys.stderr)
            return 2
        if len(positional) != 3:
            print(f"ERROR --rule needs a case id and an outcome: "
                  f"`{domain_name} --rule <case_id> <outcome> --by NAME`\n\n{USAGE}",
                  file=sys.stderr)
            return 2
        try:
            result = record_precedent(domain_name, positional[1], positional[2],
                                      ruled_by or "", note=note or "",
                                      version=pinned or "", replace=replace)
        except LookupError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(describe_ruling(result))
        return 0

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


#: Flags that take the argument after them. ``--replace`` and ``--rule`` stand
#: alone, so nothing following them is swallowed.
_TAKES_A_VALUE = ("--import", "-o", "--out", "--by", "--note", "--version")


def _consumed_values(args: list[str]) -> set[int]:
    """Indices of arguments that are a flag's value rather than a flag."""
    consumed: set[int] = set()
    skip = False
    for index, arg in enumerate(args):
        if skip:
            consumed.add(index)
            skip = False
            continue
        skip = arg in _TAKES_A_VALUE
    return consumed


def _flag(args: list[str], name: str) -> str | None:
    """The value after ``--name``, or None. Empty string if the flag ends the line."""
    if name not in args:
        return None
    index = args.index(name)
    value = args[index + 1] if index + 1 < len(args) else ""
    return "" if value.startswith("-") else value


def _value_after(args: list[str], name: str) -> str | None:
    """Like :func:`_flag`, but a leading dash is part of the value.

    For ``--note`` only. A reviewer's reason is free text they wrote, and
    "-40 GBP under the old band" is a sentence rather than a mistyped flag;
    :func:`_flag`'s dash check would read it as the flag ending the line and
    report that ``--note`` needs a value the caller had just given it.
    """
    if name not in args:
        return None
    index = args.index(name)
    return args[index + 1] if index + 1 < len(args) else ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
