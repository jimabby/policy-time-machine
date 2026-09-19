"""SQLite persistence.

Deliberately boring. The interesting parts are :func:`load_cases`, which
reconstructs each case as it was known on the day it was decided rather than as
it is known today, and :func:`save_replay`, which writes one replay atomically
along with the aggregates the dashboard reads.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import DB_PATH
from .diff import DEVIATION
from .models import Case, Flip, FlipConfirmation, Precedent, Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id          TEXT PRIMARY KEY,
    domain           TEXT NOT NULL,
    subject_id       TEXT NOT NULL,
    decided_at       TEXT NOT NULL,
    payload          TEXT NOT NULL,
    actual_outcome   TEXT NOT NULL,
    actual_rationale TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS cases_domain_decided ON cases (domain, decided_at);

-- Slowly-changing facts about the subject of a case (an employee, a member,
-- a policyholder). known_from is what makes point-in-time replay possible:
-- a fact recorded after a case was decided must not influence that case.
CREATE TABLE IF NOT EXISTS subject_facts (
    subject_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    known_from  TEXT NOT NULL,
    PRIMARY KEY (subject_id, key, known_from)
);

CREATE TABLE IF NOT EXISTS verdicts (
    run_id         TEXT NOT NULL,
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    confidence     REAL NOT NULL,
    policy_clause  TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, case_id)
);
CREATE INDEX IF NOT EXISTS verdicts_domain_policy ON verdicts (domain, policy_version, case_id);

CREATE TABLE IF NOT EXISTS flips (
    run_id         TEXT NOT NULL,
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    actual_outcome TEXT NOT NULL,
    new_outcome    TEXT NOT NULL,
    direction      TEXT NOT NULL,
    impact         REAL NOT NULL,
    confidence     REAL NOT NULL,
    rationale      TEXT NOT NULL,
    -- Which clause of the proposed policy produced this change. Without it you
    -- know how much moves but not which sentence to edit.
    policy_clause  TEXT DEFAULT '',
    -- The clause responsible for the change, which is not always the clause the
    -- candidate cited: a restriction that stops firing cites nothing. Also the
    -- bucket that separates a policy effect from a reviewer deviation.
    attribution    TEXT DEFAULT '',
    -- Whether re-judging this flip reproduced it: 'stable', 'unstable', or ''
    -- when nothing has measured it. Also in MIGRATIONS, for a database seeded
    -- before it existed - a column that lives only there is invisible to
    -- anyone reading the schema to find out what a flip is.
    stability      TEXT DEFAULT '',
    -- What the policy in force gives for this case, when a baseline pass ran.
    baseline_outcome TEXT DEFAULT '',
    -- Segment values as of the decision date. Stored rather than joined from
    -- cases because point-in-time facts (an employee's grade at the time) are
    -- not in the case record and must not be re-derived from today's data.
    segments       TEXT DEFAULT '{}',
    reviewed       INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, case_id)
);
CREATE INDEX IF NOT EXISTS flips_domain_policy_case ON flips (domain, policy_version, case_id);
CREATE INDEX IF NOT EXISTS flips_review_queue ON flips (domain, policy_version, reviewed, impact DESC);

-- The one durable artefact. Everything else can be recomputed.
CREATE TABLE IF NOT EXISTS precedents (
    case_id         TEXT NOT NULL,
    domain          TEXT NOT NULL,
    correct_outcome TEXT NOT NULL,
    ruled_by        TEXT NOT NULL,
    -- Why, in the reviewer's words. The only free text here written by the
    -- person accountable for the decision.
    note            TEXT DEFAULT '',
    established_at  TEXT NOT NULL,
    established_by_run TEXT DEFAULT '',
    -- The circumstances of the ruling: which candidate policy the reviewer was
    -- shown, and the verdict they were overturning. Without these a precedent
    -- cannot be re-read later - a ruling made about a clause that has since
    -- been rewritten is indistinguishable from one made this morning.
    policy_version  TEXT DEFAULT '',
    judged_outcome  TEXT DEFAULT '',
    judged_clause   TEXT DEFAULT '',
    PRIMARY KEY (domain, case_id)
);

-- Rulings that have been superseded by a later one on the same case. Written
-- automatically by save_precedent, and durable like the precedents themselves.
--
-- Re-adjudication is the only thing that overwrites a precedent, and it exists
-- because a ruling made about a clause that has since been rewritten is no
-- longer a fact about this policy (see ptm.diff.stale_precedents). But "the
-- only durable artefact" losing its earlier version silently is precisely the
-- rot this project is about: a reviewer must be able to see that somebody
-- ruled differently in March, and what they said about it, before agreeing
-- that the new ruling replaces it.
CREATE TABLE IF NOT EXISTS precedent_history (
    domain          TEXT NOT NULL,
    case_id         TEXT NOT NULL,
    superseded_at   TEXT NOT NULL,
    correct_outcome TEXT NOT NULL,
    ruled_by        TEXT NOT NULL,
    note            TEXT DEFAULT '',
    established_at  TEXT NOT NULL,
    established_by_run TEXT DEFAULT '',
    policy_version  TEXT DEFAULT '',
    judged_outcome  TEXT DEFAULT '',
    judged_clause   TEXT DEFAULT '',
    PRIMARY KEY (domain, case_id, superseded_at)
);
CREATE INDEX IF NOT EXISTS precedent_history_lookup ON precedent_history (domain, case_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    baseline       TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    cases_replayed INTEGER DEFAULT 0,
    flips          INTEGER DEFAULT 0,
    impact         REAL DEFAULT 0,
    -- The policy version the candidate was diffed against, when a baseline pass
    -- ran. Empty means the diff was against recorded history alone.
    baseline_version TEXT DEFAULT '',
    -- The cost ledger. Named estimated_* because it is measured from the
    -- prompts we built, not read back from the vendor. See ptm/cost.py.
    estimated_requests      INTEGER DEFAULT 0,
    estimated_input_tokens  INTEGER DEFAULT 0,
    estimated_output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd      REAL DEFAULT 0,
    -- What the vendor said it actually was, when a metered judge ran. Kept
    -- beside the estimate rather than replacing it: the gap between the two is
    -- what prices the next backfill properly. See ptm/metered.py.
    actual_requests         INTEGER DEFAULT 0,
    actual_input_tokens     INTEGER DEFAULT 0,
    actual_output_tokens    INTEGER DEFAULT 0,
    actual_cost_usd         REAL DEFAULT 0,
    judge_model    TEXT DEFAULT ''
);

-- Blast radius, pre-aggregated per run so the dashboard sums rows instead of
-- re-reading every flip. cases is the denominator: 9 flips out of 12 travel
-- claims is a different story from 9 out of 400.
CREATE TABLE IF NOT EXISTS segment_stats (
    run_id         TEXT NOT NULL,
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    field          TEXT NOT NULL,
    value          TEXT NOT NULL,
    cases          INTEGER DEFAULT 0,
    flips          INTEGER DEFAULT 0,
    loosening      INTEGER DEFAULT 0,
    tightening     INTEGER DEFAULT 0,
    impact_loosening  REAL DEFAULT 0,
    impact_tightening REAL DEFAULT 0,
    PRIMARY KEY (run_id, field, value)
);
CREATE INDEX IF NOT EXISTS segment_stats_lookup ON segment_stats (domain, policy_version, field);

-- One row per (case, segment field), which is what makes the blast radius
-- safe to read when runs overlap. segment_stats above is a faithful record of
-- what *one run* saw, so summing it across a manual run that replays all of
-- history on top of a backfill counts every case twice. Keying on the case
-- instead means the newest run simply replaces its own rows, exactly like
-- every other latest-row-wins read model here.
CREATE TABLE IF NOT EXISTS case_segments (
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    field          TEXT NOT NULL,
    value          TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    PRIMARY KEY (domain, policy_version, case_id, field)
);
CREATE INDEX IF NOT EXISTS case_segments_lookup ON case_segments (domain, policy_version, field, value);
CREATE INDEX IF NOT EXISTS case_segments_run ON case_segments (run_id);

-- Whether re-judging a recorded flip reproduced it. A flip the judge will not
-- reproduce is the model changing its mind, not the policy moving, and must
-- not reach a human as though it were settled - see ptm/stability.py.
CREATE TABLE IF NOT EXISTS flip_stability (
    domain           TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    case_id          TEXT NOT NULL,
    samples          INTEGER NOT NULL,
    outcomes         TEXT NOT NULL,
    modal_outcome    TEXT NOT NULL,
    recorded_outcome TEXT DEFAULT '',
    agreement        REAL NOT NULL,
    stable           INTEGER NOT NULL,
    measured_at      TEXT NOT NULL,
    run_id           TEXT DEFAULT '',
    PRIMARY KEY (domain, policy_version, case_id)
);

-- Judge stability. One row per (case, sample) so the disagreement rate can be
-- recomputed and audited rather than taken on trust.
CREATE TABLE IF NOT EXISTS judge_samples (
    run_id         TEXT NOT NULL,
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    sample_idx     INTEGER NOT NULL,
    outcome        TEXT NOT NULL,
    confidence     REAL NOT NULL,
    policy_clause  TEXT DEFAULT '',
    PRIMARY KEY (run_id, case_id, sample_idx)
);

-- One judged prompt, keyed by the hash of the prompt itself and the model that
-- answered it. Editing one clause currently means re-judging every case at full
-- price, which makes the edit-and-re-measure loop the project is built around
-- the one thing nobody does twice. See ptm/cache.py for why the key is the
-- prompt rather than (case, version).
CREATE TABLE IF NOT EXISTS verdict_cache (
    cache_key      TEXT PRIMARY KEY,
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    case_id        TEXT NOT NULL,
    judge_model    TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    confidence     REAL NOT NULL,
    policy_clause  TEXT DEFAULT '',
    prompt_chars   INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL,
    hits           INTEGER DEFAULT 0,
    last_hit_at    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS verdict_cache_domain ON verdict_cache (domain, policy_version);

-- A drafted policy amendment: the patch, the evidence that motivated it, and
-- what checking it found. The markdown itself lives on disk under
-- include/drafts/ so every DAG can judge it like any other version; this is the
-- provenance, which is the part that must not be lost when someone opens the
-- file six weeks later and asks who wrote it and why.
CREATE TABLE IF NOT EXISTS policy_drafts (
    domain         TEXT NOT NULL,
    version        TEXT NOT NULL,
    base_version   TEXT NOT NULL,
    summary        TEXT DEFAULT '',
    patch          TEXT NOT NULL,
    evidence       TEXT DEFAULT '{}',
    verification   TEXT DEFAULT '{}',
    drafted_by     TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    created_by_run TEXT DEFAULT '',
    -- Set when a person promoted this draft into include/policies/ and the
    -- domain YAML. Both an adopted draft and a discarded one leave the drafts
    -- folder empty, so without this the two are indistinguishable on disk -
    -- and they are the opposite decision.
    adopted_as     TEXT DEFAULT '',
    adopted_by     TEXT DEFAULT '',
    adopted_at     TEXT DEFAULT '',
    PRIMARY KEY (domain, version)
);

-- A second judge's answers on the same cases under the same policy. Kept per
-- run rather than collapsed to a rate, because the useful output is the list of
-- cases the two judges split on: those are sentences of the policy to rewrite,
-- and they cost no human time to find. See ptm/crosscheck.py.
CREATE TABLE IF NOT EXISTS cross_checks (
    run_id         TEXT PRIMARY KEY,
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    primary_judge  TEXT NOT NULL,
    secondary_judge TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    compared       INTEGER DEFAULT 0,
    agreed         INTEGER DEFAULT 0,
    agreement      REAL DEFAULT 0,
    clause_agreement REAL DEFAULT 0,
    contested_flips INTEGER DEFAULT 0,
    report         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cross_checks_lookup ON cross_checks (domain, policy_version, created_at);

CREATE TABLE IF NOT EXISTS stability_runs (
    run_id           TEXT PRIMARY KEY,
    domain           TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    cases_sampled    INTEGER DEFAULT 0,
    samples_per_case INTEGER DEFAULT 0,
    unstable_cases   INTEGER DEFAULT 0,
    disagreement_rate REAL DEFAULT 0,
    judge_model      TEXT DEFAULT '',
    estimated_cost_usd REAL DEFAULT 0
);
"""

