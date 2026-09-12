"""The verdict cache: not paying twice for the same question.

A cache standing between a policy edit and the number somebody acts on has one
way to be useless (never hitting) and one way to be dangerous (hitting when the
question has changed). Almost everything here is about the second.
"""

from __future__ import annotations

from ptm import cache, store
from ptm.judge import build_prompt
from ptm.models import Verdict


def verdict(outcome: str = "deny") -> Verdict:
    return Verdict(outcome=outcome, rationale="matched", confidence=0.9, policy_clause="1.1")


class TestTheKey:
    def test_the_same_prompt_and_model_is_the_same_key(self):
        assert cache.key("a prompt", "m") == cache.key("a prompt", "m")

    def test_a_different_model_is_a_different_question(self):
        """Serving one model's verdict as another's would make a
        model-comparison run agree with itself perfectly and mean nothing."""
        assert cache.key("a prompt", "cheap") != cache.key("a prompt", "expensive")

    def test_editing_the_policy_changes_the_key_for_every_case_it_reaches(self, expenses, seeded):
        """The reason the key is the prompt and not (case, version): editing a
        clause does not change the version label, so a (case, version) key would
        serve every stale verdict as though the policy had not moved."""
        case = store.load_cases("expenses", until=__import__("datetime").datetime(2026, 9, 1),
                                limit=1)[0]
        before = cache.key(build_prompt(case, expenses, "v2"), "m")
        edited = expenses.model_copy(update={"judge_instructions": "and also this"})
        assert cache.key(build_prompt(case, edited, "v2"), "m") != before

    def test_two_different_cases_never_share_a_key(self, expenses, seeded):
        import datetime

        cases = store.load_cases("expenses", until=datetime.datetime(2026, 9, 1), limit=2)
        keys = {cache.key(build_prompt(c, expenses, "v2"), "m") for c in cases}
        assert len(keys) == 2


class TestLookupAndRemember:
    def test_a_remembered_verdict_comes_back_intact(self, fresh_db):
        cache.remember("expenses", "v2", "m", {"a": ("key-a", 1200, verdict("partial"))})
        found = cache.lookup(["key-a"])
        assert found["key-a"].outcome == "partial"
        assert found["key-a"].policy_clause == "1.1"

    def test_an_unknown_key_is_simply_absent(self, fresh_db):
        assert cache.lookup(["never-stored"]) == {}

    def test_hits_are_counted_so_the_saving_is_measured_rather_than_asserted(self, fresh_db):
        cache.remember("expenses", "v2", "m", {"a": ("key-a", 1200, verdict())})
        cache.lookup(["key-a"])
        cache.lookup(["key-a"])
        assert store.cache_stats("expenses", "v2")["hits"] == 2

    def test_re_answering_the_same_key_replaces_rather_than_is_ignored(self, fresh_db):
        """A re-run after a model upgrade must not be ignored by its own cache."""
        cache.remember("expenses", "v2", "m", {"a": ("key-a", 10, verdict("deny"))})
        cache.remember("expenses", "v2", "m", {"a": ("key-a", 10, verdict("approve"))})
        assert cache.lookup(["key-a"])["key-a"].outcome == "approve"
        assert store.cache_stats("expenses", "v2")["entries"] == 1

    def test_turning_the_cache_off_stops_reads_but_not_writes(self, fresh_db, monkeypatch):
        """Switching it off is a statement about what you are willing to read -
        usually because you stopped trusting an entry - and should not also
        throw away the run you just paid for."""
        monkeypatch.setattr(cache, "ENABLED", False)
        cache.remember("expenses", "v2", "m", {"a": ("key-a", 10, verdict())})
        assert cache.lookup(["key-a"]) == {}
        monkeypatch.setattr(cache, "ENABLED", True)
        assert cache.lookup(["key-a"])["key-a"].outcome == "deny"


class TestSplittingAFanOut:
    def test_an_item_with_no_key_is_always_judged(self, fresh_db):
        """This is how the stability fan-out opts out: it carries no key, so
        every repeat of the same prompt is really asked again."""
        misses, hits = cache.split([{"case_id": "a"}, {"case_id": "b", "cache_key": "k"}])
        assert [m["case_id"] for m in misses] == ["a", "b"]
        assert hits == {}

    def test_answered_items_leave_the_fan_out_and_come_back_by_case_id(self, fresh_db):
        cache.remember("expenses", "v2", "m", {"b": ("k", 10, verdict("partial"))})
        items = [{"case_id": "a", "cache_key": "unknown"},
                 {"case_id": "b", "cache_key": "k"}]
        misses, hits = cache.split(items)
        assert [m["case_id"] for m in misses] == ["a"]
        assert hits["b"].outcome == "partial"

    def test_the_baseline_pass_hits_the_cache_independently(self, fresh_db):
        """Editing the candidate policy does not change the one in force, so its
        half of the judging is served from cache while the candidate's is not."""
        cache.remember("expenses", "v1", "m", {"a": ("base-key", 10, verdict())})
        items = [{"case_id": "a", "cache_key": "new-candidate-key",
                  "baseline_cache_key": "base-key"}]
        assert cache.split(items)[0], "the candidate half misses"
        assert cache.split(items, "baseline_cache_key")[1], "the baseline half hits"

    def test_the_saving_is_priced_from_the_pass_that_was_skipped(self, fresh_db):
        items = [{"case_id": "a", "prompt_chars": 4000, "baseline_prompt_chars": 40}]
        candidate = cache.saving(items, "anthropic:claude-sonnet-5")
        baseline = cache.saving(items, "anthropic:claude-sonnet-5", "baseline_prompt_chars")
        assert candidate["estimated_saved_usd"] > baseline["estimated_saved_usd"] > 0
        assert candidate["cache_hits"] == 1


class TestItIsDerivedData:
    def test_a_re_seed_drops_it(self, fresh_db):
        """Its keys are hashes of prompts built from the old cases, so after a
        re-seed not one of them could ever be hit again."""
        cache.remember("expenses", "v2", "m", {"a": ("key-a", 10, verdict())})
        store.clear_domain_results("expenses")
        assert cache.lookup(["key-a"]) == {}

    def test_precedents_survive_what_the_cache_does_not(self, fresh_db):
        assert "precedents" not in store.DERIVED_TABLES
        assert "verdict_cache" in store.DERIVED_TABLES
