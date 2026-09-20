"""``ptm_retention`` - what in the database has stopped earning its disk?

One maintenance DAG for the whole database, not one per domain, and the only
module in this package whose builder takes no domain.
"""

from __future__ import annotations

from airflow.exceptions import AirflowFailException
from airflow.sdk import Param, dag, task

from ptm import prune, store
from ptm.config import available_domains
from ptm_dags.common import DEFAULTS, START


def build() -> None:
    """One maintenance DAG for the whole database, not one per domain.

    Every other DAG here is generated per domain because a policy, its cases and
    its precedents are a domain's own. Retention is not: ``verdict_cache`` and
    ``judge_samples`` are single tables shared by every domain, the cutoff is a
    property of the database rather than of any rulebook, and a VACUUM rewrites
    one file. Five copies of this would take five locks on one SQLite file to do
    the same work once.

    Why it exists at all: these two tables grow without bound, and they grow
    *because the loop works*. The cache key is the prompt, so every clause edit
    strands the whole generation of entries it invalidated - they can never be
    hit again, by construction - and a stability run leaves one row per (case,
    repeat) of which only the newest run backs a reported figure. The engine has
    had ``ptm.prune`` for a while and the only way to run it was by hand, in a
    shell, on a schedule somebody had to remember. In a project whose argument is
    that Airflow is the engine rather than the wrapper, that was the one chore
    left outside it.

    It also carries the replay evidence now, which is the heaviest thing in the
    schema and was the last to get retention: a superseded run's snapshot is
    dropped and a current run's is trimmed to its hashes, so coverage can still
    tell whether it has gone stale. A **pending** run is never touched at any
    age - it is an unresolved question, and deleting the question is not an
    answer. ``python -m ptm.provenance <domain> --resolve`` is.
    """
    @dag(
        dag_id="ptm_retention",
        # Weekly, because the thing it drops is ninety days old by default: a
        # daily run would spend six days a week proving there is nothing to do.
        schedule="@weekly",
        start_date=START,
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULTS,
        params={
            "days": Param(90, type="integer", minimum=0,
                          title="How old a row must be before it is dropped",
                          description="Dated by last use for a cache entry, and by its "
                                      "run for a stability sample."),
            "domain": Param("", type=["string", "null"],
                            title="Limit to one domain (blank = every domain)"),
            "keep_unhit": Param(False, type="boolean",
                                title="Keep cache entries nothing has ever served",
                                description="The right setting straight after a big "
                                            "replay nothing has re-run yet."),
            "dry_run": Param(False, type="boolean",
                             title="Count what would go without removing it"),
            "vacuum": Param(True, type="boolean",
                            title="Rewrite the database afterwards so the file shrinks",
                            description="SQLite keeps freed pages on a free list, so "
                                        "without this a prune that worked changes no "
                                        "file size at all."),
        },
        tags=["policy-time-machine", "maintenance"],
        doc_md=__doc__.split("propose_<domain>")[0] + (
            "\n\n`ptm_retention` drops the cache and sample rows that have stopped "
            "earning their disk. It never touches precedents, their history, the "
            "drafts table, or the aggregates the dashboard reads."),
    )
    def retention():
        @task
        def plan(**ctx) -> dict:
            """Count what is about to go, before anything goes.

            Run unconditionally rather than only under ``dry_run``: the count is
            what makes the next task's result checkable, and
            :func:`ptm.store.prune_preview` counts with the very same WHERE
            clauses :func:`ptm.store.prune` deletes by - one definition, because
            a dry run that counts different rows from the one that deletes them
            is worse than no dry run at all.
            """
            store.init_db()
            days = int(ctx["params"].get("days") or 0)
            domain = (ctx["params"].get("domain") or "").strip() or None
            keep_unhit = bool(ctx["params"].get("keep_unhit"))
            if domain and domain not in available_domains():
                raise AirflowFailException(
                    f"unknown domain {domain!r}; have {available_domains()}. Leaving it "
                    f"blank sweeps every domain.")
            found = store.prune_preview(domain, days, keep_unhit)
            print(prune.describe(found, domain, days, dry_run=True))
            return {"preview": found, "days": days, "domain": domain or "",
                    "keep_unhit": keep_unhit}

        @task
        def sweep_up(planned: dict, **ctx) -> dict:
            """Actually remove them, unless this run was only ever a count."""
            if ctx["params"].get("dry_run"):
                print("dry_run=true: nothing was removed. The counts above are what a "
                      "real run would drop.")
                return {"removed": {}, "dry_run": True}
            domain = planned["domain"] or None
            removed = store.prune(domain, planned["days"], planned["keep_unhit"])
            print(prune.describe(removed, domain, planned["days"], dry_run=False,
                                 vacuum=bool(ctx["params"].get("vacuum"))))
            return {"removed": removed, "dry_run": False}

        @task
        def compact(swept: dict, **ctx) -> dict:
            """Give the freed pages back to the filesystem.

            Separate from the delete on purpose. VACUUM cannot run inside a
            transaction and rewrites the entire file, so it is the one step here
            with a cost proportional to the database rather than to what was
            dropped - worth being able to see, retry and switch off on its own.
            """
            if not ctx["params"].get("vacuum"):
                print("vacuum=false: rows are gone, the file keeps its size. SQLite "
                      "reuses the freed pages for the next write.")
                return {"vacuumed": False}
            if swept.get("dry_run"):
                print("dry_run=true: rewriting the database is a change, so it is "
                      "skipped along with everything else this run would have done.")
                return {"vacuumed": False}
            result = store.vacuum()
            print(prune.describe_vacuum(result))
            return {"vacuumed": True, **result}

        planned = plan()
        compact(sweep_up(planned))

    retention()
