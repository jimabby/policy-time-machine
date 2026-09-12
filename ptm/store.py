"""SQLite persistence.

Deliberately boring. The interesting part is :func:`load_cases`, which
reconstructs each case as it was known on the day it was decided rather than
as it is known today.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DB_PATH
from .models import Case, Flip, Precedent, Verdict

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
    policy_clause  TEXT DEFAULT '',
    reviewed       INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, case_id)
);

-- The one durable artefact. Everything else can be recomputed.
CREATE TABLE IF NOT EXISTS precedents (
    case_id         TEXT NOT NULL,
    domain          TEXT NOT NULL,
    correct_outcome TEXT NOT NULL,
    ruled_by        TEXT NOT NULL,
    note            TEXT DEFAULT '',
    established_at  TEXT NOT NULL,
    established_by_run TEXT DEFAULT '',
    PRIMARY KEY (domain, case_id)
);

-- AI-generated analysis of a run: the brief, the themes, the amendment.
-- Keyed by kind so a newer run's brief replaces an older one for that policy.
CREATE TABLE IF NOT EXISTS insights (
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    generated_by   TEXT NOT NULL,
    run_id         TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (domain, policy_version, kind)
);

-- Candidate policy versions, drafted rather than written by hand. A version
-- lives here instead of in the domain YAML so a DAG can propose one without
-- rewriting a file a human owns, and so its provenance travels with it.
CREATE TABLE IF NOT EXISTS policy_versions (
    domain        TEXT NOT NULL,
    version       TEXT NOT NULL,
    parent        TEXT NOT NULL,
    text          TEXT NOT NULL,
    offline_rules TEXT NOT NULL DEFAULT '[]',
    rationale     TEXT DEFAULT '',
    forced_by     TEXT DEFAULT '[]',
    created_by_run TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    PRIMARY KEY (domain, version)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    domain         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    baseline       TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    cases_replayed INTEGER DEFAULT 0,
    flips          INTEGER DEFAULT 0,
    impact         REAL DEFAULT 0
);
"""


