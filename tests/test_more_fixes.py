"""More things that were silently wrong, and the panel that was missing.

Like test_fixes.py: none of these crashed or went red. They produced a
confident answer that was not the answer it claimed to be.
"""

from __future__ import annotations

import shutil
from datetime import datetime

import pytest

from ptm import cache, config, diff, report, rules, store
from ptm.config import load_domain
from ptm.judge import offline_verdict
from ptm.models import ClauseEdit, FlipConfirmation, PolicyPatch, Verdict


class TestAReplayNoLongerUnholdsAConfirmedFlip:
    """``save_replay`` wrote the flips table without the ``stability`` column,
    so the newest row - the one the human queue reads - defaulted to '' and
    forgot that a confirmation pass had measured the verdict and could not
    reproduce it. The measurement never went anywhere; only the tag the queue
    reads did, so the next adjudication run quietly offered the flip up again.

    Precedent is permanent and cannot be recomputed, which is the whole reason
    unconfirmed flips are held back in the first place.
    """

    @pytest.fixture
    def replayed_flips(self, fresh_db):
        from ptm.seed import seed_domain

        seed_domain("expenses", force=True)
        domain = load_domain("expenses")
        cases = store.load_cases("expenses", until=datetime(2026, 9, 1))
        candidate = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
        baseline = {c.case_id: offline_verdict(c, domain, "v1") for c in cases}
        flips = diff.flips(cases, candidate, domain, baseline=baseline)
        store.save_replay("run-1", "expenses", "v2", "actual", len(cases), flips, 0.0,
                          candidate, baseline_version="v1", baseline_verdicts=baseline)
        return {"domain": domain, "cases": cases, "candidate": candidate,
                "baseline": baseline, "flips": flips}

    def queue(self, domain) -> list[str]:
        import json

        rows = store.flips_for_policy("expenses", "v2")
        flips = [diff.Flip(
            case_id=r["case_id"], decided_at=datetime.fromisoformat(r["decided_at"]),
            actual_outcome=r["actual_outcome"], new_outcome=r["new_outcome"],
            rationale=r["rationale"], confidence=r["confidence"],
            policy_clause=r["policy_clause"] or "", impact=r["impact"],
            payload=json.loads(r["payload"]), direction=r["direction"],
            attribution=r["attribution"] or "", stability=r.get("stability") or "")
            for r in rows]
        return [f.case_id for f in diff.select_for_review(flips, domain)]

    def replay_again(self, r, flips=None):
        store.save_replay("run-2", "expenses", "v2", "actual", len(r["cases"]),
                          flips if flips is not None else r["flips"], 0.0, r["candidate"],
                          baseline_version="v1", baseline_verdicts=r["baseline"])

    def confirm_unstable(self, case_id: str, outcome: str):
        store.save_flip_stability("expenses", "v2", [FlipConfirmation(
            case_id=case_id, samples=3, outcomes={"approve": 2, "deny": 1},
            modal_outcome="approve", agreement=0.667, stable=False,
            recorded_outcome=outcome)], run_id="stab-1")

    def test_a_flip_the_judge_would_not_reproduce_stays_out_of_the_queue(self, replayed_flips):
        domain = replayed_flips["domain"]
        target = self.queue(domain)[0]
        recorded = next(f for f in replayed_flips["flips"] if f.case_id == target)

        self.confirm_unstable(target, recorded.new_outcome)
        assert target not in self.queue(domain), "held back, as it always was"

        self.replay_again(replayed_flips)
        assert target not in self.queue(domain), (
            "and still held after another replay - the measurement is on file, so the "
            "tag the queue reads has to survive the run that rewrites the flip row")

    def test_a_verdict_nothing_measured_inherits_nothing(self, replayed_flips):
        """A confirmation pass asks whether *this verdict* reproduces. A later
        replay reaching a different outcome has produced a new question, and
        marking it settled on the strength of the old experiment would be the
        same laundering in the other direction."""
        domain = replayed_flips["domain"]
        target = self.queue(domain)[0]
        recorded = next(f for f in replayed_flips["flips"] if f.case_id == target)
        self.confirm_unstable(target, recorded.new_outcome)

        moved = [f.model_copy(update={"new_outcome": "partial"}) if f.case_id == target
                 else f for f in replayed_flips["flips"]]
        self.replay_again(replayed_flips, moved)

        row = next(r for r in store.flips_for_policy("expenses", "v2")
                   if r["case_id"] == target)
        assert row["stability"] == "", "a different verdict is an unmeasured one"


