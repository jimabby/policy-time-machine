"""The DAG module: it must import cleanly and build what it claims to build.

Airflow is a heavy dependency, so the import-dependent checks skip when it is
absent and the structural ones run either way. CI installs Airflow, which is
what backs the README's claim of zero import errors.
"""

from __future__ import annotations

import os
import pathlib
import signal

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
#: The file Airflow parses. It is now an index and a registration loop; the DAGs
#: themselves are built one module per DAG in ``ptm_dags/``.
DAG_FILE = REPO / "dags" / "policy_time_machine.py"
DAG_PKG = REPO / "ptm_dags"
#: Every module the DAG layer is spread across. Read as a list rather than as
#: one path, because the checks below are about the layer and a check that
#: scanned only the entry point would pass while the thing it forbids sat in a
#: builder - which is exactly what splitting the file could have cost.
DAG_SOURCES = [DAG_FILE, *sorted(DAG_PKG.glob("*.py"))]
#: The five per-domain builders. ``common.py`` is helpers and ``__init__.py`` is
#: a docstring, so neither is one of these.
BUILDERS = ("replay", "adjudicate", "precedent_gate", "judge_stability", "propose")


def layer() -> str:
    """Every line of the DAG layer, for a check that is about all of it."""
    return "\n".join(path.read_text(encoding="utf-8") for path in DAG_SOURCES)


def builder(name: str) -> str:
    """One builder module's source, for a check that is about one DAG."""
    return (DAG_PKG / f"{name}.py").read_text(encoding="utf-8")

#: Why Airflow could not be imported, if it could not. Kept rather than
#: discarded: a bare "not installed" skip is what let the CI job whose entire
#: purpose is running these tests report success without ever running one.
airflow_import_error: str | None = None
try:  # pragma: no cover - depends on the environment
    import airflow  # noqa: F401
except Exception as exc:  # pragma: no cover - any failure means we cannot parse
    airflow_import_error = f"{type(exc).__name__}: {exc}"

has_airflow = airflow_import_error is None

# Locally, Airflow is a heavy optional dependency and skipping is right. In the
# job that installs it on purpose, a skip is a false pass - so CI sets this and
# the skip becomes a failure that says why.
if not has_airflow and os.environ.get("PTM_REQUIRE_AIRFLOW") == "1":  # pragma: no cover
    raise RuntimeError(
        "PTM_REQUIRE_AIRFLOW=1 but Airflow could not be imported, so the DAG-parse "
        f"tests would have silently skipped. Import failed with: {airflow_import_error}"
    )

needs_airflow = pytest.mark.skipif(
    not has_airflow, reason=f"Airflow could not be imported ({airflow_import_error})")

#: Why DagBag cannot be used here, if it cannot. Separate from importing Airflow
#: at all, because the two fail for unrelated reasons and only one of them is
#: about this project.
#:
#: ``DagBag`` bounds how long a DAG file may take to import by arming an alarm -
#: ``signal.SIGALRM`` and ``signal.setitimer``, neither of which exists on
#: Windows. The collection then raises before a single DAG is built and
#: ``bag.dags`` comes back empty, so every assertion below fails with
#: ``'replay_<domain>' not in {}``: seventeen failures that look like the DAG
#: module is broken and are nothing of the kind. Airflow says plainly that it
#: runs on POSIX and warns about it on import; the Makefile still supports a
#: Windows checkout for the *engine*, which needs none of this.
#:
#: So it is named rather than endured. The refusal below is what stops the skip
#: becoming the other failure this file exists to prevent - CI sets
#: PTM_REQUIRE_AIRFLOW and runs on Linux, where SIGALRM exists, so a skip there
#: would mean something has genuinely changed and it is turned back into an error.
dagbag_error: str | None = None
if has_airflow and not hasattr(signal, "SIGALRM"):  # pragma: no cover - platform
    dagbag_error = (
        "Airflow's DagBag arms a SIGALRM import timeout, and this platform has no "
        "SIGALRM. Airflow supports POSIX only; run the DAG tests in the container "
        "(make up) or under WSL2. The engine tests need none of this.")

can_parse = has_airflow and dagbag_error is None

if has_airflow and dagbag_error and os.environ.get("PTM_REQUIRE_AIRFLOW") == "1":  # pragma: no cover
    raise RuntimeError(
        "PTM_REQUIRE_AIRFLOW=1 but the DAG-parse tests cannot run here, so they would "
        f"have silently skipped: {dagbag_error}")