def ts(value: datetime | str) -> str:
    """Normalise a timestamp to a naive-UTC ISO string for storage and comparison.

    ``decided_at`` is compared as TEXT in SQLite, so an offset-aware bound
    (``2025-03-01T00:00:00+00:00``, which is what pendulum hands the DAG) would
    sort *above* a naive value with the same instant and silently drop cases
    sitting exactly on an interval boundary. Everything crossing the SQL
    boundary goes through here.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    """Create the schema, and bring a database from an earlier version forward.

    CREATE TABLE IF NOT EXISTS will not add a column to a table that already
    exists, so columns added after the first release are applied here.
    """
    with conn() as c:
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(flips)")}
        if "policy_clause" not in have:
            c.execute("ALTER TABLE flips ADD COLUMN policy_clause TEXT DEFAULT ''")


def load_cases(domain: str, until: datetime, limit: int = 10_000, since: datetime | None = None,
               spread: bool = False) -> list[Case]:
    """Load cases decided in ``[since, until)``, hydrated point-in-time.

    Each case's payload is enriched only with facts about its subject that
    were already known on the day the case was decided. This is the whole
    reason the replay is trustworthy: a case decided in March 2025 cannot see
    an attribute that was first recorded in August 2025.

    ``spread`` changes what a truncating ``limit`` means. Off (the default, and
    what scheduled runs want) it keeps the earliest cases in the window. On, it
    samples evenly across the window instead, because "the oldest 250 cases" is
    two years of history represented by its first four months - a capped manual
    run that reports on 2024 and calls it a sample of the period.
    """
    lo, hi = ts(since or datetime.min), ts(until)
    with conn() as c:
        if spread:
            ids = [
                r["case_id"] for r in c.execute(
                    "SELECT case_id FROM cases WHERE domain = ? AND decided_at < ? AND decided_at >= ? "
                    "ORDER BY decided_at", (domain, hi, lo)).fetchall()
            ]
            if len(ids) > limit:
                # Take every (n/limit)th case, so the sample tracks the period.
                step = len(ids) / limit
                ids = [ids[int(i * step)] for i in range(limit)]
            placeholders = ",".join("?" * len(ids))
            rows = c.execute(
                f"SELECT * FROM cases WHERE case_id IN ({placeholders}) ORDER BY decided_at",
                ids).fetchall() if ids else []
        else:
            rows = c.execute(
                """
                SELECT * FROM cases
                WHERE domain = ? AND decided_at < ? AND decided_at >= ?
                ORDER BY decided_at
                LIMIT ?
                """,
                (domain, hi, lo, limit),
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
        return cases


def save_verdicts(run_id: str, domain: str, policy_version: str, verdicts: dict[str, Verdict]) -> None:
    now = datetime.now().isoformat()
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO verdicts
               (run_id, domain, policy_version, case_id, outcome, rationale, confidence, policy_clause, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (run_id, domain, policy_version, cid, v.outcome, v.rationale, v.confidence, v.policy_clause, now)
                for cid, v in verdicts.items()
            ],
        )


def save_flips(run_id: str, domain: str, policy_version: str, flips: list[Flip]) -> None:
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO flips
               (run_id, domain, policy_version, case_id, actual_outcome, new_outcome,
                direction, impact, confidence, rationale, policy_clause)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (run_id, domain, policy_version, f.case_id, f.actual_outcome, f.new_outcome,
                 f.direction, f.impact, f.confidence, f.rationale, f.policy_clause)
                for f in flips
            ],
        )


def record_run(run_id: str, domain: str, policy_version: str, baseline: str,
               cases_replayed: int, flips: int, impact: float) -> None:
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO runs
               (run_id, domain, policy_version, baseline, started_at, cases_replayed, flips, impact)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, domain, policy_version, baseline, datetime.now().isoformat(),
             cases_replayed, flips, impact),
        )


def save_precedent(p: Precedent) -> None:
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO precedents
               (case_id, domain, correct_outcome, ruled_by, note, established_at, established_by_run)
               VALUES (?,?,?,?,?,?,?)""",
            (p.case_id, p.domain, p.correct_outcome, p.ruled_by, p.note,
             p.established_at.isoformat(), p.established_by_run),
        )


def load_precedents(domain: str) -> list[Precedent]:
    with conn() as c:
        rows = c.execute("SELECT * FROM precedents WHERE domain = ? ORDER BY established_at", (domain,)).fetchall()
    return [
        Precedent(
            case_id=r["case_id"], domain=r["domain"], correct_outcome=r["correct_outcome"],
            ruled_by=r["ruled_by"], note=r["note"] or "",
            established_at=datetime.fromisoformat(r["established_at"]),
            established_by_run=r["established_by_run"] or "",
        )
        for r in rows
    ]


def save_policy_version(domain: str, version: str, parent: str, text: str,
                        offline_rules: list | None = None, rationale: str = "",
                        forced_by: list[str] | None = None, run_id: str = "") -> None:
    """Register a candidate policy so the engine can judge it like any other.

    ``forced_by`` records which precedents forced the amendment, so a version
    carries the reason it exists rather than just its text.
    """
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO policy_versions
               (domain, version, parent, text, offline_rules, rationale, forced_by,
                created_by_run, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (domain, version, parent, text, json.dumps(offline_rules or []), rationale,
             json.dumps(forced_by or []), run_id, datetime.now().isoformat()),
        )


def load_policy_version(domain: str, version: str) -> dict | None:
    """One candidate policy, or None if this version is a hand-written one."""
    rows = query("SELECT * FROM policy_versions WHERE domain=? AND version=?", (domain, version))
    if not rows:
        return None
    row = rows[0]
    row["offline_rules"] = json.loads(row["offline_rules"])
    row["forced_by"] = json.loads(row["forced_by"])
    return row