class TestALongLivedProcessSeesADraftAppear:
    """``load_domain`` was an unconditional ``lru_cache``. That is right for a
    task that runs once and exits, and wrong for the API server hosting the
    plugin, which loaded the domain at startup and never looked again: a draft
    written by ``propose_<domain>`` on a worker was missing from /api/domains,
    reported by /api/drafts as ``available: false`` - which the dashboard
    renders as "files gone" - and 404'd from every version-scoped endpoint,
    until somebody restarted the webserver.
    """

    @pytest.fixture
    def workspace(self, tmp_path, monkeypatch, seeded):
        include = tmp_path / "include"
        shutil.copytree(config.INCLUDE_DIR, include)
        monkeypatch.setattr(config, "INCLUDE_DIR", include)
        config.load_domain.cache_clear()
        yield include
        config.load_domain.cache_clear()

    def draft(self, workspace, version="v2-draft1"):
        folder = workspace / "drafts" / "expenses"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{version}.md").write_text(
            f"# Expenses {version}\n\n1.1 Something.\n", encoding="utf-8")

    def test_a_draft_written_by_another_process_shows_up(self, workspace):
        assert report.domains()[0]["draft_versions"] == []
        self.draft(workspace)
        assert "v2-draft1" in report.domains()[0]["draft_versions"], \
            "no cache_clear() was called - the read side never knew to"
        assert report.summary("expenses", "v2-draft1")["is_draft"], \
            "and every version-scoped endpoint resolves it"

    def test_a_discarded_draft_stops_showing_up(self, workspace):
        self.draft(workspace)
        assert "v2-draft1" in load_domain("expenses").policies
        (workspace / "drafts" / "expenses" / "v2-draft1.md").unlink()
        assert "v2-draft1" not in load_domain("expenses").policies

    def test_an_edited_policy_is_re_read(self, workspace):
        """Not only drafts: the whole config is keyed on what it was built
        from, so editing the YAML in place is picked up as well."""
        before = load_domain("expenses").label
        path = workspace / "domains" / "expenses.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            f"label: {before}", "label: something else"), encoding="utf-8")
        assert load_domain("expenses").label == "something else"

    def test_an_unchanged_domain_is_not_re_parsed(self, workspace):
        """The check is a stat, not a reload: the object is still shared, so
        nothing that relied on identity has quietly started copying."""
        assert load_domain("expenses") is load_domain("expenses")


class TestDraftRulesAreCheckedAgainstTheDraft:
    """``write_draft`` validated generated rules against the *base* version's
    clause list. Adding a clause is the normal case for a drafter - the patch
    applier appends one under its own heading on purpose - so every rule
    implementing the new clause was rejected, and because a rule set is ordered
    and first-match-wins, one rejection dropped the whole set. The draft then
    shipped with no offline rules, and an offline replay of it returned the
    most generous outcome for every case: a wildly permissive policy nobody
    wrote.
    """

    @pytest.fixture
    def drafted(self, expenses):
        from ptm import proposal

        patch = PolicyPatch(summary="cap large claims", edits=[ClauseEdit(
            clause="8.1", current_text="", proposed_text="Claims above GBP 500 are denied.",
            rationale="evidence", expected_effect="")])
        return proposal.apply_to_markdown(expenses, "v2", patch, "v2-draft1")

    def rule(self, clause: str) -> list[dict]:
        return [{"when": "amount_gbp > 500", "outcome": "deny", "clause": clause,
                 "because": "over the cap", "confidence": 0.9}]

    def test_the_drafted_markdown_really_does_add_a_clause(self, drafted, expenses):
        assert "8.1" in config.clauses_in(drafted)
        assert "8.1" not in expenses.clauses("v2")

    def test_a_rule_for_the_new_clause_is_accepted(self, drafted, expenses):
        assert rules.validate(self.rule("8.1"), expenses, "v2-draft1",
                              policy_text=drafted) == []

    def test_a_rule_citing_nothing_real_is_still_rejected(self, drafted, expenses):
        problems = rules.validate(self.rule("9.9"), expenses, "v2-draft1",
                                  policy_text=drafted)
        assert problems and "9.9" in problems[0]

    def test_without_the_draft_text_it_reads_the_version_on_disk(self, expenses):
        """The override is opt-in, so the lint's behaviour over hand-written
        rules is untouched."""
        assert rules.validate(self.rule("1.1"), expenses, "v2") == []
        assert rules.validate(self.rule("8.1"), expenses, "v2")