#: Always a string, never None. ``skipif`` with a boolean condition rejects a
#: reason of None outright - "you need to specify reason=STRING when using
#: booleans as conditions" - and it does so at *fixture setup*, as an error
#: rather than a failure. On the platform where everything works, which is the
#: one CI runs on, that would have turned twenty-two passing tests into twenty-two
#: errors. The reason is what pytest prints for a skip, so it is only ever read
#: when one happens; that is exactly why the unread branch has to be safe.
DAGBAG_SKIP_REASON = (
    f"Airflow could not be imported ({airflow_import_error})" if not has_airflow
    else dagbag_error or "DagBag is usable on this platform")

needs_dagbag = pytest.mark.skipif(not can_parse, reason=DAGBAG_SKIP_REASON)


class TestStatic:
    """Runs with or without Airflow installed."""

    def test_every_module_compiles(self):
        import py_compile

        for path in DAG_SOURCES:
            py_compile.compile(str(path), doraise=True)

    def test_each_builder_module_builds_exactly_one_dag(self):
        """The point of the split. A module that grows a second DAG is a module
        whose name has stopped describing it, and the next reader looking for
        ``propose_<domain>`` will not find it where the package says it is."""
        for name in BUILDERS:
            source = builder(name)
            assert source.count("@dag(") == 1, f"{name}.py builds more than one DAG"
            assert f"dag_id=f\"{name}_{{domain_name}}\"" in source, \
                f"{name}.py does not build the DAG it is named after"

    def test_the_entry_point_registers_every_builder(self):
        """A builder nobody calls is a DAG that silently stopped existing."""
        entry = DAG_FILE.read_text(encoding="utf-8")
        for name in BUILDERS:
            assert f"{name}.build(ctx)" in entry, f"{name} is never registered"

    def test_no_domain_knowledge_leaks_into_the_dag_layer(self):
        """The engine's central claim: swapping the YAML swaps the application.

        A domain word hard-coded here would mean the pipeline only looks generic.
        """
        for path in DAG_SOURCES:
            source = path.read_text(encoding="utf-8").lower()
            for word in ("expense", "refund", "receipt", "gbp", "grade", "tier", "claim"):
                assert word not in source, \
                    f"{word!r} in {path.name} is domain knowledge, it belongs in the YAML"

    def test_prompts_are_not_carried_in_the_case_payload(self):
        """Every prompt embeds the whole policy text. Putting one in the dict that
        fans out to each judge task duplicates the policy per case, through XCom,
        for a value the offline judge never reads."""
        source = layer()
        assert '"prompt": build_prompt' not in source
        assert '"prompt_chars"' in source

    def test_human_answers_are_never_zipped_against_a_short_list(self):
        """``record`` runs on all_done so a failed review does not strand the
        others - but a short response list zipped positionally against the full
        flip list would file one reviewer's ruling against somebody else's
        case. Precedent cannot be recomputed, so it has to refuse."""
        source = builder("adjudicate")
        assert "if len(responses) != len(flips):" in source
        assert "attribute a ruling to the wrong case" in source

    def test_the_gate_loads_precedent_cases_by_id(self):
        """Loading everything and filtering is subject to the default limit, so
        past that many cases the gate would check a subset and still pass."""
        source = builder("precedent_gate")
        assert "case_ids=ids" in source
        assert "if c.case_id in ids" not in source, "the filtered load is the truncating one"

    def test_the_stability_fan_out_is_the_one_that_never_carries_a_cache_key(self):
        """Judging the same prompt repeatedly *is* the stability measurement. A
        cached answer would be served to every repeat and report a judge that
        never contradicts itself - not a wrong number, a reassuring one."""
        stability_dag = builder("judge_stability")
        assert "cacheable=False" in stability_dag
        assert "cache_key" not in stability_dag
        # common.py is excluded on purpose: it *defines* the flag, and its
        # docstring names it. The claim is about the five DAGs that use it.
        for name in BUILDERS:
            if name == "judge_stability":
                continue
            assert "cacheable=False" not in builder(name), \
                f"only the stability fan-out opts out of the cache; {name} must not"

    def test_nothing_tries_to_map_over_one_key_of_a_multiple_output_task(self):
        """Airflow refuses it - *cannot map over XCom with custom key* - and it
        refuses at DAG import, so the whole file fails to parse and every DAG in
        it disappears. It is valid Python, so `py_compile` above says nothing;
        only an Airflow-installed environment catches it, which is a slow place
        to find out. A task whose output is expanded over returns a plain list.
        """
        assert "multiple_outputs" not in layer()

    def test_the_reviewer_is_asked_for_a_reason(self):
        """`record` reads params_input['note'] and the drafter renders it. If
        the operator does not declare the parameter, every precedent on file
        carries an empty note and nobody notices."""
        source = builder("adjudicate")
        review = source[source.index("HITLOperator.partial("):]
        review = review[:review.index(".expand(")]
        assert "params=" in review and '"note"' in review

    def test_the_precedent_records_what_it_was_a_ruling_about(self):
        """A ruling with no policy version attached cannot be re-read later -
        see ptm.diff.stale_precedents."""
        source = builder("adjudicate")
        record = source[source.index("store.save_precedent("):]
        record = record[:record.index("saved.append")]
        for field in ("policy_version=", "judged_outcome=", "judged_clause="):
            assert field in record

    def test_nothing_imports_an_llm_only_name_unconditionally(self):
        """``LLMOperator`` and ``UsageLimits`` do not exist offline.

        While this was one module they were defined behind ``if not OFFLINE``
        and every use of them sat behind the same test, so their absence cost
        nothing. Splitting the file turned those uses into imports, and an
        import of a name that does not exist is not a quiet absence - it is an
        ImportError at parse time that takes every DAG in the package with it.
        Offline is the default, so this would have been the first thing anybody
        running the demo saw.
        """
        for name in BUILDERS:
            source = builder(name)
            for llm_only in ("LLMOperator", "UsageLimits"):
                if llm_only not in source:
                    continue
                imported = [line for line in source.splitlines()
                            if line.startswith("    from ptm_dags.common import")
                            and llm_only in line]
                assert imported, (
                    f"{name}.py uses {llm_only} but does not import it under "
                    f"`if not OFFLINE:` - an unindented import of it fails offline")

    def test_the_proposer_can_be_run_without_it_writing_anything(self):
        """Proposing and adopting are separate acts, and the directory it writes
        into is the one a person is accountable for."""
        source = builder("propose")
        assert '"publish": Param(' in source
        assert 'ctx["params"].get("publish")' in source

    def test_per_case_segments_are_persisted(self, seeded):
        """Summing the per-run aggregates counts a case once per run that saw
        it, and manual runs overlap backfills on purpose.

        Asserted against the behaviour rather than the source text. The grep
        this replaced passed on a reformat that broke the write and failed on a
        rename that did not, which is the wrong way round for both.
        """
        from datetime import datetime

        from ptm import diff, store
        from ptm.config import load_domain
        from ptm.judge import offline_verdict

        domain = load_domain("expenses")
        cases = store.load_cases("expenses", until=datetime(2026, 9, 1), limit=25)
        verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
        flips = diff.flips(cases, verdicts, domain)
        for run in ("overlap-a", "overlap-b"):
            store.save_replay(run, "expenses", "v2-segment-probe", "actual", len(cases),
                              flips, 0.0, verdicts,
                              segments=diff.segment_stats(cases, flips, domain),
                              case_segments=diff.case_segment_rows(cases, domain))
        rows = store.segment_breakdown("expenses", "v2-segment-probe")
        by_field: dict[str, int] = {}
        for row in rows:
            by_field[row["field"]] = by_field.get(row["field"], 0) + row["cases"]
        assert by_field, "the replay wrote no per-case segment rows at all"
        for field, counted in by_field.items():
            assert counted == len(cases), \
                f"{field} counted {counted} of {len(cases)} cases across two overlapping runs"

    def test_the_cap_on_a_manual_run_is_not_applied_to_a_scheduled_one(self):
        """The parameter says "Ignored by scheduled and backfilled runs" and it
        was passed on every run, so a month holding more cases than the cap
        replayed its oldest 250 and reported a rate for a window it had only
        partly seen."""
        source = builder("replay")
        prepare = source[source.index("def prepare("):source.index("def prompts(")]
        assert "if manual else None" in prepare, \
            "the cap has to be conditional on the run being a manual one"


    def test_retention_is_one_dag_for_the_whole_database(self):
        """Not generated per domain, unlike every other DAG here.

        The tables it prunes are shared by every domain, the cutoff is a
        property of the database rather than of any rulebook, and the file it
        rewrites is one file. Five copies would take five locks on one SQLite
        database to do the same work once - so this is the one DAG built outside
        the per-domain loop, and a future refactor that folds it back into
        build() should have to notice.
        """
        entry = DAG_FILE.read_text(encoding="utf-8")
        per_domain = entry[entry.index("def build(domain_name"):entry.index("for _name in")]
        assert "retention" not in per_domain, \
            "retention belongs outside the per-domain loop"
        registration = entry[entry.index("for _name in"):]
        assert "retention.build()" in registration
        # Its builder takes no domain, which is the version of this claim that
        # a refactor cannot talk its way around.
        assert "def build() -> None:" in (DAG_PKG / "retention.py").read_text(encoding="utf-8")


