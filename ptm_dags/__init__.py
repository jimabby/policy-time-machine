"""The DAG layer, one module per DAG.

``dags/policy_time_machine.py`` is the file Airflow parses; it does nothing but
name the domains and call the builders in here. Each module owns exactly one
DAG and answers one question about it, which is what makes them readable on
their own:

    replay.py           what would this rule change actually do?
    adjudicate.py       which of those changes does a person have to rule on?
    precedent_gate.py   does this policy overturn a ruling somebody made?
    judge_stability.py  how much of what we just measured is the judge's noise?
    propose.py          what should the next version of the policy say?
    retention.py        what in the database has stopped earning its disk?

    common.py           what all of them need: the cost ledger, the cache
                        merge, the case payload, the review queue's rails.

Everything domain-specific arrives through :class:`common.DomainDags`, built
once per domain. Nothing in this package knows what a domain *is* - that is
still only in include/domains/*.yaml, and tests/test_dags.py asserts it across
every file here rather than only the entry point.

**Why this lives beside the engine rather than under dags/.** Airflow puts the
*configured* dags folder on sys.path, which is not necessarily the directory a
given DagBag was pointed at - the DAG-parse tests point one straight at this
repo's ``dags/`` while AIRFLOW_HOME is somewhere else entirely, and an import
error there takes all eleven DAGs with it. ``/opt/airflow`` is already on
PYTHONPATH because that is how ``ptm`` itself is found, so importing these the
same way needs no path manipulation in a file Airflow execs by hand. It also
keeps the dags folder to what Airflow actually has to parse.
"""
