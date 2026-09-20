"""The chores: usage, retention, and the levers a deployment is told it has.

Three things that were true of this project and are not the kind of thing a
feature test catches, because none of them is about what the engine computes.

**Every entry point could be asked for help except one, by accident.** The
modules here parse their own arguments and only :mod:`ptm.sweep` answered
``--help``, because its usage string happened to be what it printed when given
too few arguments. The rest read ``--help`` as the name of a domain: the best of
them said "unknown domain '--help'" and :mod:`ptm.selftest` - the command whose
whole job is to show the project running cleanly - got as far as trying to seed
it and exited on an uncaught ``KeyError``.

That was fixed for nine of them and the list below was written from the same
count, so :mod:`ptm.seed` and :mod:`ptm.pit_check` kept the old behaviour with
nothing to say so - and ``python -m ptm.seed --help`` was the worst of the lot,
because it did not merely fail to print usage, it *seeded every domain*, and
with ``--force`` in front of it would have cleared every derived table for them.
Hence the assertion at the bottom of this file: the list is checked against the
modules that actually have a ``main``, so it cannot fall behind again.

**Retention was the one chore left outside Airflow.** ``verdict_cache`` and
``judge_samples`` grow because the loop works, and the only way to drop them was
to remember to run a command by hand.

**A documented switch that cannot be reached is worse than no switch.** Three
environment levers were read by the code and described in ``.env.example`` and
the README, and none of them was declared in ``docker-compose.yaml`` - so
setting one and running ``make up`` did nothing at all, silently.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from ptm import (
    calibration,
    cli,
    crosscheck,
    disparity,
    gate,
    ingest,
    lint,
    pit_check,
    precedents,
    preflight,
    proposal,
    provenance,
    prune,
    replay,
    report,
    rules,
    seed,
    stability,
    store,
    sweep,
)
from ptm import selftest as selftest_module

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Every ``python -m ptm.*`` entry point, as (module, callable). Listed rather
#: than discovered: a module that stopped having a CLI should fail this list
#: loudly rather than quietly drop out of the sweep below.
ENTRY_POINTS = [
    ("ptm.ingest", ingest.main),
    ("ptm.provenance", provenance.main),
    ("ptm.replay", replay.main),
    ("ptm.preflight", preflight.main),
    ("ptm.calibration", calibration.main),
    ("ptm.rules", rules.main),
    ("ptm.lint", lint.main),
    ("ptm.prune", prune.main),
    ("ptm.sweep", sweep.main),
    ("ptm.report", report.main),
    ("ptm.proposal", proposal.main),
    ("ptm.selftest", selftest_module.cli_main),
    ("ptm.seed", seed.main),
    ("ptm.pit_check", pit_check.main),
    ("ptm.gate", gate.main),
    ("ptm.precedents", precedents.main),
    # The three measurements that had no entry point. ptm.gate's own docstring
    # makes the argument for why that mattered: everything here can be reached
    # from a shell with no Airflow *precisely* so it runs in CI and on a laptop
    # with no key, and a measurement reachable only by triggering a DAG is one
    # nobody takes. Stability was the last of them, and it is the one the whole
    # project quotes as the error bar on every flip rate it reports.
    ("ptm.stability", stability.main),
    ("ptm.crosscheck", crosscheck.main),
    ("ptm.disparity", disparity.main),
]
ENTRY_IDS = [name for name, _ in ENTRY_POINTS]


class TestEveryEntryPointAnswersHelp:
    @pytest.mark.parametrize("flag", ["--help", "-h"])
    @pytest.mark.parametrize("name,main", ENTRY_POINTS, ids=ENTRY_IDS)
    def test_help_exits_zero_and_prints_usage(self, name, main, flag, capsys, seeded):
        """Asking how to use a command is not an error, and must not do work.

        Exit zero specifically: piping ``--help`` into a shell that checks the
        status is a normal thing to do, and the old behaviour - 2 from most of
        them, a traceback from one - makes that read as a broken install.
        """
        assert main([flag]) == 0
        out = capsys.readouterr().out
        assert "usage" in out.lower(), f"{name} printed no usage for {flag}"
        assert name in out, f"{name} usage does not say how to invoke it"

    @pytest.mark.parametrize("name,main", ENTRY_POINTS, ids=ENTRY_IDS)
    def test_help_is_checked_before_anything_is_interpreted(self, name, main, capsys, seeded):
        """A nonsense argument followed by ``--help`` still answers.

        This is the case that matters: help is wanted most by somebody who has
        just got the arguments wrong. Every one of these used to read the
        positional first and fail on it, so the one moment the usage string was
        needed was the one moment it could not be reached.
        """
        assert main(["nosuchdomain", "--help"]) == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_the_list_above_holds_every_module_with_a_cli(self):
        """The list is hand-maintained, which is how two modules fell off it.

        Discovered here rather than in place of the list: a module that stops
        having a CLI should fail loudly instead of quietly dropping out of the
        sweep above, and a module that grows one should fail here instead of
        quietly never being checked. Both directions, because the list was wrong
        in the second one for two releases - ptm.seed answered --help by seeding
        every domain, and nothing in this file could see it.
        """
        import importlib
        import pkgutil

        import ptm

        listed = {name for name, _ in ENTRY_POINTS}
        found = set()
        for module in pkgutil.iter_modules(ptm.__path__):
            loaded = importlib.import_module(f"ptm.{module.name}")
            main = getattr(loaded, "main", None) or getattr(loaded, "cli_main", None)
            # A `main` that is somebody else's import, not this module's own
            # entry point - ptm.report imports nothing of the kind, but a future
            # module might.
            if callable(main) and main.__module__ == loaded.__name__:
                found.add(f"ptm.{module.name}")
        assert found == listed, (
            f"entry points not covered by the --help sweep: {sorted(found - listed)}; "
            f"listed but no longer entry points: {sorted(listed - found)}")

    def test_a_domain_named_help_would_still_be_reachable(self):
        """Only the dashed spellings count.

        A domain is named positionally and ``include/domains/help.yaml`` is a
        legal file, so the bare word must not be swallowed. Nothing ships such a
        domain; the point is that the flag set cannot grow into one.
        """
        assert cli.wants_help(["--help"]) and cli.wants_help(["-h"])
        assert not cli.wants_help(["help"])
        assert not cli.wants_help(["expenses", "v2"])


class TestSelftestRefusesInsteadOfCrashing:
    """The traceback that made the project look broken when the user mistyped."""

    def test_an_unknown_domain_is_an_error_not_a_stack_trace(self, capsys):
        assert selftest_module.cli_main(["nosuchdomain"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("ERROR ")
        assert "Traceback" not in err
        # The message has to name the domains that do exist, or the reader is
        # told they are wrong and not what would have been right.
        assert "expenses" in err

    def test_an_unknown_option_is_refused_with_the_usage(self, capsys):
        assert selftest_module.cli_main(["--replay-everything"]) == 2
        err = capsys.readouterr().err
        assert "--replay-everything" in err and "usage" in err.lower()


class TestVacuum:
    """Prune deletes rows; only this gives the pages back to the filesystem."""

    def _fill(self, n: int = 2000) -> None:
        store.cache_put([{"cache_key": f"k{i}", "domain": "expenses",
                          "policy_version": "v2", "case_id": f"c{i}", "judge_model": "m",
                          "outcome": "approve", "rationale": "x" * 500, "confidence": 1.0,
                          "prompt_chars": 10} for i in range(n)])

    def test_it_reports_what_it_reclaimed(self, fresh_db):
        self._fill()
        store.cache_clear()
        result = store.vacuum()
        assert result["bytes_after"] < result["bytes_before"]
        assert result["bytes_reclaimed"] == result["bytes_before"] - result["bytes_after"]

    def test_it_runs_outside_a_transaction(self, fresh_db):
        """VACUUM raises inside one, which is why it does not use store.conn.

        A regression worth pinning: wrapping this in the module's own context
        manager is the obvious tidy-up, and it does not work.
        """
        assert store.vacuum()["bytes_after"] > 0

    def test_nothing_to_reclaim_is_not_reported_as_a_failure(self, fresh_db):
        store.vacuum()
        assert "already compact" in prune.describe_vacuum(store.vacuum())

    def test_the_cli_vacuums_only_when_asked(self, fresh_db, capsys):
        assert prune.main(["--days", "0"]) == 0
        assert "vacuumed" not in capsys.readouterr().out

        assert prune.main(["--days", "0", "--vacuum"]) == 0
        assert "vacuumed" in capsys.readouterr().out

    def test_a_dry_run_never_rewrites_the_file(self, fresh_db, capsys):
        """--dry-run promises to change nothing, and a rewrite is a change."""
        assert prune.main(["--dry-run", "--vacuum"]) == 0
        out = capsys.readouterr().out
        assert "vacuumed" not in out
        assert "skipped" in out

    def test_the_advice_line_names_the_flag_rather_than_sql(self, fresh_db, capsys):
        """It used to tell the reader to go and run VACUUM themselves."""
        self._fill(1)
        prune.main(["--days", "0"])
        assert "--vacuum" in capsys.readouterr().out


class TestTheDocumentedLeversReachTheContainer:
    """A switch described in two documents and wired into neither.

    Compose reads ``.env`` for ``${...}`` interpolation but passes only what its
    ``environment:`` block declares. Three levers the code reads were described
    in ``.env.example`` and the README and named in neither block, so setting one
    and running ``make up`` did exactly nothing - and the failure is silent,
    which for ``PTM_ALLOW_ANONYMOUS`` means concluding the auth cannot be
    switched off rather than that the switch never arrived.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def compose_env() -> set[str]:
        text = (REPO / "docker-compose.yaml").read_text(encoding="utf-8")
        return set(re.findall(r"^\s{6}(PTM_[A-Z_]+):", text, re.MULTILINE))

    def test_every_ptm_variable_the_code_reads_is_declared(self, compose_env):
        read: set[str] = set()
        for path in sorted((REPO / "ptm").glob("*.py")) + [
                REPO / "plugins" / "policy_time_machine_plugin.py"]:
            source = path.read_text(encoding="utf-8")
            # Both spellings: os.environ.get("PTM_X"), and the constant-then-get
            # indirection the plugin uses for PTM_ALLOW_ANONYMOUS - which is
            # exactly the one a plain grep for environ missed.
            read |= set(re.findall(r'environ(?:\.get)?\(\s*"(PTM_[A-Z_]+)"', source))
            read |= set(re.findall(r'^[A-Z_]+ = "(PTM_[A-Z_]+)"', source, re.MULTILINE))
        missing = read - compose_env
        assert not missing, (
            f"{sorted(missing)} are read by the code but not declared in "
            f"docker-compose.yaml, so setting them in .env reaches nothing")

    @pytest.mark.parametrize("name", ["PTM_ALLOW_ANONYMOUS", "PTM_CACHE",
                                      "PTM_CACHE_EPOCH"])
    def test_the_levers_env_example_advertises_are_among_them(self, name, compose_env):
        example = (REPO / ".env.example").read_text(encoding="utf-8")
        assert name in example, f"{name} is no longer documented; drop it from this list"
        assert name in compose_env

    def test_the_explorer_stays_shut_by_default(self, compose_env):
        """The default for the anonymous lever must be empty, not 1.

        Declaring it is what makes it settable; declaring it *on* would be a
        different bug in the opposite direction.
        """
        text = (REPO / "docker-compose.yaml").read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if "PTM_ALLOW_ANONYMOUS:" in ln)
        assert "-}" in line, f"expected an empty default, got: {line.strip()}"
        assert ":-1}" not in line


class TestDisparityRatioIsRejectedRatherThanInverted:
    """``max_ratio`` is read twice, and below 1 the two readings overlap."""

    @pytest.mark.parametrize("bad", [1.0, 0.5, 0.0, -2.0])
    def test_a_multiplier_at_or_below_one_is_refused(self, bad):
        from pydantic import ValidationError

        from ptm.config import DisparityPolicy

        with pytest.raises(ValidationError, match="must be greater than 1"):
            DisparityPolicy(max_ratio=bad)

    def test_the_shipped_domains_are_above_it(self, seeded):
        from ptm.config import available_domains, load_domain

        for name in available_domains():
            assert load_domain(name).disparity.max_ratio > 1.0
