"""Test fixtures.

``ptm.config`` resolves paths from the environment at import time, so tests
redirect the database by patching the name ``ptm.store`` actually reads rather
than by setting env vars after the fact.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PTM_INCLUDE_DIR", str(ROOT / "include"))
os.environ.setdefault("PTM_OFFLINE", "1")

from ptm import store  # noqa: E402
from ptm.config import load_domain  # noqa: E402
from ptm.models import Case  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty, schema'd database isolated to one test."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "ptm.db")
    store.init_db()
    return store


@pytest.fixture
def expenses():
    return load_domain("expenses")


@pytest.fixture
def refunds():
    return load_domain("refunds")


def insert_case(db, case_id: str, decided_at: str, payload: dict, outcome: str,
                subject_id: str = "sub-1", domain: str = "expenses") -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO cases (case_id, domain, subject_id, decided_at, payload, "
            "actual_outcome, actual_rationale) VALUES (?,?,?,?,?,?,'')",
            (case_id, domain, subject_id, decided_at, __import__("json").dumps(payload), outcome),
        )


def insert_fact(db, subject_id: str, key: str, value: str, known_from: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO subject_facts (subject_id, key, value, known_from) VALUES (?,?,?,?)",
            (subject_id, key, value, known_from),
        )


def case(case_id="c-1", payload=None, outcome="approve", decided="2025-06-15T12:00:00"):
    return Case(case_id=case_id, domain="expenses", decided_at=datetime.fromisoformat(decided),
                payload=payload or {}, actual_outcome=outcome)
