"""The two things a database that has been running a while needs, and neither had.

**Export.** Every other measurement in this project can be reached from a shell
with no Airflow and no key. The one artefact built to *leave* the room could
not: :func:`ptm.report.export_bundle` assembles the numbers with their caveats
attached precisely so they cannot be pasted into a slide without them, and it
lived only behind a FastAPI route behind an Airflow login. A decision gets
argued about away from the dashboard, which is exactly when nobody can start it.

**Retention.** ``verdict_cache`` grows *because* the project works - the loop is
edit a clause and measure again, the key is the prompt, so every edit strands
the generation of entries it invalidated. Those rows can never be hit again by
construction. The only lever was ``cache_clear``, which also destroys the
entries about to save the next replay.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from ptm import prune as prune_cli
from ptm import report, store
from ptm.models import Precedent


def utc_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=days)).isoformat()


def entry(key: str, **over) -> dict:
    return {"cache_key": key, "domain": "expenses", "policy_version": "v2",
            "case_id": "c1", "judge_model": "m", "outcome": "approve",
            "rationale": "", "confidence": 1.0, "prompt_chars": 10, **over}


def ruling(case_id: str, who: str = "finance.lead", when: datetime | None = None):
    return Precedent(case_id=case_id, domain="expenses", correct_outcome="deny",
                     ruled_by=who, established_at=when or datetime(2026, 1, 1))


class TestTheExportCli:
    def test_it_writes_the_same_bundle_the_endpoint_serves(self, replayed, tmp_path):
        out = tmp_path / "bundle.json"
        assert report.main(["expenses", "v2", "-o", str(out)]) == 0
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["summary"]["flips"] == report.summary("expenses", "v2")["flips"]

    def test_the_caveats_travel_with_the_numbers(self, replayed, tmp_path):
        """The whole reason the bundle exists rather than a pile of endpoints:
        the figures cannot leave without the sentences that qualify them."""
        out = tmp_path / "bundle.json"
        report.main(["expenses", "v2", "-o", str(out)])
        bundle = json.loads(out.read_text(encoding="utf-8"))
        assert bundle["caveats"]
        assert any("sampling error" in c for c in bundle["caveats"])

    def test_csv_is_the_flip_set_with_a_header(self, replayed, tmp_path):
        out = tmp_path / "flips.csv"
        assert report.main(["expenses", "v2", "--csv", "-o", str(out)]) == 0
        rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
        assert rows and "case_id" in rows[0] and "attribution" in rows[0]

    def test_it_writes_utf8_rather_than_the_platform_default(self, replayed, tmp_path):
        """The bundle carries policy prose and reviewer notes. Read back as
        cp1252 on a Windows checkout they load without error and come back
        mojibake - the same reason policy_text names its encoding."""
        out = tmp_path / "bundle.json"
        report.main(["expenses", "v2", "-o", str(out)])
        assert json.loads(out.read_text(encoding="utf-8"))

    def test_it_prints_to_stdout_so_it_pipes(self, replayed, capsys):
        assert report.main(["expenses", "v2"]) == 0
        assert json.loads(capsys.readouterr().out)["policy_version"] == "v2"

    def test_listing_says_which_version_is_in_force(self, seeded, capsys):
        assert report.main(["--list"]) == 0
        out = capsys.readouterr().out
        assert "expenses" in out and "in force" in out

    def test_an_unknown_version_is_an_error_not_a_traceback(self, seeded, capsys):
        assert report.main(["expenses", "v99"]) == 2
        assert "Unknown policy version" in capsys.readouterr().err

    def test_it_refuses_an_option_it_does_not_know(self, seeded, capsys):
        assert report.main(["expenses", "v2", "--everything"]) == 2
        assert "unknown option" in capsys.readouterr().err


class TestPrune:
    def test_it_drops_an_entry_nothing_has_served_since_the_cutoff(self, fresh_db):
        store.cache_put([entry("stale"), entry("fresh", case_id="c2")])
        with store.conn() as c:
            c.execute("UPDATE verdict_cache SET created_at=? WHERE cache_key='stale'",
                      (utc_days_ago(200),))
        assert store.prune("expenses", days=90)["verdict_cache"] == 1
        left = [r["cache_key"] for r in store.query("SELECT cache_key FROM verdict_cache")]
        assert left == ["fresh"]

    def test_an_entry_still_being_hit_survives_its_own_age(self, fresh_db):
        """Age is not the test - being useless is. An old prompt that is still
        answered from cache is the entry paying for the whole table."""
        store.cache_put([entry("old-but-hot")])
        with store.conn() as c:
            c.execute("UPDATE verdict_cache SET created_at=?, last_hit_at=?, hits=9",
                      (utc_days_ago(200), utc_days_ago(1)))
        assert store.prune("expenses", days=90)["verdict_cache"] == 0

    def test_keep_unhit_retains_a_replay_nothing_has_re_run(self, fresh_db):
        store.cache_put([entry("never-hit")])
        with store.conn() as c:
            c.execute("UPDATE verdict_cache SET created_at=?", (utc_days_ago(200),))
        assert store.prune("expenses", days=90, keep_unhit=True)["verdict_cache"] == 0
        assert store.prune("expenses", days=90)["verdict_cache"] == 1

    def test_the_dry_run_counts_exactly_what_the_real_one_removes(self, fresh_db):
        """A preview that counts different rows from the delete is worse than no
        preview: it is a promise about what is about to happen."""
        store.cache_put([entry(f"k{i}", case_id=f"c{i}") for i in range(5)])
        with store.conn() as c:
            c.execute("UPDATE verdict_cache SET created_at=? "
                      "WHERE cache_key IN ('k0', 'k1', 'k2')", (utc_days_ago(200),))
        assert store.prune_preview("expenses", days=90)["verdict_cache"] == 3
        assert store.prune("expenses", days=90)["verdict_cache"] == 3
        assert store.prune_preview("expenses", days=90)["verdict_cache"] == 0

    def test_it_never_touches_a_human_ruling(self, fresh_db):
        """Precedent is the only thing here that cannot be recomputed, and age
        is not a reason to forget one."""
        store.save_precedent(ruling("c1", when=datetime(2019, 1, 1)))
        store.prune(days=0)
        assert len(store.load_precedents("expenses")) == 1

    def test_it_keeps_the_latest_cross_check_at_any_age(self, fresh_db):
        """latest_cross_check is its only reader, and an absent panel reads as
        a check that passed rather than one nobody has run lately."""
        for run_id in ("old", "newest"):
            store.save_cross_check(run_id, "expenses", "v2",
                                   {"primary": "a", "secondary": "b", "compared": 1})
        with store.conn() as c:
            c.execute("UPDATE cross_checks SET created_at=?", (utc_days_ago(200),))
            c.execute("UPDATE cross_checks SET created_at=? WHERE run_id='newest'",
                      (utc_days_ago(199),))
        store.prune("expenses", days=90)
        assert store.latest_cross_check("expenses", "v2")["run_id"] == "newest"

    def test_it_is_scoped_to_one_domain_when_asked(self, fresh_db):
        store.cache_put([entry("e"), entry("r", domain="refunds", case_id="c2")])
        with store.conn() as c:
            c.execute("UPDATE verdict_cache SET created_at=?", (utc_days_ago(200),))
        store.prune("expenses", days=90)
        domains = [r["domain"] for r in store.query("SELECT domain FROM verdict_cache")]
        assert domains == ["refunds"]


class TestThePruneCli:
    def test_a_dry_run_removes_nothing(self, fresh_db, capsys):
        store.cache_put([entry("stale")])
        with store.conn() as c:
            c.execute("UPDATE verdict_cache SET created_at=?", (utc_days_ago(200),))
        assert prune_cli.main(["--dry-run"]) == 0
        assert "would remove 1" in capsys.readouterr().out
        assert store.query("SELECT COUNT(*) n FROM verdict_cache")[0]["n"] == 1

    def test_it_refuses_a_domain_that_does_not_exist(self, fresh_db, capsys):
        assert prune_cli.main(["nonsuch"]) == 2
        assert "unknown domain" in capsys.readouterr().err

    def test_it_refuses_a_negative_window(self, fresh_db, capsys):
        assert prune_cli.main(["--days", "-5"]) == 2

    def test_it_says_the_file_will_not_shrink_on_its_own(self, fresh_db, capsys):
        """SQLite does not hand freed pages back, so a prune that worked shows
        no change in the file size - the one surprise worth saying out loud.

        It names the flag that fixes it rather than the SQL. Saying "run VACUUM"
        left the reader to go and do by hand, in another tool, against a path
        this command already knew, the one thing it had everything it needed to
        do - which is how the advice got read as a limitation.
        """
        store.cache_put([entry("stale")])
        with store.conn() as c:
            c.execute("UPDATE verdict_cache SET created_at=?", (utc_days_ago(200),))
        prune_cli.main([])
        out = capsys.readouterr().out
        assert "will not shrink" in out
        assert "--vacuum" in out

    def test_the_advice_is_dropped_when_the_flag_already_did_it(self, fresh_db, capsys):
        """Telling somebody to add a flag they just used is noise."""
        store.cache_put([entry("stale")])
        with store.conn() as c:
            c.execute("UPDATE verdict_cache SET created_at=?", (utc_days_ago(200),))
        prune_cli.main(["--vacuum"])
        out = capsys.readouterr().out
        assert "will not shrink" not in out
        assert "vacuumed" in out


class TestTimestamps:
    def test_every_column_is_written_in_one_zone(self, fresh_db):
        """Each of these is compared as TEXT. datetime.now() is local, an
        Airflow task writes UTC, and twice a year the same column holds an hour
        of stamps that sort before rows written before them."""
        written = datetime.fromisoformat(store._stamp())
        assert written.tzinfo is None
        drift = datetime.now(timezone.utc).replace(tzinfo=None) - written
        assert abs(drift.total_seconds()) < 5

    def test_a_tz_aware_ruling_is_normalised_on_the_way_in(self, fresh_db):
        """pendulum.now("UTC") from a DAG and datetime.now() from a CLI are two
        spellings of one instant, and they do not compare as TEXT."""
        aware = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=5)))
        store.save_precedent(ruling("c1", when=aware))
        stored = store.query("SELECT established_at FROM precedents")[0]
        assert stored["established_at"] == "2026-01-01T07:00:00"
        assert store.load_precedents("expenses")[0].established_at.tzinfo is None

    def test_two_supersessions_in_one_tick_both_survive(self, fresh_db, monkeypatch):
        """The archive copy is a plain INSERT in the same transaction as the new
        ruling, so a key collision would roll back and lose *the ruling* - in
        the one table that exists so nothing is silently lost."""
        monkeypatch.setattr(store, "_stamp", lambda: "2026-01-01T00:00:00")
        for who in ("a.human", "b.human", "c.human"):
            store.save_precedent(ruling("c1", who=who))
        archived = store.precedent_history("expenses", "c1")
        assert sorted(r["ruled_by"] for r in archived) == ["a.human", "b.human"]
        assert len({r["superseded_at"] for r in archived}) == 2
        assert store.load_precedents("expenses")[0].ruled_by == "c.human"


class TestNothingPrintsCharactersAConsoleCannotEncode:
    @pytest.mark.parametrize("module", ["ptm.pit_check", "ptm.proposal", "ptm.cost"])
    def test_the_shipped_modules_are_ascii(self, module):
        """A cp1252 console raises UnicodeEncodeError on a printed ellipsis, at
        the end of a command the README advertises."""
        import importlib
        import pathlib
        source = pathlib.Path(importlib.import_module(module).__file__)
        text = source.read_text(encoding="utf-8")
        offenders = sorted({c for c in text if ord(c) > 127})
        assert not offenders, f"{module} carries {offenders}"
