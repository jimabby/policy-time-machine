"""Policy Time Machine - five DAGs per domain, generated from include/domains/*.yaml.

Drop a new YAML in and Airflow grows a new set of DAGs on the next parse. The
DAG code below contains no domain knowledge at all.

    replay_<domain>          @monthly, backfilled across history.
                             Reads the policy before spending anything on it,
                             then replays every real decision in its data
                             interval under it, point-in-time correct.
                             Attributes every change to the clause that caused
                             it, breaks the blast radius down by segment, says
                             which segments carry more of it than the rest of
                             their field, and records what the judging cost -
                             net of everything served from cache. Emits the
                             flips asset.

    adjudicate_<domain>      Triggered by the flips asset. Puts the handful of
                             genuinely contested flips in front of a human via
                             HITL, and turns their rulings into precedent.
                             Emits the precedents asset.

    precedent_gate_<domain>  Triggered by the precedents asset. Re-judges every
                             established precedent under the candidate policy
                             and FAILS if the policy would reverse one. This is
                             the regression suite for organisational judgment.
                             Also warns when two human rulings contradict each
                             other, which would make that suite unsatisfiable.

    judge_stability_<domain> Manual. Judges the same cases repeatedly under the
                             same policy to measure how often the judge
                             contradicts itself - the error bar on every flip
                             rate the other DAGs report. Deliberately the one
                             DAG that never reads the verdict cache: serving a
                             repeat judgement from cache would report a judge
                             that never contradicts itself, which is not a
                             clean bill of health but a broken instrument.

    propose_<domain>         Manual. Reads everything the pipeline measured and
                             drafts the next version of the policy, then puts
                             that draft through the regression suite that
                             guards every other version. The only DAG here
                             where a model writes rather than judges, and the
                             gate at the end of it is why that is allowed.

Each DAG is built by its own module in :mod:`ptm_dags`; this file is the index
and the registration loop, so what a reader meets first is the shape of the
pipeline rather than two thousand lines of it.
"""

from __future__ import annotations

from ptm.config import available_domains
from ptm_dags import (
    adjudicate,
    judge_stability,
    precedent_gate,
    propose,
    replay,
    retention,
)
from ptm_dags.common import context


def build(domain_name: str) -> None:
    """The five DAGs for one domain, in the order they trigger each other.

    The order is not load-bearing - Airflow parses the whole file before
    anything runs - but it is the order the pipeline actually moves in, and a
    reader looking for where a DAG comes from should find them listed the way
    the docstring above describes them.
    """
    ctx = context(domain_name)
    replay.build(ctx)
    adjudicate.build(ctx)
    precedent_gate.build(ctx)
    judge_stability.build(ctx)
    propose.build(ctx)


for _name in available_domains():
    build(_name)

# Once, not per domain: the tables it prunes are shared and the file it rewrites
# is one file. See ptm_dags.retention.
retention.build()