#: Columns added after the first release. SQLite's CREATE TABLE IF NOT EXISTS
#: will not alter a table that already exists, so a database seeded by an
#: earlier version needs these bolted on explicitly.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("flips", "policy_clause", "TEXT DEFAULT ''"),
    ("flips", "segments", "TEXT DEFAULT '{}'"),
    ("flips", "attribution", "TEXT DEFAULT ''"),
    ("flips", "baseline_outcome", "TEXT DEFAULT ''"),
    ("runs", "baseline_version", "TEXT DEFAULT ''"),
    ("runs", "estimated_requests", "INTEGER DEFAULT 0"),
    ("runs", "estimated_input_tokens", "INTEGER DEFAULT 0"),
    ("runs", "estimated_output_tokens", "INTEGER DEFAULT 0"),
    ("runs", "estimated_cost_usd", "REAL DEFAULT 0"),
    ("runs", "judge_model", "TEXT DEFAULT ''"),
    ("flips", "stability", "TEXT DEFAULT ''"),
    ("precedents", "policy_version", "TEXT DEFAULT ''"),
    ("precedents", "judged_outcome", "TEXT DEFAULT ''"),
    ("precedents", "judged_clause", "TEXT DEFAULT ''"),
    ("runs", "actual_requests", "INTEGER DEFAULT 0"),
    ("runs", "actual_input_tokens", "INTEGER DEFAULT 0"),
    ("runs", "actual_output_tokens", "INTEGER DEFAULT 0"),
    ("runs", "actual_cost_usd", "REAL DEFAULT 0"),
    ("policy_drafts", "adopted_as", "TEXT DEFAULT ''"),
    ("policy_drafts", "adopted_by", "TEXT DEFAULT ''"),
    ("policy_drafts", "adopted_at", "TEXT DEFAULT ''"),
]


def _bound(value: datetime) -> str:
    """Render an interval bound the way ``decided_at`` is stored.

    SQLite compares these as TEXT, and the DAG's bounds come from pendulum, so
    they serialise with a "+00:00" suffix that a naive ``decided_at`` never has.
    "2025-06-01T00:00:00" sorts *below* "2025-06-01T00:00:00+00:00", so a case
    decided exactly on a boundary silently drops out of its own window - the one
    kind of loss a point-in-time replay must never have.
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


def _stamp() -> str:
    """Now, in the one format every timestamp column here is written in.

    Naive UTC, which is what :func:`_bound` already normalises a data-interval
    bound to and what ``decided_at`` is seeded as. Every column in this schema
    is compared and sorted by SQLite as TEXT, so what matters is not which zone
    is chosen but that one is chosen: ``datetime.now()`` is the machine's local
    time, and the same column then holds local stamps from a CLI run, UTC ones
    from an Airflow task that used ``pendulum.now("UTC")``, and - twice a year -
    an hour of local stamps that sort *before* rows written before them.

    ``runs.started_at`` orders the backfill trend, ``precedent_history`` is
    ordered newest-first, and ``cross_checks`` is read with ``ORDER BY
    created_at DESC LIMIT 1`` to decide which measurement is current. All three
    are wrong for an hour a year, silently, on a value nobody would think to
    check.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        # WAL is a property of the database file and survives every later
        # connection, which is why it is set here and nowhere else. A
        # `PRAGMA foreign_keys=ON` used to sit beside it and was doing nothing
        # twice over: the setting is per *connection*, so it applied to this one
        # and to none of the connections that actually write, and SCHEMA
        # declares no foreign keys for it to enforce in the first place. A line
        # that reads as a guarantee and is not one is worse than its absence -
        # the tables are joined on (domain, case_id) by convention, and that
        # convention is held by the queries and the tests, not by SQLite.
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """Add post-release columns to a database that predates them."""
    for table, column, decl in MIGRATIONS:
        existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if not existing:  # table itself is new; SCHEMA already created it
            continue
        if column not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


