"""The state where every case is already answered, which broke both fan-outs.

``to_judge`` returns only the cache misses. When there are none, the mapped
judge task expands over an empty list, Airflow marks it **skipped**, and under
the default ``all_success`` rule that skip propagates to ``merge`` - leaving
``reconcile``/``enforce`` (both ``none_failed``) to run with no verdicts and
fail the run.

That is not a corner case. It is the documented steady state of both callers:

- the precedent gate fires on every recorded ruling and re-asks the same
  questions about the same handful of cases, and its own comment says so;
- a replay re-run over a window the policy edit does not reach asks nothing new
  either, and ``ptm.selftest`` prints exactly this state as the headline win:
  *"600 of 600 verdicts come from cache, 0 would be judged again."*

The fix is one trigger rule, declared on ``merge`` itself so a future call site
cannot forget it. These tests cover both halves: that the state is reachable at
all, and that ``merge`` produces a correct full result when it arrives.
"""

from __future__ import annotations

from datetime import datetime

from ptm import cache, store
from ptm.config import load_domain
from ptm.judge import build_prompt, offline_verdict

JUDGE = "offline"


def items_for(domain_name: str, version: str, limit: int) -> tuple[list[dict], list]:
    domain = load_domain(domain_name)
    cases = store.load_cases(domain_name, until=datetime(2026, 9, 1), limit=limit)
    return [{"case_id": c.case_id, "domain": domain_name,
             "cache_key": cache.key(build_prompt(c, domain, version), JUDGE),
             "prompt_chars": 100} for c in cases], cases


class TestTheStateIsReachable:
    def test_a_second_pass_over_the_same_prompts_has_nothing_to_judge(self, fresh_db, seeded):
        """The gate's normal second run, and any replay re-run."""
        domain = load_domain("expenses")
        items, cases = items_for("expenses", "v2", 8)

        first, _ = cache.split(items, count=False)
        assert len(first) == len(items), "nothing is cached yet, so everything is a miss"

        cache.remember("expenses", "v2", JUDGE,
                       {c.case_id: (i["cache_key"], 100, offline_verdict(c, domain, "v2"))
                        for c, i in zip(cases, items)})

        second, hits = cache.split(items, count=False)
        assert second == [], (
            "every prompt is already answered, so the fan-out is empty and the mapped "
            "judge task is skipped - which is what the trigger rule has to survive")
        assert len(hits) == len(items)


class TestMergeSurvivesIt:
    """``merge`` is reached with an empty fan-out and has to produce the full set."""

    def merged(self, items: list[dict], misses: list[dict], fresh) -> dict:
        """What the task body does, without Airflow: see ptm_dags/common.py."""
        fresh = list(fresh or [])
        assert len(fresh) == len(misses), "a partial pass must refuse, not merge"
        cached = cache.lookup([i["cache_key"] for i in items])
        return {i["case_id"]: cached[i["cache_key"]] for i in items
                if i["cache_key"] in cached}

    def test_every_verdict_comes_back_when_none_were_judged(self, fresh_db, seeded):
        domain = load_domain("expenses")
        items, cases = items_for("expenses", "v2", 8)
        cache.remember("expenses", "v2", JUDGE,
                       {c.case_id: (i["cache_key"], 100, offline_verdict(c, domain, "v2"))
                        for c, i in zip(cases, items)})
        misses, _ = cache.split(items, count=False)

        # fresh is None, which is what a skipped mapped task resolves to.
        out = self.merged(items, misses, None)
        assert len(out) == len(items), (
            "a replay whose cases are all cached must still produce a verdict for each "
            "of them; anything less is the 'refusing partial replay' failure")
        for case in cases:
            assert out[case.case_id].outcome == offline_verdict(case, domain, "v2").outcome


class TestTheTriggerRuleIsDeclared:
    """Asserted on the source as well as the DagBag, because the engine job runs
    without Airflow and this is the job that would notice first."""

    def test_merge_carries_none_failed(self):
        import pathlib

        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "ptm_dags" / "common.py").read_text(encoding="utf-8")
        decorated = source[:source.index("def merge(")]
        assert decorated.rstrip().endswith('@task(trigger_rule="none_failed")'), (
            "merge must not run under all_success: an empty fan-out skips the judge, "
            "the skip propagates, and the run fails with no verdicts")
