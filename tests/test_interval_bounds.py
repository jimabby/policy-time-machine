"""The upper bound a reader passes to :func:`ptm.store.load_cases` is UTC.

:func:`ptm.store._bound` deliberately treats a naive datetime as already-UTC.
That is what makes an Airflow data-interval bound and a seeded ``decided_at``
comparable, and it is right. What it cannot do is tell a naive *UTC* value from
a naive *local* one, so a caller that reached for ``datetime.now()`` handed it
the machine's clock labelled UTC - and every case decided inside the offset
dropped out of the query with nothing said.

Eight call sites did exactly that: the precedent gate, the rule-agreement
score, the proposal's threshold search and its verification pass, the sweep and
the joint sweep, ``pit_check``, the lint's probe, the stability sampler and the
selftest. West of UTC that is silent data loss in the read path - on a UTC-5
host the gate stopped five hours short of the present. East of UTC the bound
merely runs generous, which is why a fixture made entirely of historical cases,
checked by a developer in UTC+10, never once complained.

So the property is pinned two ways. :class:`TestTheBoundIsUTC` proves the
failure is real and that :func:`ptm.store.now_utc` fixes it, by moving the
clock rather than the data. :class:`TestNobodyReachesForTheLocalClock` is the
one that stops it coming back, because the bug was never in the arithmetic -
it was in eight callers each independently reaching for the obvious function.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from ptm import store

REPO = pathlib.Path(__file__).resolve().parents[1]


def _local_now(offset_hours: float) -> datetime:
    """What ``datetime.now()`` returns on a machine at this UTC offset."""
    zone = timezone(timedelta(hours=offset_hours))
    return datetime.now(timezone.utc).astimezone(zone).replace(tzinfo=None)


class TestTheBoundIsUTC:
    def test_a_naive_western_clock_hides_a_case_decided_two_hours_ago(self, fresh_db):
        """The bug, reproduced by moving the clock rather than the data."""
        decided = (datetime.now(timezone.utc) - timedelta(hours=2))
        with store.conn() as c:
            c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                      ("tz-recent", "expenses", "subject-1",
                       decided.replace(tzinfo=None).isoformat(),
                       '{"amount_gbp": 10}', "approve", ""))

        hidden = store.load_cases("expenses", until=_local_now(-5))
        assert [c.case_id for c in hidden] == [], (
            "a naive UTC-5 'now' should sort below a case decided two hours ago - "
            "if this passes the bound, the fixture below is no longer the bug")

        found = store.load_cases("expenses", until=store.now_utc())
        assert [c.case_id for c in found] == ["tz-recent"], (
            "store.now_utc() is the fix: _bound normalises an aware value, so the "
            "case decided two hours ago is inside the window wherever the host is")

    @pytest.mark.parametrize("offset", [-11, -5, -0.5, 0, 5.5, 10, 14])
    def test_now_utc_reaches_the_present_from_every_offset(self, offset, fresh_db):
        """A case decided a minute ago is inside the window from every host.

        Parametrised across the real range because the failure is one-sided,
        and that one-sidedness is what made it invisible: east of UTC the naive
        clock runs *generous*, so a developer in UTC+10 sees this pass while a
        US deployment silently loses the tail of its history. The naive
        assertion underneath is the other half - it records that the zone is
        doing the work here, so a future ``_bound`` that started normalising
        naive values would fail this rather than make it vacuous.
        """
        decided = datetime.now(timezone.utc) - timedelta(minutes=1)
        with store.conn() as c:
            c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                      ("tz-minute", "expenses", "subject-1",
                       decided.replace(tzinfo=None).isoformat(),
                       '{"amount_gbp": 10}', "approve", ""))

        assert [c.case_id for c in store.load_cases("expenses", until=store.now_utc())] \
            == ["tz-minute"], "store.now_utc() must reach the present from any host"

        naive = store.load_cases("expenses", until=_local_now(offset))
        assert bool(naive) is (offset >= 0), (
            f"a naive clock at UTC{offset:+} should {'find' if offset >= 0 else 'miss'} "
            f"a case decided a minute ago; that asymmetry is the bug")

    def test_now_utc_is_aware_so_bound_can_check_it(self):
        """Aware rather than naive-UTC, which is the whole point of the helper."""
        assert store.now_utc().tzinfo is not None
        assert store.now_utc().utcoffset() == timedelta(0)


#: Modules allowed to call ``datetime.now()``: the one that defines what "now"
#: means here, and the seed, which builds a synthetic history rather than
#: reading one.
ALLOWED = {"store.py", "seed.py"}


class TestNobodyReachesForTheLocalClock:
    """No module under ptm/ calls ``datetime.now()`` without a timezone.

    An assertion about the source rather than about behaviour, and deliberately
    so. Every one of the eight call sites was correct-looking, independently
    written, and wrong in the same way; a behavioural test would have to know
    which function to call to catch the ninth. This just refuses the spelling.
    """

    def test_no_module_calls_datetime_now_with_no_argument(self):
        offenders = []
        for path in sorted((REPO / "ptm").glob("*.py")):
            if path.name in ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or node.args or node.keywords:
                    continue
                func = node.func
                # datetime.now() and datetime.datetime.now() alike.
                if isinstance(func, ast.Attribute) and func.attr == "now":
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, (
            f"{offenders} call an argument-less .now(). On a host west of UTC that is "
            f"the machine's local clock handed to store._bound as though it were UTC, "
            f"and every case decided inside the offset disappears from the query. "
            f"Use store.now_utc().")

    def test_the_seed_is_the_only_exception_and_builds_its_own_dates(self):
        """The allow-list is two files; this is what stops it quietly growing."""
        assert ALLOWED == {"store.py", "seed.py"}
        assert (REPO / "ptm" / "store.py").read_text(encoding="utf-8").count(
            "def now_utc(") == 1, "store.now_utc is the one definition the rest import"