#: SQLite's default parameter ceiling is 999, so an ``IN`` list is chunked.
_ID_CHUNK = 400


def load_cases(domain: str, until: datetime, limit: int | None = None,
               since: datetime | None = None, newest_first: bool = False,
               case_ids: list[str] | None = None) -> list[Case]:
    """Load cases decided in ``[since, until)``, hydrated point-in-time.

    Each case's payload is enriched only with facts about its subject that were
    already known on the day the case was decided. This is the whole reason the
    replay is trustworthy: a case decided in March 2025 cannot see an attribute
    first recorded in August 2025.

    ``newest_first`` changes only *which* cases a ``limit`` keeps, never the
    order they come back in. It matters because the oldest slice of history is
    the least representative one: slowly-changing facts have not changed yet, so
    a capped replay taken from the front of the period misses precisely the
    interactions a point-in-time engine exists to get right.

    ``case_ids`` asks for exactly those cases and ignores ``limit`` entirely.
    Callers that need a *specific* set - the precedent gate needs every case a
    human has ruled on - must use it rather than loading everything and
    filtering, because a ``limit`` silently truncating the set would make the
    regression suite check fewer precedents than exist and still pass.

    ``limit`` defaults to **None, meaning every case in the window**. It used to
    default to 10,000, which is the same failure one order of magnitude further
    out: a sweep curve, a rule-agreement figure or a proposal drawn from the
    first 10,000 of 40,000 cases describes a sample nobody asked for and nothing
    says so. A caller that wants a bound now has to name it, which is also the
    point at which it can report it.
    """
    order = "DESC" if newest_first else "ASC"
    with conn() as c:
        if case_ids is not None:
            wanted = list(dict.fromkeys(case_ids))
            rows = []
            for i in range(0, len(wanted), _ID_CHUNK):
                chunk = wanted[i:i + _ID_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows += c.execute(
                    f"SELECT * FROM cases WHERE domain = ? AND case_id IN ({placeholders})",
                    (domain, *chunk),
                ).fetchall()
        else:
            rows = c.execute(
                f"""
                SELECT * FROM cases
                WHERE domain = ? AND decided_at < ? AND decided_at >= ?
                ORDER BY decided_at {order}
                LIMIT ?
                """,
                # SQLite reads a negative LIMIT as no limit, which is how
                # "every case" stays one query rather than two code paths.
                (domain, _bound(until), _bound(since or datetime.min),
                 -1 if limit is None else limit),
            ).fetchall()

        cases: list[Case] = []
        for row in rows:
            payload = json.loads(row["payload"])
            facts = c.execute(
                """
                SELECT f.key, f.value
                FROM subject_facts f
                WHERE f.subject_id = ? AND f.known_from <= ?
                  AND f.known_from = (
                      SELECT MAX(g.known_from) FROM subject_facts g
                      WHERE g.subject_id = f.subject_id AND g.key = f.key AND g.known_from <= ?
                  )
                """,
                (row["subject_id"], row["decided_at"], row["decided_at"]),
            ).fetchall()
            payload.update({f["key"]: f["value"] for f in facts})
            cases.append(
                Case(
                    case_id=row["case_id"],
                    domain=row["domain"],
                    decided_at=datetime.fromisoformat(row["decided_at"]),
                    payload=payload,
                    actual_outcome=row["actual_outcome"],
                    actual_rationale=row["actual_rationale"] or "",
                )
            )
        # Callers reason chronologically regardless of which end was sampled.
        cases.sort(key=lambda c: c.decided_at)
        return cases


def save_precedent(p: Precedent) -> None:
    """Record a human ruling, archiving any ruling it replaces.

    The archive is not optional and not the caller's job. ``INSERT OR REPLACE``
    on ``(domain, case_id)`` is what lets a stale ruling be re-adjudicated at
    all, and it is also a silent overwrite of the one thing in this system that
    cannot be recomputed. Doing the copy here means no future caller can
    re-adjudicate without leaving the earlier ruling behind - see
    ``precedent_history`` in SCHEMA.

    ``established_at`` is normalised the same way every other timestamp here is.
    It arrives tz-aware from an Airflow task (``pendulum.now("UTC")``) and naive
    from a CLI or a test, and the two spellings of one instant do not compare
    as TEXT - which is how this column is read back and ordered.
    """
    with conn() as c:
        c.execute(
            """INSERT INTO precedent_history
               (domain, case_id, superseded_at, correct_outcome, ruled_by, note,
                established_at, established_by_run, policy_version, judged_outcome,
                judged_clause)
               SELECT domain, case_id, ?, correct_outcome, ruled_by, note,
                      established_at, established_by_run, policy_version,
                      judged_outcome, judged_clause
               FROM precedents WHERE domain=? AND case_id=?""",
            (_free_supersede_stamp(c, p.domain, p.case_id), p.domain, p.case_id),
        )
        c.execute(
            """INSERT OR REPLACE INTO precedents
               (case_id, domain, correct_outcome, ruled_by, note, established_at,
                established_by_run, policy_version, judged_outcome, judged_clause)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (p.case_id, p.domain, p.correct_outcome, p.ruled_by, p.note,
             _bound(p.established_at), p.established_by_run,
             p.policy_version, p.judged_outcome, p.judged_clause),
        )


def _free_supersede_stamp(c: sqlite3.Connection, domain: str, case_id: str) -> str:
    """An archive timestamp not already taken for this case.

    ``precedent_history``'s key is ``(domain, case_id, superseded_at)``, and the
    copy above is a plain INSERT inside the same transaction as the new ruling.
    Two supersessions of one case landing in the same microsecond would
    therefore raise, roll the transaction back, and lose *the ruling* - not just
    its archive - which is the one failure this whole table exists to prevent.
    A microsecond collision needs two reviewers finishing at once on a re-run,
    which is rare and not impossible; stepping the stamp forward until it is
    free costs one indexed lookup and removes the case entirely.
    """
    stamp = _stamp()
    taken = {r["superseded_at"] for r in c.execute(
        "SELECT superseded_at FROM precedent_history WHERE domain=? AND case_id=?",
        (domain, case_id))}
    while stamp in taken:
        stamp = (datetime.fromisoformat(stamp) + timedelta(microseconds=1)).isoformat()
    return stamp


def precedent_history(domain: str, case_id: str | None = None) -> list[dict]:
    """Rulings that a later ruling on the same case replaced, newest first."""
    where = "WHERE domain=?" + (" AND case_id=?" if case_id else "")
    params: tuple = (domain,) if case_id is None else (domain, case_id)
    return query(
        f"""SELECT * FROM precedent_history {where}
            ORDER BY superseded_at DESC, case_id""", params)


def import_precedent_history(rows: list[dict]) -> int:
    """Merge archived rulings in, keeping any this database already has.

    ``INSERT OR IGNORE`` on the natural key ``(domain, case_id, superseded_at)``,
    so importing the same export twice adds nothing and importing two
    environments' archives merges them. It is the one write here that must not
    overwrite: an archived ruling is the record of what somebody said before
    somebody else disagreed, and the whole reason it is kept is that nothing
    should be able to quietly replace it - :func:`save_precedent` included.
    """
    if not rows:
        return 0
    with conn() as c:
        before = c.execute("SELECT COUNT(*) n FROM precedent_history").fetchone()["n"]
        c.executemany(
            """INSERT OR IGNORE INTO precedent_history
               (domain, case_id, superseded_at, correct_outcome, ruled_by, note,
                established_at, established_by_run, policy_version, judged_outcome,
                judged_clause)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(r["domain"], r["case_id"], r["superseded_at"], r["correct_outcome"],
              r["ruled_by"], r.get("note") or "", r["established_at"],
              r.get("established_by_run") or "", r.get("policy_version") or "",
              r.get("judged_outcome") or "", r.get("judged_clause") or "")
             for r in rows],
        )
        after = c.execute("SELECT COUNT(*) n FROM precedent_history").fetchone()["n"]
    return after - before


