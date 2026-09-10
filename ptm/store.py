"""SQLite persistence.

Deliberately boring. The interesting part is :func:`load_cases`, which
reconstructs each case as it was known on the day it was decided rather than
as it is known today.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
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
    note            TEXT DEFAULT '',
    established_at  TEXT NOT NULL,
    established_by_run TEXT DEFAULT '',
    PRIMARY KEY (domain, case_id)
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
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(SCHEMA)


def load_cases(domain: str, until: datetime, limit: int = 10_000, since: datetime | None = None) -> list[Case]:
    """Load cases decided in ``[since, until)``, hydrated point-in-time.

    Each case's payload is enriched only with facts about its subject that
    were already known on the day the case was decided. This is the whole
    reason the replay is trustworthy: a case decided in March 2025 cannot see
    an attribute that was first recorded in August 2025.
    """
    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM cases
            WHERE domain = ? AND decided_at < ? AND decided_at >= ?
            ORDER BY decided_at
            LIMIT ?
            """,
            (domain, until.isoformat(), (since or datetime.min).isoformat(), limit),
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
                direction, impact, confidence, rationale)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (run_id, domain, policy_version, f.case_id, f.actual_outcome, f.new_outcome,
                 f.direction, f.impact, f.confidence, f.rationale)
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


def save_replay(run_id: str, domain: str, policy_version: str, baseline: str,
                cases_replayed: int, flips: list[Flip], impact: float,
                verdicts: dict[str, Verdict]) -> None:
    """Persist one replay atomically, including an idempotent re-run cleanup."""
    now = datetime.now().isoformat()
    with conn() as c:
        # Airflow retries use the same run id. Clear old rows first so a
        # policy edit cannot leave a no-longer-flipped case visible in the UI.
        c.execute("DELETE FROM verdicts WHERE run_id = ?", (run_id,))
        c.execute("DELETE FROM flips WHERE run_id = ?", (run_id,))
        c.executemany(
            """INSERT INTO verdicts
               (run_id, domain, policy_version, case_id, outcome, rationale, confidence, policy_clause, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(run_id, domain, policy_version, case_id, v.outcome, v.rationale,
              v.confidence, v.policy_clause, now) for case_id, v in verdicts.items()],
        )
        c.executemany(
            """INSERT INTO flips
               (run_id, domain, policy_version, case_id, actual_outcome, new_outcome,
                direction, impact, confidence, rationale)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(run_id, domain, policy_version, f.case_id, f.actual_outcome, f.new_outcome,
              f.direction, f.impact, f.confidence, f.rationale) for f in flips],
        )
        c.execute(
            """INSERT OR REPLACE INTO runs
               (run_id, domain, policy_version, baseline, started_at, cases_replayed, flips, impact)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, domain, policy_version, baseline, now, cases_replayed, len(flips), impact),
        )