def candidate_versions(domain: str) -> list[dict]:
    """Every drafted candidate for a domain, newest first."""
    return query(
        "SELECT domain, version, parent, rationale, created_at, forced_by "
        "FROM policy_versions WHERE domain=? ORDER BY created_at DESC", (domain,))


def save_insight(domain: str, policy_version: str, kind: str, payload: dict,
                 generated_by: str, run_id: str = "") -> None:
    """Store one piece of AI analysis. Newer analysis replaces older for a kind."""
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO insights
               (domain, policy_version, kind, payload, generated_by, run_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (domain, policy_version, kind, json.dumps(payload), generated_by,
             run_id, datetime.now().isoformat()),
        )


def load_insights(domain: str, policy_version: str) -> dict[str, dict]:
    """Every insight for a policy version, keyed by kind."""
    with conn() as c:
        rows = c.execute(
            "SELECT kind, payload, generated_by, created_at FROM insights WHERE domain=? AND policy_version=?",
            (domain, policy_version),
        ).fetchall()
    return {
        r["kind"]: {"generated_by": r["generated_by"], "created_at": r["created_at"],
                    **json.loads(r["payload"])}
        for r in rows
    }


def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Escape hatch for the plugin's read-only API."""
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def flips_for_policy(domain: str, policy_version: str) -> list[dict]:
    """Every flip found for a policy version, one row per case.

    Runs overlap: a backfill's month windows are disjoint, but a manual run
    replays all of history and so re-judges cases an earlier run already
    covered. Returning a row per (run, case) double-counts those - it inflated
    the themes' case counts and put the same case_id in a theme's examples
    twice. The most recent run wins, since it reflects the current policy text.
    """
    return query(
        """SELECT * FROM (
               SELECT f.*, c.payload, c.decided_at, c.actual_rationale,
                      ROW_NUMBER() OVER (
                          PARTITION BY f.case_id ORDER BY r.started_at DESC, f.run_id DESC
                      ) AS rn
               FROM flips f
               JOIN cases c ON c.case_id = f.case_id
               LEFT JOIN runs r ON r.run_id = f.run_id
               WHERE f.domain = ? AND f.policy_version = ?
           ) WHERE rn = 1
           ORDER BY impact DESC""",
        (domain, policy_version),
    )


def policy_summary(domain: str, policy_version: str) -> dict:
    """Cumulative aggregates for a policy, counted per case rather than per run.

    Summing ``runs.cases_replayed`` would double-count any case that more than
    one run judged, so the population is the distinct cases with a verdict.
    This is the single source of truth for both the dashboard tiles and the
    brief - they disagreed when each computed its own.
    """
    cases = query(
        "SELECT COUNT(DISTINCT case_id) n FROM verdicts WHERE domain=? AND policy_version=?",
        (domain, policy_version))[0]["n"]
    runs = query(
        "SELECT COUNT(*) n FROM runs WHERE domain=? AND policy_version=?",
        (domain, policy_version))[0]["n"]

    rows = flips_for_policy(domain, policy_version)
    loosening = [r for r in rows if r["direction"] == "loosening"]
    tightening = [r for r in rows if r["direction"] == "tightening"]
    loose_impact = round(sum(r["impact"] for r in loosening), 2)
    tight_impact = round(sum(r["impact"] for r in tightening), 2)
    return {
        "runs": runs,
        "cases_replayed": cases,
        "flips": len(rows),
        "flip_rate": round(len(rows) / cases, 4) if cases else 0.0,
        "loosening": len(loosening),
        "tightening": len(tightening),
        "impact_loosening": loose_impact,
        "impact_tightening": tight_impact,
        "net_impact": round(loose_impact - tight_impact, 2),
    }


def mark_reviewed(case_ids: list[str]) -> None:
    with conn() as c:
        c.executemany("UPDATE flips SET reviewed = 1 WHERE case_id = ?", [(i,) for i in case_ids])