def revision_counts(domain: str) -> dict[str, int]:
    """How many times each case's ruling has been replaced, keyed by case id."""
    return {r["case_id"]: r["n"] for r in query(
        "SELECT case_id, COUNT(*) n FROM precedent_history WHERE domain=? GROUP BY case_id",
        (domain,))}


def load_precedents(domain: str) -> list[Precedent]:
    with conn() as c:
        rows = c.execute("SELECT * FROM precedents WHERE domain = ? ORDER BY established_at", (domain,)).fetchall()
    return [
        Precedent(
            case_id=r["case_id"], domain=r["domain"], correct_outcome=r["correct_outcome"],
            ruled_by=r["ruled_by"], note=r["note"] or "",
            established_at=datetime.fromisoformat(r["established_at"]),
            established_by_run=r["established_by_run"] or "",
            policy_version=r["policy_version"] or "",
            judged_outcome=r["judged_outcome"] or "",
            judged_clause=r["judged_clause"] or "",
        )
        for r in rows
    ]


def precedents_with_payload(domain: str) -> list[dict]:
    """Precedents joined to their case records, for conflict detection.

    Payloads come from the case record rather than point-in-time hydration:
    conflict detection asks whether two rulings are consistent with each other,
    which is a question about the cases as filed.
    """
    rows = query(
        """SELECT p.*, c.payload, c.decided_at, c.actual_outcome
           FROM precedents p JOIN cases c ON c.case_id = p.case_id
           WHERE p.domain = ? ORDER BY p.established_at""",
        (domain,),
    )
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Escape hatch for the plugin's read-only API."""
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def flips_for_policy(domain: str, policy_version: str) -> list[dict]:
    """The current flip for each case and policy version.

    Manual replays overlap backfills by design. Selecting the most recently
    persisted result prevents those helpful spot-checks from creating duplicate
    human-review tasks or inflated dashboard totals.
    """
    return query(
        """SELECT f.*, c.payload, c.decided_at, c.actual_rationale
           FROM flips f JOIN cases c ON c.case_id = f.case_id
           JOIN (
             SELECT case_id, MAX(rowid) AS latest_rowid FROM flips
             WHERE domain = ? AND policy_version = ? GROUP BY case_id
           ) latest ON latest.latest_rowid = f.rowid
           ORDER BY f.impact DESC""",
        (domain, policy_version),
    )


def mark_reviewed(domain: str, policy_version: str, case_ids: list[str]) -> None:
    """Mark only the reviewed version of a case, never every policy run."""
    if not case_ids:
        return
    with conn() as c:
        c.executemany(
            "UPDATE flips SET reviewed = 1 WHERE domain = ? AND policy_version = ? AND case_id = ?",
            [(domain, policy_version, case_id) for case_id in case_ids],
        )


#: Baseline verdicts share a run with the candidate but are stored under their
#: own synthetic run id. The verdicts primary key is (run_id, case_id), so the
#: two passes would otherwise collide on every case; suffixing keeps both - and
#: keeping both is what lets two policy versions be compared later without
#: paying to judge either of them again.
BASELINE_RUN_SUFFIX = "::baseline"


def _confirmations(c: sqlite3.Connection, domain: str,
                   policy_version: str) -> dict[tuple[str, str], str]:
    """Confirmation results keyed by ``(case_id, the outcome they were about)``.

    Keyed on the outcome as well as the case deliberately. A confirmation pass
    asks whether *this verdict* reproduces, so a later replay that reaches a
    different outcome for the same case has not inherited the answer - it has
    produced a new question nothing has measured yet. Carrying the tag across
    regardless would mark a brand new verdict 'stable' on the strength of an
    experiment run against a different one.
    """
    rows = c.execute(
        """SELECT case_id, recorded_outcome, stable FROM flip_stability
           WHERE domain = ? AND policy_version = ? AND recorded_outcome <> ''""",
        (domain, policy_version),
    ).fetchall()
    return {(r["case_id"], r["recorded_outcome"]): ("stable" if r["stable"] else "unstable")
            for r in rows}


def save_replay(run_id: str, domain: str, policy_version: str, baseline: str,
                cases_replayed: int, flips: list[Flip], impact: float,
                verdicts: dict[str, Verdict], segments: list[dict] | None = None,
                ledger: dict | None = None, baseline_version: str = "",
                baseline_verdicts: dict[str, Verdict] | None = None,
                case_segments: list[dict] | None = None) -> None:
    """Persist one replay atomically, including an idempotent re-run cleanup."""
    now = _stamp()
    ledger = ledger or {}
    baseline_run = run_id + BASELINE_RUN_SUFFIX
    with conn() as c:
        # Airflow retries use the same run id. Clear old rows first so a
        # policy edit cannot leave a no-longer-flipped case visible in the UI.
        c.execute("DELETE FROM verdicts WHERE run_id IN (?, ?)", (run_id, baseline_run))
        c.execute("DELETE FROM flips WHERE run_id = ?", (run_id,))
        c.execute("DELETE FROM segment_stats WHERE run_id = ?", (run_id,))
        # Keyed on the case rather than the run, so a retry that sees fewer
        # cases - or a domain that has dropped a segment_field - would otherwise
        # leave rows behind that INSERT OR REPLACE never touches and that the
        # blast radius then counts forever. Clearing this run's own rows first
        # makes the write a replacement rather than an accumulation.
        c.execute("DELETE FROM case_segments WHERE run_id = ?", (run_id,))
        c.executemany(
            """INSERT INTO verdicts
               (run_id, domain, policy_version, case_id, outcome, rationale, confidence, policy_clause, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(run_id, domain, policy_version, case_id, v.outcome, v.rationale,
              v.confidence, v.policy_clause, now) for case_id, v in verdicts.items()],
        )
        # Confirmation results already on file, carried onto the rows this run
        # writes. Without this the column defaults to '' and the *latest* flip
        # row - the one the human queue reads - forgets that a confirmation pass
        # measured this verdict and would not reproduce it, so the next
        # adjudication run quietly offers it up again. The measurement itself
        # never went anywhere: it is in flip_stability, and this is what puts it
        # back where select_for_review looks.
        confirmed = _confirmations(c, domain, policy_version)
        c.executemany(
            """INSERT INTO flips
               (run_id, domain, policy_version, case_id, actual_outcome, new_outcome,
                direction, impact, confidence, rationale, policy_clause, segments,
                attribution, baseline_outcome, stability)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(run_id, domain, policy_version, f.case_id, f.actual_outcome, f.new_outcome,
              f.direction, f.impact, f.confidence, f.rationale, f.policy_clause,
              json.dumps(f.segments), f.attribution, f.baseline_outcome,
              confirmed.get((f.case_id, f.new_outcome), "")) for f in flips],
        )
        if baseline_verdicts and baseline_version:
            c.executemany(
                """INSERT INTO verdicts
                   (run_id, domain, policy_version, case_id, outcome, rationale,
                    confidence, policy_clause, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(baseline_run, domain, baseline_version, case_id, v.outcome, v.rationale,
                  v.confidence, v.policy_clause, now)
                 for case_id, v in baseline_verdicts.items()],
            )
        c.executemany(
            """INSERT INTO segment_stats
               (run_id, domain, policy_version, field, value, cases, flips,
                loosening, tightening, impact_loosening, impact_tightening)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(run_id, domain, policy_version, s["field"], s["value"], s["cases"],
              s["flips"], s["loosening"], s["tightening"],
              s["impact_loosening"], s["impact_tightening"]) for s in (segments or [])],
        )
        # Keyed on the case, so a manual run that replays history a second time
        # replaces these rows instead of adding a second copy of every case to
        # the blast radius. See the case_segments comment in SCHEMA.
        c.executemany(
            """INSERT OR REPLACE INTO case_segments
               (domain, policy_version, case_id, field, value, run_id)
               VALUES (?,?,?,?,?,?)""",
            [(domain, policy_version, r["case_id"], r["field"], r["value"], run_id)
             for r in (case_segments or [])],
        )
        c.execute(
            """INSERT OR REPLACE INTO runs
               (run_id, domain, policy_version, baseline, started_at, cases_replayed,
                flips, impact, baseline_version, estimated_requests, estimated_input_tokens,
                estimated_output_tokens, estimated_cost_usd, actual_requests,
                actual_input_tokens, actual_output_tokens, actual_cost_usd, judge_model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, domain, policy_version, baseline, now, cases_replayed, len(flips), impact,
             baseline_version,
             ledger.get("estimated_requests", 0), ledger.get("estimated_input_tokens", 0),
             ledger.get("estimated_output_tokens", 0), ledger.get("estimated_cost_usd", 0.0),
             ledger.get("actual_requests", 0), ledger.get("actual_input_tokens", 0),
             ledger.get("actual_output_tokens", 0), ledger.get("actual_cost_usd", 0.0),
             ledger.get("judge_model", "")),
        )


def save_verdicts(run_id: str, domain: str, policy_version: str,
                  verdicts: dict[str, Verdict]) -> None:
    """Persist verdicts from a pass that is not a replay.

    The precedent gate judges cases too, and storing what it found is what lets
    the dashboard show the gate's answer without paying to re-judge it.
    """
    now = _stamp()
    with conn() as c:
        c.execute("DELETE FROM verdicts WHERE run_id = ?", (run_id,))
        c.executemany(
            """INSERT INTO verdicts
               (run_id, domain, policy_version, case_id, outcome, rationale,
                confidence, policy_clause, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(run_id, domain, policy_version, case_id, v.outcome, v.rationale,
              v.confidence, v.policy_clause, now) for case_id, v in verdicts.items()],
        )


