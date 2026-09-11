"""SQLite persistence.

Deliberately boring. The interesting parts are :func:`load_cases`, which
reconstructs each case as it was known on the day it was decided rather than as
it is known today, and :func:`save_replay`, which writes one replay atomically
along with the aggregates the dashboard reads.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

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
    PRIMARY KEY (domain, version)
);

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
]


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


def load_cases(domain: str, until: datetime, limit: int = 10_000,
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
    filtering, because a default ``limit`` silently truncating the set would
    make the regression suite check fewer precedents than exist and still pass.
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
        # Callers reason chronologically regardless of which end was sampled.
        cases.sort(key=lambda c: c.decided_at)
        return cases


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


def save_replay(run_id: str, domain: str, policy_version: str, baseline: str,
                cases_replayed: int, flips: list[Flip], impact: float,
                verdicts: dict[str, Verdict], segments: list[dict] | None = None,
                ledger: dict | None = None, baseline_version: str = "",
                baseline_verdicts: dict[str, Verdict] | None = None,
                case_segments: list[dict] | None = None) -> None:
    """Persist one replay atomically, including an idempotent re-run cleanup."""
    now = datetime.now().isoformat()
    ledger = ledger or {}
    baseline_run = run_id + BASELINE_RUN_SUFFIX
    with conn() as c:
        # Airflow retries use the same run id. Clear old rows first so a
        # policy edit cannot leave a no-longer-flipped case visible in the UI.
        c.execute("DELETE FROM verdicts WHERE run_id IN (?, ?)", (run_id, baseline_run))
        c.execute("DELETE FROM flips WHERE run_id = ?", (run_id,))
        c.execute("DELETE FROM segment_stats WHERE run_id = ?", (run_id,))
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
                direction, impact, confidence, rationale, policy_clause, segments,
                attribution, baseline_outcome)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(run_id, domain, policy_version, f.case_id, f.actual_outcome, f.new_outcome,
              f.direction, f.impact, f.confidence, f.rationale, f.policy_clause,
              json.dumps(f.segments), f.attribution, f.baseline_outcome) for f in flips],
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
                estimated_output_tokens, estimated_cost_usd, judge_model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, domain, policy_version, baseline, now, cases_replayed, len(flips), impact,
             baseline_version,
             ledger.get("estimated_requests", 0), ledger.get("estimated_input_tokens", 0),
             ledger.get("estimated_output_tokens", 0), ledger.get("estimated_cost_usd", 0.0),
             ledger.get("judge_model", "")),
        )


def save_verdicts(run_id: str, domain: str, policy_version: str,
                  verdicts: dict[str, Verdict]) -> None:
    """Persist verdicts from a pass that is not a replay.

    The precedent gate judges cases too, and storing what it found is what lets
    the dashboard show the gate's answer without paying to re-judge it.
    """
    now = datetime.now().isoformat()
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
    now = datetime.now().isoformat()
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


#: Everything derived from a domain's cases. Precedents are deliberately absent:
#: they are the one durable artefact and survive a re-seed on purpose. The
#: verdict cache goes: its keys are hashes of prompts built from the *old*
#: cases, so after a re-seed not one of them can ever be hit again.
DERIVED_TABLES = ("verdicts", "flips", "segment_stats", "case_segments", "runs",
                  "judge_samples", "stability_runs", "flip_stability", "verdict_cache")


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
    now = datetime.now().isoformat()
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
    now = datetime.now().isoformat()
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
             datetime.now().isoformat(), run_id),
        )


def drafts(domain: str) -> list[dict]:
    rows = query("SELECT * FROM policy_drafts WHERE domain=? ORDER BY created_at DESC",
                 (domain,))
    for r in rows:
        r["patch"] = json.loads(r["patch"])
        r["evidence"] = json.loads(r["evidence"] or "{}")
        r["verification"] = json.loads(r["verification"] or "{}")
    return rows


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
            (run_id, domain, policy_version, datetime.now().isoformat(),
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
                  COALESCE(SUM(estimated_cost_usd),0) AS cost_usd
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


def latest_stability(domain: str, policy_version: str) -> dict | None:
    rows = query(
        """SELECT * FROM stability_runs WHERE domain=? AND policy_version=?
           ORDER BY created_at DESC LIMIT 1""",
        (domain, policy_version),
    )
    return rows[0] if rows else None