@needs_dagbag
class TestParses:
    @pytest.fixture(scope="class")
    def dagbag(self):
        from airflow.models.dagbag import DagBag

        bag = DagBag(dag_folder=str(REPO / "dags"), include_examples=False)
        assert not bag.import_errors, bag.import_errors
        return bag

    def test_five_dags_per_domain(self, dagbag, seeded):
        from ptm.config import available_domains

        for name in available_domains():
            for prefix in ("replay", "adjudicate", "precedent_gate", "judge_stability",
                           "propose"):
                assert f"{prefix}_{name}" in dagbag.dags


    def test_retention_runs_on_a_schedule_rather_than_by_hand(self, dagbag):
        """The chore that was left outside Airflow.

        verdict_cache and judge_samples grow *because* the loop works - the key
        is the prompt, so every clause edit strands the generation of entries it
        invalidated - and the only lever was a command somebody had to remember
        to run. In a project arguing that Airflow is the engine rather than the
        wrapper, that was the odd one out.
        """
        dag = dagbag.dags["ptm_retention"]
        assert dag.schedule == "@weekly"
        assert {"days", "domain", "keep_unhit", "dry_run", "vacuum"} <= set(dag.params)

    def test_retention_counts_before_it_deletes_and_compacts_after(self, dagbag):
        """Three steps in order, and the order is the point.

        The count comes from the same WHERE clauses the delete runs, so the log
        says what is about to go before it goes. The compaction is last and
        separate because VACUUM rewrites the whole file - the one step whose
        cost is proportional to the database rather than to what was dropped.
        """
        dag = dagbag.dags["ptm_retention"]
        assert {t.task_id for t in dag.tasks} == {"plan", "sweep_up", "compact"}
        assert [t.task_id for t in dag.get_task("plan").downstream_list] == ["sweep_up"]
        assert [t.task_id for t in dag.get_task("sweep_up").downstream_list] == ["compact"]

    def test_retention_defaults_to_doing_the_work_but_can_only_count(self, dagbag):
        """Scheduled runs should actually prune; a curious click should be able
        to ask what would happen without it happening."""
        dag = dagbag.dags["ptm_retention"]
        assert dag.params["dry_run"] is False
        assert dag.params["vacuum"] is True
        assert dag.params["days"] == 90

    def test_retention_does_not_multiply_by_domain(self, dagbag, seeded):
        from ptm.config import available_domains

        for name in available_domains():
            assert f"ptm_retention_{name}" not in dagbag.dags
        assert "ptm_retention" in dagbag.dags

    def test_replay_is_schedulable_and_backfillable(self, dagbag):
        dag = dagbag.dags["replay_expenses"]
        assert dag.schedule == "@monthly"
        assert {"policy_version", "max_cases", "baseline_version"} <= set(dag.params)

    def test_replay_reads_the_policy_before_it_judges_anything(self, dagbag):
        """Free, and it catches the problems that make a paid replay unusable
        rather than merely expensive."""
        dag = dagbag.dags["replay_expenses"]
        assert "read_the_policy" in {t.task_id for t in dag.tasks}
        assert dag.params.get_param("preflight").schema.get("enum") == ["warn", "fail", "off"]

    def test_replay_can_be_made_to_stop_on_an_uneven_change(self, dagbag):
        dag = dagbag.dags["replay_expenses"]
        assert dag.params.get_param("disparity_gate").schema.get("enum") == \
            ["domain", "warn", "fail", "off"]

    def test_the_cache_sits_in_front_of_both_fan_outs_that_repeat_work(self, dagbag):
        """The replay re-judges history after every policy edit; the gate
        re-judges the same precedents every time a ruling is recorded."""
        for dag_id, task_id in (("replay_expenses", "to_judge"),
                                ("precedent_gate_expenses", "gate_to_judge")):
            assert task_id in {t.task_id for t in dagbag.dags[dag_id].tasks}

    def test_the_proposer_is_manual_only(self, dagbag):
        """It writes a policy version and costs money. It must not fire on a
        schedule."""
        assert dagbag.dags["propose_expenses"].schedule is None

    def test_the_proposer_ends_in_the_gate_rather_than_in_a_summary(self, dagbag):
        """The whole reason a model is allowed to write here: the draft is
        re-judged against every ruling a human has made."""
        tasks = {t.task_id for t in dagbag.dags["propose_expenses"].tasks}
        assert {"verification_items", "record"} <= tasks

    def test_adjudication_wakes_on_the_flips_asset(self, dagbag):
        """Nothing polls; the replay emitting the asset is what starts this."""
        dag = dagbag.dags["adjudicate_expenses"]
        assert [a.uri for a in dag.schedule] == ["ptm://expenses/flips"]

    def test_the_gate_wakes_on_the_precedents_asset(self, dagbag):
        dag = dagbag.dags["precedent_gate_expenses"]
        assert [a.uri for a in dag.schedule] == ["ptm://expenses/precedents"]

    def test_stability_is_manual_only(self, dagbag):
        """It costs real money per run, so it must not fire on a schedule."""
        assert dagbag.dags["judge_stability_expenses"].schedule is None

    def test_stability_requires_at_least_two_samples(self, dagbag):
        # dag.params resolves to plain values; get_param returns the Param
        # itself, which is where the validation schema lives.
        params = dagbag.dags["judge_stability_expenses"].params
        assert params.get_param("samples_per_case").schema.get("minimum") == 2

    def test_stability_can_target_the_recorded_flips(self, dagbag):
        """The aggregate noise floor and per-flip confirmation are two
        questions sharing one fan-out."""
        params = dagbag.dags["judge_stability_expenses"].params
        assert params.get_param("target").schema.get("enum") == ["sample", "flips"]

    def test_the_gate_judges_the_policy_in_force_as_well(self, dagbag):
        """So a reversal the status quo already makes is not reported as the
        proposal's doing."""
        dag = dagbag.dags["precedent_gate_expenses"]
        assert "baseline_version" in dag.params
        assert any("baseline" in t.task_id for t in dag.tasks), \
            [t.task_id for t in dag.tasks]

    def test_adjudication_reports_what_it_held_back(self, dagbag):
        """A flip silently dropped from the queue looks exactly like a flip
        that never happened."""
        dag = dagbag.dags["adjudicate_expenses"]
        assert "unconfirmed" in {t.task_id for t in dag.tasks}

    def test_the_merges_survive_a_fan_out_with_nothing_in_it(self, dagbag):
        """Every case already answered means the judge expands over an empty
        list and Airflow skips it. Under all_success that skip reaches the
        merge, and the run then fails for having no verdicts - which is the
        gate's normal second run and any replay re-run. See test_empty_fanout.
        """
        from airflow.task.trigger_rule import TriggerRule

        for dag_id, task_ids in (
                ("replay_expenses", ("merge", "merge_baseline")),
                ("precedent_gate_expenses", ("gate_merge", "gate_merge_baseline"))):
            tasks = {t.task_id: t for t in dagbag.dags[dag_id].tasks}
            for task_id in task_ids:
                assert tasks[task_id].trigger_rule == TriggerRule.NONE_FAILED, \
                    f"{dag_id}.{task_id} would be skipped by an empty fan-out"

    def test_stale_rulings_can_be_put_back_in_front_of_a_human(self, dagbag):
        """The gate has always warned that a ruling was made about a clause
        since rewritten, and enforced it anyway. This is the route to fixing
        one, which is what the warning has always told people to do."""
        params = dagbag.dags["adjudicate_expenses"].params
        assert params.get_param("target").schema.get("enum") == ["flips", "stale"]

    def test_the_proposer_can_replay_its_own_draft(self, dagbag):
        """verify() checks the draft against the precedent set and says so;
        what it does to every other case still costs a replay."""
        dag = dagbag.dags["propose_expenses"]
        tasks = {t.task_id: t for t in dag.tasks}
        assert {"should_replay", "replay_the_draft"} <= set(tasks)
        assert tasks["replay_the_draft"].trigger_dag_id == "replay_expenses"
        # Off by default: it is a full replay and a full bill.
        assert dag.params["replay"] is False
        # A triggered run has no data interval, so replay applies its manual
        # cap - and a draft measured on 250 cases is not comparable with the
        # backfill it is being judged against.
        assert tasks["replay_the_draft"].conf["max_cases"] == 0

    def test_every_task_belongs_to_a_domain_tagged_dag(self, dagbag):
        for dag_id, dag in dagbag.dags.items():
            assert "policy-time-machine" in dag.tags
            assert dag.tasks, f"{dag_id} has no tasks"
