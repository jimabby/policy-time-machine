"""Test fixtures.

The environment has to be set before anything under ``ptm`` is imported, because
:mod:`ptm.config` resolves its paths at import time. pytest loads ``conftest.py``
ahead of the test modules, so this is the one place that can do it.

Domain configuration and policies come from the real ``include/`` directory -
these tests check the *shipped* fixtures, not a parallel set that could drift
from them. Only the database is disposable.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="ptm-tests-"))

os.environ["PTM_INCLUDE_DIR"] = str(REPO / "include")
os.environ["PTM_DB"] = str(_TMP / "ptm.db")
# Not forced: the DAG-parse test has to be runnable in both configurations,
# because the LLM-backed branch imports operators the offline branch never
# touches - so parsing offline only ever proves half the module.
os.environ.setdefault("PTM_OFFLINE", "1")
# A price override in the developer's shell would otherwise change what the
# cost assertions expect.
os.environ.pop("PTM_PRICE", None)

import pytest  # noqa: E402

from ptm import cost, diff, store  # noqa: E402
from ptm.config import load_domain  # noqa: E402
from ptm.judge import offline_verdict  # noqa: E402
from ptm.seed import seed_all  # noqa: E402


@pytest.fixture(scope="session")
def seeded() -> dict:
    """Both shipped domains, seeded once for the whole session."""
    store.init_db()
    return seed_all()


@pytest.fixture(scope="session")
def expenses(seeded):
    return load_domain("expenses")


@pytest.fixture(scope="session")
def replayed(seeded):
    """A full point-in-time replay of expenses under v2, with a baseline pass.

    Mirrors what a backfill of ``replay_expenses`` produces, so the assertions
    below are about the pipeline's real output rather than a toy input.
    """
    from datetime import datetime

    domain = load_domain("expenses")
    cases = store.load_cases("expenses", until=datetime(2026, 9, 1))
    candidate = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
    baseline = {c.case_id: offline_verdict(c, domain, "v1") for c in cases}
    flips = diff.flips(cases, candidate, domain, baseline=baseline)
    store.save_replay("pytest__full", "expenses", "v2", "actual", len(cases), flips,
                      diff.summarise(flips, len(cases), domain)["net_impact"], candidate,
                      segments=diff.segment_stats(cases, flips, domain),
                      ledger=cost.zero(), baseline_version="v1",
                      baseline_verdicts=baseline)
    return {"domain": domain, "cases": cases, "candidate": candidate,
            "baseline": baseline, "flips": flips}


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    """An empty database, isolated from the session one.

    ``store`` binds ``DB_PATH`` at import, so the patch has to land on the
    ``store`` module's own name rather than on ``config``.
    """
    path = tmp_path / "isolated.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    store.init_db()
    return path