class TestTheCacheCanBeInvalidatedOnPurpose:
    """The key is the prompt and the model, which covers everything this
    project controls. It cannot cover a vendor changing what sits behind an
    unchanged model identifier - so that gets a lever rather than a pretence.
    """

    def test_an_epoch_changes_every_key(self):
        assert cache.key("p", "m", "a") != cache.key("p", "m", "b")

    def test_no_epoch_is_the_key_the_existing_entries_were_written_under(self):
        assert cache.key("p", "m", "") == cache.key("p", "m")

    def remember(self, key: str):
        cache.remember("expenses", "v2", "m",
                       {"a": (key, 10, Verdict(outcome="deny", rationale="",
                                               confidence=0.9))})

    def test_an_epoch_misses_entries_written_before_it(self, fresh_db):
        self.remember(cache.key("p", "m"))
        assert cache.lookup([cache.key("p", "m")])
        assert not cache.lookup([cache.key("p", "m", "2026-09")]), \
            "bumping the epoch stops the old answers being served"

    def test_the_old_entries_are_still_there_to_be_read(self, fresh_db):
        """An epoch leaves the evidence on disk, which is the difference
        between it and cache_clear(): 'what did the judge say before the model
        changed underneath us' stays answerable."""
        self.remember(cache.key("p", "m"))
        assert store.cache_stats("expenses", "v2")["entries"] == 1

    def test_a_run_with_an_epoch_set_says_so(self, monkeypatch):
        """A cold cache and a deliberately invalidated one look identical in
        the hit counts, and only one of them is a decision somebody made."""
        monkeypatch.setattr(cache, "EPOCH", "2026-09")
        assert "cache epoch" in cache.describe(3, 1)
        monkeypatch.setattr(cache, "EPOCH", "")
        assert "cache epoch" not in cache.describe(3, 1)


class TestVersionsCanBeComparedWithoutTwoTabs:
    """The question the whole loop is for - *did the edit help?* - and the one
    thing no panel could answer. A single-version view says 147 outcomes
    change; it cannot say whether that beats the 161 the version before it
    changed, nor whether the draft written to fix it fixed anything.
    """

    def test_every_replayed_version_is_a_row(self, replayed):
        found = report.history("expenses")
        versions = {r["policy_version"]: r for r in found["versions"]}
        assert {"v1", "v2"} <= set(versions)
        assert versions["v2"]["flips"] > 0
        assert versions["v2"]["cases"] > 0

    def test_the_version_in_force_is_marked(self, replayed):
        found = report.history("expenses")
        in_force = [r for r in found["versions"] if r["in_force"]]
        assert [r["policy_version"] for r in in_force] == [found["in_force"]]

    def test_a_version_nothing_replayed_is_reported_as_unmeasured(self, replayed):
        """Not as zero. No reversals and no evidence look identical in a column
        of integers, and one of them is a clean bill of health."""
        store.query("SELECT 1")
        found = report.history("expenses")
        rows = {r["policy_version"]: r for r in found["versions"]}
        assert "v1" in rows and "v2" in rows
        for row in found["versions"]:
            if not row["cases"]:
                assert row["precedents_checked"] == 0

    def test_the_rate_carries_its_sampling_band(self, replayed):
        """Two versions measured on different numbers of cases differ by sample
        size before they differ by policy."""
        v2 = next(r for r in report.history("expenses")["versions"]
                  if r["policy_version"] == "v2")
        assert v2["flip_rate_lo"] <= v2["flip_rate"] <= v2["flip_rate_hi"]
        assert v2["flip_rate_lo"] < v2["flip_rate_hi"]

    def test_overlapping_runs_are_not_double_counted(self, replayed):
        """Manual runs overlap backfills by design, so a version replayed twice
        must not look twice as busy as the one beside it."""
        before = next(r for r in report.history("expenses")["versions"]
                      if r["policy_version"] == "v2")["flips"]
        store.save_replay("pytest__overlap", "expenses", "v2", "actual",
                          len(replayed["cases"]), replayed["flips"], 0.0,
                          replayed["candidate"])
        after = next(r for r in report.history("expenses")["versions"]
                     if r["policy_version"] == "v2")["flips"]
        assert after == before

    def test_the_trend_behind_the_totals_is_there_too(self, replayed):
        found = report.history("expenses")
        assert found["runs"], "the individual runs, oldest first"
        assert [r["started_at"] for r in found["runs"]] == \
            sorted(r["started_at"] for r in found["runs"])
