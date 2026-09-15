"""Drop the bulk rows that have stopped earning their disk.

Two tables here grow without bound and are never read once they are old, and
until this existed the only levers were all-or-nothing.

``verdict_cache`` is the bigger one, and it grows *because* the project works:
the loop it is built around is edit a clause, measure again, and the key is the
prompt - so every edit strands the generation of entries it invalidated. Those
rows can never be hit again by construction, and nothing removed them.
``ptm.store.cache_clear`` would, along with every entry that is about to save
the next replay.

``judge_samples`` is one row per (case, repeat) for every stability run ever
made, and only the newest run backs a reported figure.

    python -m ptm.prune                 # everything older than 90 days
    python -m ptm.prune expenses --days 30
    python -m ptm.prune --dry-run       # count it without doing it

What it will not touch: precedents, their history, the drafts table, and the
aggregates the dashboard reads. Age is not a reason to forget a human ruling,
and a run row is what a trend is drawn from - see :func:`ptm.store.prune`.
"""

from __future__ import annotations

import sys

from . import cli, store
from .config import available_domains


def describe(result: dict, domain: str | None, days: int, dry_run: bool,
             vacuum: bool = False) -> str:
    rows = {k: v for k, v in result.items() if k != "cutoff"}
    total = sum(rows.values())
    where = domain or "every domain"
    verb = "would remove" if dry_run else "removed"
    lines = [f"{verb} {total:,} row(s) from {where}, older than {days} day(s) "
             f"(before {result['cutoff'][:19]} UTC)"]
    for table, n in sorted(rows.items()):
        lines.append(f"  {table:<16} {n:>10,}")
    if not total:
        lines.append("  nothing old enough to drop; this database is not carrying weight")
    elif not dry_run and not vacuum:
        # Said out loud because it is the one surprise here: SQLite does not
        # hand freed pages back to the filesystem, so a prune that worked shows
        # no change in the file size until the database is rewritten. It used to
        # stop at saying so, which left the reader to go and do by hand the one
        # thing this command already had everything it needed to do - hence
        # --vacuum, and hence this line naming it rather than naming SQL.
        lines.append("  the file will not shrink until SQLite is asked to rewrite it: "
                     "add --vacuum, or just re-seed.")
    return "\n".join(lines)


USAGE = """usage:
  python -m ptm.prune [domain] [--days N] [--dry-run] [--keep-unhit] [--vacuum]

Drop the cache and sample rows that have stopped earning their disk. Precedents,
their history, the drafts table and the aggregates the dashboard reads are never
touched - age is not a reason to forget a human ruling.

  domain         defaults to every domain
  --days N       how old a row must be to go. Default 90
  --dry-run      count what would go, using the same clauses that delete it
  --keep-unhit   keep cache entries nothing has ever served - the right setting
                 straight after a big replay nothing has re-run yet
  --vacuum       rewrite the database afterwards so the file actually shrinks.
                 SQLite keeps freed pages for itself otherwise"""


def describe_vacuum(result: dict) -> str:
    """What rewriting the database actually gave back."""
    reclaimed = result["bytes_reclaimed"]
    line = (f"vacuumed: {result['bytes_before']:,} -> {result['bytes_after']:,} bytes "
            f"({reclaimed:,} reclaimed)")
    if not reclaimed:
        # Not a failure, and it would read as one. A database with nothing to
        # give back is the state this command is trying to reach.
        line += " - nothing to give back, this database was already compact"
    return line


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.prune [domain] [--days N] [--dry-run] [--keep-unhit] [--vacuum]``."""
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0
    dry_run = "--dry-run" in args
    keep_unhit = "--keep-unhit" in args
    vacuum = "--vacuum" in args
    days = 90
    if "--days" in args:
        index = args.index("--days")
        try:
            days = int(args[index + 1])
        except (IndexError, ValueError):
            print("ERROR --days needs a number of days", file=sys.stderr)
            return 2
        args.pop(index + 1)
    positional = [a for a in args if not a.startswith("--")]
    if days < 0:
        print("ERROR --days cannot be negative", file=sys.stderr)
        return 2

    domain = positional[0] if positional else None
    if domain and domain not in available_domains():
        print(f"ERROR unknown domain {domain!r}; have {available_domains()}",
              file=sys.stderr)
        return 2

    store.init_db()
    if dry_run:
        result = store.prune_preview(domain, days, keep_unhit)
    else:
        result = store.prune(domain, days, keep_unhit)
    print(describe(result, domain, days, dry_run, vacuum))
    # A dry run reports and changes nothing, and rewriting the file is a change.
    # Vacuuming under --dry-run would be the one thing that command promises not
    # to do, however harmless the rewrite itself is.
    if vacuum and not dry_run:
        print(describe_vacuum(store.vacuum()))
    elif vacuum:
        print("  --vacuum skipped: --dry-run changes nothing, and rewriting the "
              "database is a change")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