def save_flip_stability(domain: str, policy_version: str,
                        confirmations: list[FlipConfirmation], run_id: str = "") -> int:
    """Record whether each re-judged flip reproduced, and tag the flip rows.

    The tag on ``flips.stability`` is what keeps an unconfirmed flip out of the
    human queue: a verdict the judge will not repeat is not a policy change and
    must not be turned into permanent precedent.
    """
    if not confirmations:
        return 0
    now = _stamp()
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO flip_stability
               (domain, policy_version, case_id, samples, outcomes, modal_outcome,
                recorded_outcome, agreement, stable, measured_at, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(domain, policy_version, f.case_id, f.samples, json.dumps(f.outcomes),
              f.modal_outcome, f.recorded_outcome, f.agreement, int(f.stable), now, run_id)
             for f in confirmations],
        )
        c.executemany(
            "UPDATE flips SET stability = ? WHERE domain = ? AND policy_version = ? AND case_id = ?",
            [("stable" if f.stable else "unstable", domain, policy_version, f.case_id)
             for f in confirmations],
        )
    return len(confirmations)


def flip_stability(domain: str, policy_version: str) -> dict[str, dict]:
    """Per-flip confirmation results, keyed by case id."""
    rows = query(
        "SELECT * FROM flip_stability WHERE domain=? AND policy_version=?",
        (domain, policy_version))
    for r in rows:
        r["outcomes"] = json.loads(r["outcomes"])
        r["stable"] = bool(r["stable"])
    return {r["case_id"]: r for r in rows}


#: Everything derived from a domain's cases. Precedents and their history are
#: deliberately absent: they are the one durable artefact - and the record of
#: what an earlier ruling said is part of it - so both survive a re-seed. The
#: verdict cache goes: its keys are hashes of prompts built from the *old*
#: cases, so after a re-seed not one of them can ever be hit again.
DERIVED_TABLES = ("verdicts", "flips", "segment_stats", "case_segments", "runs",
                  "judge_samples", "stability_runs", "flip_stability", "verdict_cache",
                  "cross_checks")


# ------------------------------------------------------------- verdict cache
# Keyed on the hash of the prompt, so any change to the policy text, the case,
# the rendering or the instructions misses. See ptm/cache.py.

def cache_lookup(keys: list[str], count: bool = True) -> dict[str, dict]:
    """Cached verdicts for the keys that have one, counting the hits.

    The hit counter is what makes the saving reportable rather than asserted,
    so the read writes. Chunked for SQLite's parameter ceiling like every other
    id lookup here.

    ``count=False`` is for a read that only decides what to *do* - the planning
    read that splits a fan-out looks the same keys up as the one that serves
    them, and counting both would report every saving twice.
    """
    if not keys:
        return {}
    wanted = list(dict.fromkeys(keys))
    found: dict[str, dict] = {}
    now = _stamp()
    with conn() as c:
        for i in range(0, len(wanted), _ID_CHUNK):
            chunk = wanted[i:i + _ID_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            for row in c.execute(
                f"SELECT * FROM verdict_cache WHERE cache_key IN ({placeholders})",
                tuple(chunk),
            ).fetchall():
                found[row["cache_key"]] = dict(row)
        if found and count:
            c.executemany(
                "UPDATE verdict_cache SET hits = hits + 1, last_hit_at = ? WHERE cache_key = ?",
                [(now, k) for k in found],
            )
    return found


def cache_put(entries: list[dict]) -> int:
    """Remember verdicts for next time. Entries are dicts, not models.

    ``INSERT OR REPLACE`` rather than ``IGNORE``: the same key answered twice is
    the same question answered twice, and keeping the newer answer means a
    re-run after a model upgrade replaces the entry instead of being ignored by
    its own cache.
    """
    if not entries:
        return 0
    now = _stamp()
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO verdict_cache
               (cache_key, domain, policy_version, case_id, judge_model, outcome,
                rationale, confidence, policy_clause, prompt_chars, created_at,
                hits, last_hit_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,
                       COALESCE((SELECT hits FROM verdict_cache WHERE cache_key = ?), 0), '')""",
            [(e["cache_key"], e["domain"], e["policy_version"], e["case_id"],
              e["judge_model"], e["outcome"], e["rationale"], e["confidence"],
              e.get("policy_clause", ""), int(e.get("prompt_chars") or 0), now,
              e["cache_key"]) for e in entries],
        )
    return len(entries)


def cache_stats(domain: str, policy_version: str | None = None) -> dict:
    """Entries, hits, and the prompt volume those hits did not have to re-send."""
    where = "WHERE domain = ?" + (" AND policy_version = ?" if policy_version else "")
    params: tuple = (domain,) if policy_version is None else (domain, policy_version)
    row = query(
        f"""SELECT COUNT(*) AS entries,
                   COALESCE(SUM(hits), 0) AS hits,
                   COALESCE(SUM(hits * prompt_chars), 0) AS hit_prompt_chars,
                   COALESCE(SUM(prompt_chars), 0) AS stored_prompt_chars
            FROM verdict_cache {where}""", params)[0]
    models = query(
        f"SELECT DISTINCT judge_model FROM verdict_cache {where} AND judge_model <> ''",
        params)
    row["models"] = sorted(m["judge_model"] for m in models)
    return row


def save_draft(domain: str, version: str, base_version: str, patch: dict,
               evidence: dict | None = None, verification: dict | None = None,
               drafted_by: str = "", run_id: str = "") -> None:
    """Record a drafted amendment and where it came from.

    Kept out of :data:`DERIVED_TABLES` deliberately. A draft is a written
    document, not an aggregate: the markdown survives a re-seed on disk, and a
    row deleted from under it would leave a policy version on the filesystem
    that nothing can explain.
    """
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO policy_drafts
               (domain, version, base_version, summary, patch, evidence, verification,
                drafted_by, created_at, created_by_run)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (domain, version, base_version, patch.get("summary", ""), json.dumps(patch),
             json.dumps(evidence or {}), json.dumps(verification or {}), drafted_by,
             _stamp(), run_id),
        )


def mark_adopted(domain: str, version: str, adopted_as: str, by: str) -> None:
    """Record that a person promoted this draft, without touching its provenance.

    An UPDATE of three columns rather than a re-INSERT: the patch, the evidence
    and the verification are why the text exists, and they matter most for the
    draft that became a policy.
    """
    with conn() as c:
        c.execute(
            """UPDATE policy_drafts SET adopted_as=?, adopted_by=?, adopted_at=?
               WHERE domain=? AND version=?""",
            (adopted_as, by, _stamp(), domain, version),
        )


def drafts(domain: str) -> list[dict]:
    rows = query("SELECT * FROM policy_drafts WHERE domain=? ORDER BY created_at DESC",
                 (domain,))
    for r in rows:
        r["patch"] = json.loads(r["patch"])
        r["evidence"] = json.loads(r["evidence"] or "{}")
        r["verification"] = json.loads(r["verification"] or "{}")
    return rows


#: What :func:`prune` removes, as (table, WHERE clause). One definition, because
#: a dry run that counts different rows from the one that deletes them is worse
#: than no dry run at all - it is a promise about what is about to happen.
#:
#: ``{domain}`` is filled in with the domain filter or nothing, and every
#: statement takes the cutoff as its first parameter.
_PRUNE: list[tuple[str, str]] = [
    # An entry is dead when it has not been *served* since the cutoff. A
    # never-hit entry is dated by when it was written, so a fresh one survives
    # its first window without having to be used.
    ("verdict_cache",
     "(CASE WHEN last_hit_at <> '' THEN last_hit_at ELSE created_at END) < ?{domain}"),
    # Samples belonging to a stability run older than the cutoff. Dated by the
    # run rather than per row, because a sample carries no timestamp of its own
    # and the run is what makes it current or not. The run's summary row stays,
    # so a disagreement rate is still readable after its samples are gone.
    ("judge_samples",
     """run_id IN (SELECT run_id FROM stability_runs
                   WHERE created_at < ?{domain})"""),
    # Superseded cross-checks. The latest per (domain, version) is kept at any
    # age: latest_cross_check is its only reader, and an absent panel reads as a
    # check that passed rather than as one nobody has run lately.
    ("cross_checks",
     """created_at < ?{domain} AND run_id NOT IN (
          SELECT run_id FROM cross_checks x WHERE x.created_at = (
            SELECT MAX(y.created_at) FROM cross_checks y
            WHERE y.domain = x.domain AND y.policy_version = x.policy_version))"""),
    # Verdicts a later verdict on the same (domain, version, case) replaced.
    #
    # The biggest table here by some distance - one row per case per run, six
    # hundred at a time on the shipped fixture - and the one nothing could drop.
    # Every reader takes MAX(rowid) per case (latest_verdicts, summary,
    # version_totals), so a superseded row is unreachable by construction, which
    # is the same argument that justifies dropping a stranded cache entry. The
    # *newest* verdict per case is never touched at any age: it is what the
    # precedent check, the calibration score and the rule agreement are all
    # computed from, and losing it would make a version look unjudged rather
    # than unchanged.
    ("verdicts",
     """created_at < ?{domain} AND rowid NOT IN (
          SELECT MAX(rowid) FROM verdicts GROUP BY domain, policy_version, case_id)"""),
    # Flip rows a later run replaced, dated by their run because a flip carries
    # no timestamp of its own. Same argument as the verdicts above:
    # flips_for_policy, clause_breakdown and segment_breakdown all collapse to
    # MAX(rowid) per case, so an older row is already invisible - it is only
    # taking up disk and making every one of those joins scan further.
    ("flips",
     """run_id IN (SELECT run_id FROM runs WHERE started_at < ?{domain})
        AND rowid NOT IN (
          SELECT MAX(rowid) FROM flips GROUP BY domain, policy_version, case_id)"""),
]


def _prune_plan(domain: str | None, days: int,
                keep_unhit: bool) -> tuple[str, list[tuple[str, str, tuple]]]:
    """The cutoff, and one (table, where, params) per statement :func:`prune` runs."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(days=max(days, 0))).isoformat()
    suffix = " AND domain = ?" if domain else ""
    params: tuple = (cutoff, domain) if domain else (cutoff,)
    plan = []
    for table, where in _PRUNE:
        clause = where.format(domain=suffix)
        if table == "verdict_cache" and keep_unhit:
            clause += " AND hits > 0"
        plan.append((table, clause, params))
    return cutoff, plan


def prune(domain: str | None = None, days: int = 90,
          keep_unhit: bool = False) -> dict[str, int]:
    """Drop the bulk rows that have stopped earning their disk, and say what went.

    Four tables grow without bound and none of them is ever read once it is old.
    ``verdict_cache`` holds one row per prompt ever judged, so the edit-and-
    re-measure loop this project is built around adds a generation of entries
    per edit and never removes the ones the edit invalidated - they can never be
    hit again, because the key is the prompt and the prompt changed.
    ``judge_samples`` holds one row per (case, repeat) for every stability run
    ever made, and only the newest run backs a reported figure. ``verdicts`` and
    ``flips`` are the same failure one order of magnitude larger: one row per
    case per run, and every reader of both collapses to the newest row per case,
    so a superseded one is unreachable the moment it is written.

    Until this existed the only levers were all-or-nothing:
    :func:`clear_domain_results` drops every derived table including the ones
    that back the dashboard, and :func:`cache_clear` destroys the whole cache -
    including the entries that would have saved the next replay.

    What is deliberately *not* pruned: precedents and their history, the drafts
    table, the aggregates the dashboard reads, and the newest verdict and flip
    row for every case. Age is not a reason to forget a human ruling, a run row
    is what a trend is drawn from, and the current verdict for a case is what
    the precedent check and the calibration score are computed from - dropping
    it would make a version read as unjudged rather than as unchanged.

    ``keep_unhit`` retains cache entries nothing has ever served, which is the
    right setting straight after a big replay nothing has re-run yet; by default
    an entry that is both old and never hit is exactly the stranded generation
    this is for.
    """
    cutoff, plan = _prune_plan(domain, days, keep_unhit)
    cleared: dict[str, int] = {}
    with conn() as c:
        for table, clause, params in plan:
            cleared[table] = c.execute(
                f"DELETE FROM {table} WHERE {clause}", params).rowcount
    return {"cutoff": cutoff, **cleared}


def prune_preview(domain: str | None = None, days: int = 90,
                  keep_unhit: bool = False) -> dict[str, int]:
    """What :func:`prune` would remove, counted with the same clauses it deletes by."""
    cutoff, plan = _prune_plan(domain, days, keep_unhit)
    counted: dict[str, int] = {}
    with conn() as c:
        for table, clause, params in plan:
            counted[table] = c.execute(
                f"SELECT COUNT(*) n FROM {table} WHERE {clause}", params).fetchone()["n"]
    return {"cutoff": cutoff, **counted}


def vacuum() -> dict:
    """Rewrite the database so freed pages go back to the filesystem.

    :func:`prune` deletes rows and the file does not get smaller. That is SQLite
    working as designed - freed pages are kept on a free list and reused - but it
    is also the one genuinely surprising thing about running a prune, and until
    now the only advice was a line of output telling the reader to go and run
    VACUUM themselves, in a shell, against a path the tool already knew.

    Reported as before and after bytes rather than as a bare success, because
    the useful answer is *how much came back*: a prune that removed a thousand
    stranded cache entries and recovered nothing is a database that was already
    compact, which is worth knowing before scheduling another one.

    VACUUM cannot run inside a transaction, so this deliberately does not use
    :func:`conn` - that context manager commits on the way out, and Python's
    sqlite3 opens one implicitly for the statements it thinks are writes.
    ``isolation_level=None`` is autocommit, which is the mode VACUUM needs.
    """
    before = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    try:
        c.execute("VACUUM")
    finally:
        c.close()
    after = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {"bytes_before": before, "bytes_after": after,
            "bytes_reclaimed": max(before - after, 0)}


def cache_clear(domain: str | None = None) -> int:
    with conn() as c:
        if domain:
            return c.execute("DELETE FROM verdict_cache WHERE domain = ?", (domain,)).rowcount
        return c.execute("DELETE FROM verdict_cache").rowcount


def clear_domain_results(domain: str) -> dict[str, int]:
    """Drop every recomputable result for a domain, keeping its precedents.

    Re-seeding replaces the cases. Aggregates computed against the *old* cases
    would otherwise survive and quietly join onto the new ones, so a changed
    fixture size shows up as a dashboard that mixes two different histories.
    """
    cleared: dict[str, int] = {}
    with conn() as c:
        for table in DERIVED_TABLES:
            cur = c.execute(f"DELETE FROM {table} WHERE domain = ?", (domain,))
            cleared[table] = cur.rowcount
    return cleared


def save_stability(run_id: str, domain: str, policy_version: str, samples: list[dict],
                   report: dict, ledger: dict | None = None) -> None:
    """Persist one stability measurement and the samples that produced it."""
    ledger = ledger or {}
    with conn() as c:
        c.execute("DELETE FROM judge_samples WHERE run_id = ?", (run_id,))
        c.executemany(
            """INSERT INTO judge_samples
               (run_id, domain, policy_version, case_id, sample_idx, outcome, confidence, policy_clause)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(run_id, domain, policy_version, s["case_id"], s["sample_idx"],
              s["outcome"], s["confidence"], s.get("policy_clause", "")) for s in samples],
        )
        c.execute(
            """INSERT OR REPLACE INTO stability_runs
               (run_id, domain, policy_version, created_at, cases_sampled, samples_per_case,
                unstable_cases, disagreement_rate, judge_model, estimated_cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, domain, policy_version, _stamp(),
             report["cases_sampled"], report["samples_per_case"], report["unstable_cases"],
             report["disagreement_rate"], ledger.get("judge_model", ""),
             ledger.get("estimated_cost_usd", 0.0)),
        )


# --------------------------------------------------------------- read models
# The queries below back the plugin's API. Each collapses "the latest row per
# case" the same way, because manual replays deliberately overlap backfills.

def clause_breakdown(domain: str, policy_version: str) -> list[dict]:
    """Which clause of the proposed policy is doing the damage.

    The answer a policy author actually needs: not *147 decisions change* but
    *clause 1.1 accounts for 96 of them*, which is the sentence to go and edit.

    Groups on the attribution bucket rather than the raw cited clause, so a
    restriction that stopped firing is credited to the clause that relaxed, and
    flips that were never policy-driven are separated out instead of being
    silently folded into the proposal's impact.
    """
    rows = query(
        """SELECT CASE
                    WHEN f.attribution <> '' THEN f.attribution
                    WHEN f.policy_clause <> '' THEN 'clause ' || f.policy_clause
                    ELSE '(no clause applies)' END AS clause,
                  COUNT(*) AS flips,
                  SUM(CASE WHEN f.direction = 'loosening' THEN 1 ELSE 0 END) AS loosening,
                  SUM(CASE WHEN f.direction = 'tightening' THEN 1 ELSE 0 END) AS tightening,
                  SUM(CASE WHEN f.direction = 'loosening' THEN f.impact ELSE 0 END) AS impact_loosening,
                  SUM(CASE WHEN f.direction = 'tightening' THEN f.impact ELSE 0 END) AS impact_tightening,
                  AVG(f.confidence) AS mean_confidence
           FROM flips f
           JOIN (SELECT case_id, MAX(rowid) latest_rowid FROM flips
                 WHERE domain=? AND policy_version=? GROUP BY case_id) latest
             ON latest.latest_rowid = f.rowid
           GROUP BY clause
           ORDER BY flips DESC""",
        (domain, policy_version),
    )
    for row in rows:
        row["policy_driven"] = row["clause"] != DEVIATION
    return rows


def segment_breakdown(domain: str, policy_version: str) -> list[dict]:
    """Blast radius by segment, counting each case once however many runs saw it.

    Manual replays overlap backfills by design, so the per-run ``segment_stats``
    rows cannot simply be summed: a manual run that replays all of history on
    top of a completed backfill would report 1,200 cases out of 600 and a
    partial overlap would skew the flip *rate*, not just the totals. Counting
    ``case_segments`` instead makes this collapse duplicates the same way every
    other read model here does.
    """
    rows = query(
        """SELECT s.field, s.value,
                  COUNT(*) AS cases,
                  COALESCE(SUM(f.case_id IS NOT NULL), 0) AS flips,
                  COALESCE(SUM(f.direction = 'loosening'), 0) AS loosening,
                  COALESCE(SUM(f.direction = 'tightening'), 0) AS tightening,
                  COALESCE(SUM(CASE WHEN f.direction = 'loosening' THEN f.impact END), 0)
                    AS impact_loosening,
                  COALESCE(SUM(CASE WHEN f.direction = 'tightening' THEN f.impact END), 0)
                    AS impact_tightening
           FROM case_segments s
           LEFT JOIN (
             SELECT g.case_id, g.direction, g.impact FROM flips g
             JOIN (SELECT case_id, MAX(rowid) latest_rowid FROM flips
                   WHERE domain=? AND policy_version=? GROUP BY case_id) latest
               ON latest.latest_rowid = g.rowid
           ) f ON f.case_id = s.case_id
           WHERE s.domain=? AND s.policy_version=?
           GROUP BY s.field, s.value
           ORDER BY s.field, flips DESC""",
        (domain, policy_version, domain, policy_version),
    )
    if rows:
        return rows
    # A database written before case_segments existed still has the per-run
    # aggregates. Summing them is what this function used to do and is correct
    # whenever runs do not overlap, which is the only state such a database can
    # be read in - but re-run the replay to get the deduplicated answer.
    return query(
        """SELECT field, value,
                  SUM(cases) AS cases, SUM(flips) AS flips,
                  SUM(loosening) AS loosening, SUM(tightening) AS tightening,
                  SUM(impact_loosening) AS impact_loosening,
                  SUM(impact_tightening) AS impact_tightening
           FROM segment_stats
           WHERE domain=? AND policy_version=?
           GROUP BY field, value
           ORDER BY field, flips DESC""",
        (domain, policy_version),
    )


def version_totals(domain: str) -> dict[str, dict]:
    """Every policy version this domain has replayed, side by side.

    One query rather than :func:`ptm.report.summary` per version, and
    deduplicated the same latest-row-wins way as every other read model here so
    a manual run overlapping a backfill does not make one version look twice as
    busy as the one beside it. Comparing versions is the whole point, and a
    comparison where the rows were counted differently is worse than none.
    """
    flips = query(
        """SELECT f.policy_version,
                  COUNT(*) AS flips,
                  SUM(CASE WHEN f.direction='loosening' THEN 1 ELSE 0 END) AS loosening,
                  SUM(CASE WHEN f.direction='tightening' THEN 1 ELSE 0 END) AS tightening,
                  SUM(CASE WHEN f.direction='loosening' THEN f.impact ELSE 0 END)
                    AS impact_loosening,
                  SUM(CASE WHEN f.direction='tightening' THEN f.impact ELSE 0 END)
                    AS impact_tightening,
                  SUM(CASE WHEN f.attribution <> ? THEN 1 ELSE 0 END) AS policy_driven,
                  AVG(f.confidence) AS mean_confidence
           FROM flips f
           JOIN (SELECT policy_version, case_id, MAX(rowid) latest_rowid FROM flips
                 WHERE domain=? GROUP BY policy_version, case_id) latest
             ON latest.latest_rowid = f.rowid
           GROUP BY f.policy_version""",
        (DEVIATION, domain),
    )
    judged = query(
        """SELECT policy_version, COUNT(*) AS cases FROM (
             SELECT policy_version, case_id FROM verdicts WHERE domain=?
             GROUP BY policy_version, case_id)
           GROUP BY policy_version""",
        (domain,),
    )
    runs_ = query(
        """SELECT policy_version, COUNT(*) AS runs, MIN(started_at) AS first_run,
                  MAX(started_at) AS last_run,
                  COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost_usd,
                  COALESCE(SUM(actual_cost_usd),0) AS actual_cost_usd
           FROM runs WHERE domain=? GROUP BY policy_version""",
        (domain,),
    )
    models = query(
        """SELECT DISTINCT policy_version, judge_model FROM runs
           WHERE domain=? AND judge_model <> ''""", (domain,))

    out: dict[str, dict] = {}

    def row(version: str) -> dict:
        return out.setdefault(version, {
            "policy_version": version, "runs": 0, "cases": 0, "flips": 0,
            "loosening": 0, "tightening": 0, "impact_loosening": 0.0,
            "impact_tightening": 0.0, "policy_driven_flips": 0,
            "mean_confidence": 0.0, "judged_by": [], "first_run": "", "last_run": "",
            "estimated_cost_usd": 0.0, "actual_cost_usd": 0.0,
        })

    for r in flips:
        row(r["policy_version"]).update({
            k: r[k] for k in ("flips", "loosening", "tightening",
                              "impact_loosening", "impact_tightening")})
        row(r["policy_version"])["policy_driven_flips"] = r["policy_driven"]
        row(r["policy_version"])["mean_confidence"] = round(r["mean_confidence"] or 0, 3)
    for r in judged:
        row(r["policy_version"])["cases"] = r["cases"]
    for r in runs_:
        row(r["policy_version"]).update({
            k: r[k] for k in ("runs", "first_run", "last_run",
                              "estimated_cost_usd", "actual_cost_usd")})
    for r in models:
        row(r["policy_version"])["judged_by"].append(r["judge_model"])
    for r in out.values():
        r["judged_by"] = sorted(r["judged_by"])
    return out


def runs_over_time(domain: str, policy_version: str | None = None) -> list[dict]:
    """Individual runs, oldest first - the trend behind a version's totals.

    Backfill runs are the interesting shape here: two years of replay arrive as
    twenty-four rows, and a flip rate that moves across them is a policy whose
    effect depends on when you ask, which no single headline can show.
    """
    where = "WHERE domain=?" + (" AND policy_version=?" if policy_version else "")
    params: tuple = (domain,) if policy_version is None else (domain, policy_version)
    return query(
        f"""SELECT run_id, policy_version, baseline_version, started_at, cases_replayed,
                   flips, impact, judge_model, estimated_cost_usd, actual_cost_usd
            FROM runs {where} ORDER BY started_at, run_id""", params)


def latest_verdicts(domain: str, policy_version: str) -> dict[str, dict]:
    """The most recent verdict per case for a policy version."""
    rows = query(
        """SELECT v.case_id, v.outcome, v.confidence, v.rationale, v.policy_clause
           FROM verdicts v
           JOIN (SELECT case_id, MAX(rowid) latest_rowid FROM verdicts
                 WHERE domain=? AND policy_version=? GROUP BY case_id) latest
             ON latest.latest_rowid = v.rowid""",
        (domain, policy_version),
    )
    return {r["case_id"]: r for r in rows}


def cost_ledger(domain: str, policy_version: str) -> dict:
    """What judging this policy version has cost so far."""
    row = query(
        """SELECT COUNT(*) AS runs,
                  COALESCE(SUM(estimated_requests),0) AS requests,
                  COALESCE(SUM(estimated_input_tokens),0) AS input_tokens,
                  COALESCE(SUM(estimated_output_tokens),0) AS output_tokens,
                  COALESCE(SUM(estimated_cost_usd),0) AS cost_usd,
                  COALESCE(SUM(actual_requests),0) AS actual_requests,
                  COALESCE(SUM(actual_input_tokens),0) AS actual_input_tokens,
                  COALESCE(SUM(actual_output_tokens),0) AS actual_output_tokens,
                  COALESCE(SUM(actual_cost_usd),0) AS actual_cost_usd
           FROM runs WHERE domain=? AND policy_version=?""",
        (domain, policy_version),
    )[0]
    models = query(
        """SELECT DISTINCT judge_model FROM runs
           WHERE domain=? AND policy_version=? AND judge_model <> ''""",
        (domain, policy_version),
    )
    row["models"] = sorted(m["judge_model"] for m in models)
    row["cost_usd"] = round(row["cost_usd"] or 0, 4)
    row["actual_cost_usd"] = round(row["actual_cost_usd"] or 0, 4)
    return row


def compare_versions(domain: str, left: str, right: str) -> dict:
    """Two candidate policies, case by case.

    The question this answers is *"did my edit to clause 1.1 actually help?"* -
    which a single-version view cannot answer at all. Only cases both versions
    judged are compared, because a case one version never saw is a gap in the
    data rather than a disagreement between the policies.
    """
    a, b = latest_verdicts(domain, left), latest_verdicts(domain, right)
    shared = sorted(set(a) & set(b))
    actual = {
        r["case_id"]: r["actual_outcome"]
        for r in query("SELECT case_id, actual_outcome FROM cases WHERE domain=?", (domain,))
    }
    differences = []
    for case_id in shared:
        if a[case_id]["outcome"] == b[case_id]["outcome"]:
            continue
        differences.append({
            "case_id": case_id,
            "actual_outcome": actual.get(case_id, ""),
            "left_outcome": a[case_id]["outcome"],
            "right_outcome": b[case_id]["outcome"],
            "left_clause": a[case_id]["policy_clause"],
            "right_clause": b[case_id]["policy_clause"],
            "left_confidence": a[case_id]["confidence"],
            "right_confidence": b[case_id]["confidence"],
        })
    return {
        "left": left,
        "right": right,
        "judged_left": len(a),
        "judged_right": len(b),
        "compared": len(shared),
        "agree": len(shared) - len(differences),
        "differ": len(differences),
        "differences": differences,
    }


def save_cross_check(run_id: str, domain: str, policy_version: str, report: dict) -> None:
    """Persist one cross-check, report and all.

    The whole report rather than its headline: the number is a rate, and the
    thing worth keeping is the list of cases underneath it. A rate on its own
    cannot be turned back into the sentences somebody has to go and rewrite.
    """
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO cross_checks
               (run_id, domain, policy_version, primary_judge, secondary_judge,
                created_at, compared, agreed, agreement, clause_agreement,
                contested_flips, report)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, domain, policy_version, report.get("primary", ""),
             report.get("secondary", ""), _stamp(),
             report.get("compared", 0), report.get("agreed", 0),
             report.get("agreement", 0.0), report.get("clause_agreement", 0.0),
             report.get("contested_flips", 0), json.dumps(report)),
        )


def latest_cross_check(domain: str, policy_version: str) -> dict | None:
    rows = query(
        """SELECT * FROM cross_checks WHERE domain=? AND policy_version=?
           ORDER BY created_at DESC LIMIT 1""",
        (domain, policy_version),
    )
    if not rows:
        return None
    row = rows[0]
    row["report"] = json.loads(row["report"])
    return row


def latest_stability(domain: str, policy_version: str) -> dict | None:
    rows = query(
        """SELECT * FROM stability_runs WHERE domain=? AND policy_version=?
           ORDER BY created_at DESC LIMIT 1""",
        (domain, policy_version),
    )
    return rows[0] if rows else None
