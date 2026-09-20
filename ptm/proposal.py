"""Draft the next version of the policy, then make the pipeline check it.

Everything upstream of here narrows the question and then stops one step short
of answering it. Attribution says *clause 1.1 accounts for 48 of the changes*.
The sweep says *at 100 you get 161 flips instead of 147*. The precedent gate
says *this proposal reverses a ruling finance.lead made in March*. What nobody
has done at that point is the actual work: open the markdown and write the
sentence differently.

This writes it. A model is given the current policy and everything measured
about it, and returns a :class:`~ptm.models.PolicyPatch` - clause-level edits,
each tied to the evidence that motivated it. The draft is written to
``include/drafts/<domain>/`` where :func:`ptm.config.merge_drafts` picks it up
as an ordinary policy version, so from the next DAG parse it can be replayed,
gated, swept and compared exactly like a version a person wrote.

**Why letting a model write here is not the usual bad idea.** It is not asked to
judge anything, and nothing it produces is believed. The output is a proposal
against an oracle that already exists and that it does not get to influence: the
precedent gate re-judges every human ruling under the draft and fails on a
reversal. A patch that argues well and breaks precedent fails exactly as loudly
as one that argues badly. That is the whole safety story, and it is why this
module ends in :func:`verify` rather than in a congratulatory summary.

**Offline there is still a proposer.** :func:`offline_patch` is deterministic:
it searches the dials the sweep exposes for the setting that reverses the fewest
human rulings, ties broken by the smallest change to the decision base, and
writes the resulting clause edit. It is far dumber than a model and it is
optimising exactly the thing the gate measures, so it is a fair floor - if the
model cannot beat it, that is worth knowing before anyone pays for the model.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime

import yaml

from . import cli, config, diff, store
from . import sweep as sweep_engine
from .config import DRAFTS_DIR, DomainConfig, load_domain
from .judge import offline_verdict
from .models import ClauseEdit, PolicyPatch, Verdict

#: Multiples of the current setting the offline proposer tries. Deliberately
#: modest: an offline proposer that suggests moving a threshold by 10x has found
#: a number that makes the gate pass, not a policy anybody would sign.
LADDER = (0.5, 0.75, 1.25, 1.5, 2.0)

DRAFT_SYSTEM_PROMPT = (
    "You draft amendments to written policy. You write in the register of the "
    "document you are given - same voice, same structure, same level of detail - "
    "and you change as little as possible to address the evidence you are shown. "
    "You are not reforming the policy or improving its style. Every edit you "
    "propose must be traceable to a specific measurement in the evidence, and "
    "where the evidence does not support an edit you say so instead of inventing "
    "a justification. You state plainly what your amendment could break."
)

PROMPT = """Draft an amendment to this {label} policy.

# The policy as it stands (version {version})
{policy}

# What replaying {cases} real historical decisions under it found
{measured}

# Which clause is responsible for what
{attribution}

# Human rulings this policy reverses
{violations}

# What moving the numeric thresholds would do
{curves}

# What moving the two most responsible thresholds together would do
{grid}

# Your task
Propose the smallest set of clause edits that addresses the evidence above.

Rules:
- Every edit must cite the measurement that motivates it. An edit the evidence
  above does not support is worse than no edit, because the next run will
  attribute its effects to this proposal.
- A reversed human ruling is the strongest evidence here. Those are cases a
  named person looked at and settled, and a policy that reverses one fails a
  regression suite whatever else it achieves.
- Do not propose an edit to a clause the attribution shows is responsible for
  nothing. It changes decisions you have no measurement for.
- Leave the clause numbering alone. Attribution reports against these numbers
  and renumbering silently invalidates every comparison with the current run.
- Say what your amendment risks. The evidence covers the cases on file and
  nothing else."""


# ------------------------------------------------------------------- evidence

def evidence(domain: DomainConfig, version: str, max_dials: int = 3) -> dict:
    """Everything measured about a policy, gathered for the drafter.

    Read from what the pipeline has already stored, so assembling it costs
    nothing and - more importantly - the drafter sees exactly the numbers the
    dashboard shows. A proposer arguing from figures nobody else can see is a
    proposer nobody can check.
    """
    name = domain.name
    clauses = store.clause_breakdown(name, version)
    precedents = store.load_precedents(name)
    stored = store.latest_verdicts(name, version)
    verdicts = {
        case_id: Verdict(outcome=row["outcome"], rationale=row["rationale"],
                         confidence=row["confidence"], policy_clause=row["policy_clause"] or "")
        for case_id, row in stored.items()
    }
    violations = diff.precedent_violations(verdicts, precedents)

    run = store.query(
        """SELECT COALESCE(SUM(cases_replayed),0) AS cases, COALESCE(SUM(flips),0) AS flips
           FROM runs WHERE domain=? AND policy_version=?""", (name, version))[0]

    # The dials that matter, not every dial: sweeping all of them is slow and
    # buries the one clause the attribution says is doing the work.
    responsible = [row["clause"] for row in clauses if row["policy_driven"]]
    # A dial the sweep refuses is a dial the proposer must not search either:
    # ptm.sweep.variant would rewrite both ends of a band onto one number, and
    # the proposer scores the result as though it were a policy somebody could
    # adopt. See :func:`ptm.sweep.collapsing`.
    dials = [d for d in sweep_engine.thresholds(domain, version) if not d["collapses"]]
    ranked = sorted(
        dials,
        key=lambda d: responsible.index(f"clause {d['clause']}")
        if f"clause {d['clause']}" in responsible else len(responsible))

    curves = []
    for dial in ranked[:max_dials]:
        values = _ladder(dial["value"])
        try:
            curve = sweep_engine.sweep(name, version, dial["field"], values,
                                       clause=dial["clause"])
        except LookupError:
            continue
        curves.append({"clause": dial["clause"], "field": dial["field"],
                       "current": dial["value"], "points": curve["points"]})

    # The two dials the attribution blames most, moved together. Every curve
    # above holds the other dials still, which is the assumption a drafter is
    # most likely to inherit without noticing: it reads "at 100 you get 161
    # flips" as a property of that clause when it is a property of that clause
    # *given where the others sit*. The grid is what makes the interaction
    # visible, and it costs nothing but rule evaluation.
    grid = _grid(name, version, ranked)

    return {
        "domain": name,
        "version": version,
        "cases": run["cases"],
        "flips": run["flips"],
        "clauses": clauses,
        "violations": violations,
        "precedents": len(precedents),
        "dials": ranked,
        "curves": curves,
        "grid": grid,
        "impact_unit": domain.impact_unit,
    }


def _grid(name: str, version: str, ranked: list[dict]) -> dict:
    """The top two *distinct* dials, swept together. ``{}`` when there is no pair.

    Distinct matters twice over. The same field in the same clause is one dial
    listed twice, and a grid over it would put the only real measurements on the
    diagonal. The same field in two different clauses is a legitimate pair but a
    confusing one to read - both axes carry the same name - so a dial on a
    *different field* is preferred where the policy has one, and the same-field
    pair is the fallback rather than the first choice.
    """
    if len(ranked) < 2:
        return {}
    first = ranked[0]
    rest = [d for d in ranked[1:]
            if (d["field"], d["clause"]) != (first["field"], first["clause"])]
    second = next((d for d in rest if d["field"] != first["field"]), None) or \
        next(iter(rest), None)
    if second is None:
        return {}
    try:
        return sweep_engine.joint(
            name, version,
            {"field": first["field"], "clause": first["clause"],
             "values": _axis_values(first["value"])},
            {"field": second["field"], "clause": second["clause"],
             "values": _axis_values(second["value"])})
    except LookupError:
        return {}


def _axis_values(current: float) -> list[float]:
    """A short, *ordered* axis around the current setting, including it.

    Sorted because an axis is read as a direction: a row of settings in ladder
    order reads as noise, and a reader cannot see a trend in it even when the
    numbers underneath are perfectly good.
    """
    return sorted({*_ladder(current)[:3], current})


def _ladder(current: float) -> list[float]:
    """Candidate settings around a current one, deduplicated and sane."""
    out = []
    for multiple in LADDER:
        value = current * multiple
        value = int(round(value)) if float(current).is_integer() else round(value, 2)
        if value > 0 and value != current and value not in out:
            out.append(value)
    return out


def build_prompt(domain: DomainConfig, version: str, found: dict) -> str:
    """The drafting prompt. Evidence is rendered as text, not as JSON.

    A model handed a JSON dump of six read models spends its attention parsing
    it. These are the same numbers the Diff Explorer puts on screen, laid out
    the way the Diff Explorer lays them out.
    """
    unit = domain.impact_unit
    attribution = "\n".join(
        f"  {row['clause']:<34} {row['flips']:>4} flips, net {unit} "
        f"{(row['impact_loosening'] or 0) - (row['impact_tightening'] or 0):>10,.0f}"
        + ("" if row["policy_driven"] else "   (not caused by this policy - a reviewer "
                                           "departed from the rulebook already in force)")
        for row in found["clauses"]) or "  nothing attributed yet"

    violations = "\n".join(
        f"  {v['case_id']}: {v['ruled_by']} ruled '{v['established_outcome']}' on "
        f"{v['established_at']}; this policy gives '{v['proposed_outcome']}'"
        + (f"\n      their note: {v['note']}" if v.get("note") else "")
        for v in found["violations"]) or "  none - this policy reverses no human ruling"

    curves = []
    for curve in found["curves"]:
        curves.append(f"  clause {curve['clause']}, {curve['field']} "
                      f"(currently {curve['current']}):")
        for point in curve["points"]:
            curves.append(
                f"      {point['value']:>10}  {point['flips']:>4} flips "
                f"({point['flip_rate']:>6.1%}), {point['policy_driven_flips']:>4} caused by "
                f"this policy, net {unit} {point['net_impact']:>10,.0f}")
    curve_text = "\n".join(curves) or "  no numeric thresholds in this policy to sweep"

    # The pair, moved together. Rendered as a grid because that is the shape of
    # the thing: a drafter reading two curves has no way to see that the best
    # setting for one depends on where the other sits.
    grid = found.get("grid") or {}
    if grid.get("points"):
        rows = [f"  {grid['first']['field']} (rows, currently {grid['first']['current']}) "
                f"against {grid['second']['field']} (columns, currently "
                f"{grid['second']['current']}), as decisions changed:"]
        seconds = list(dict.fromkeys(pt["second_value"] for pt in grid["points"]))
        by_pair = {(pt["first_value"], pt["second_value"]): pt for pt in grid["points"]}
        rows.append("      " + "".join(f"{v:>10}" for v in ["", *seconds]))
        for a in dict.fromkeys(pt["first_value"] for pt in grid["points"]):
            cells = "".join(
                f"{by_pair[(a, b)]['flips'] if (a, b) in by_pair else '-':>10}"
                for b in seconds)
            rows.append(f"      {a:>10}{cells}")
        interaction = grid.get("interaction") or {}
        if interaction.get("measured"):
            rows.append(
                f"    moving {grid['first']['field']} changes "
                f"{interaction['effect_min_flips']}-{interaction['effect_max_flips']} "
                f"decisions depending on {grid['second']['field']}"
                + ("; the two are independent" if interaction["independent"]
                   else "; they interact, so these two clauses cannot be reasoned "
                        "about one at a time"))
        grid_text = "\n".join(rows)
    else:
        grid_text = "  no pair of thresholds to move together"

    return PROMPT.format(
        label=domain.label,
        version=version,
        policy=domain.policy_text(version),
        cases=found["cases"],
        measured=f"  {found['flips']} of {found['cases']} decisions change under this policy.\n"
                 f"  {found['precedents']} human ruling(s) on file; it reverses "
                 f"{len(found['violations'])} of them.",
        attribution=attribution,
        violations=violations,
        curves=curve_text,
        grid=grid_text,
    )


# ------------------------------------------------------- the offline proposer

def offline_patch(domain: DomainConfig, version: str, found: dict) -> PolicyPatch:
    """A deterministic amendment: the dial setting that reverses fewest rulings.

    Searches every numeric dial the offline rules expose, scoring each candidate
    setting on what the pipeline actually gates: how many human rulings the
    policy would reverse at that setting, then - only as a tie-break - how far it
    moves the decision base away from where it is today. Optimising the gate
    directly is exactly what a proposer should not be trusted to do on its own,
    which is why the result is still put through :func:`verify` like any other.

    Returns a patch with no edits when it cannot express a change honestly: no
    dials, no precedents to satisfy, or a clause whose text does not contain the
    number the rules compare against. An empty patch is a real answer.
    """
    precedents = store.load_precedents(domain.name)
    cases = store.load_cases(domain.name, until=datetime.now())
    if not cases or not found["dials"]:
        return PolicyPatch(
            summary="no amendment proposed",
            expected_effect="",
            risks="Nothing to propose: this policy exposes no numeric threshold the "
                  "offline rules can move, or there are no cases on file to measure "
                  "against. A model-backed draft is not restricted this way.")

    baseline = {c.case_id: offline_verdict(c, domain, domain.in_force) for c in cases}
    scored: list[tuple[tuple[int, int], dict]] = []
    for dial in found["dials"]:
        for value in _ladder(dial["value"]):
            patched, hits = sweep_engine.variant(domain, version, dial["field"], value,
                                                 clause=dial["clause"])
            if not hits:
                continue
            verdicts = {c.case_id: offline_verdict(c, patched, version) for c in cases}
            reversed_rulings = len(diff.precedent_violations(verdicts, precedents))
            moved = diff.summarise(
                diff.flips(cases, verdicts, patched, baseline=baseline),
                len(cases), patched)["policy_driven_flips"]
            scored.append(((reversed_rulings, moved),
                           {"dial": dial, "value": value, "reversals": reversed_rulings,
                            "moved": moved, "rules": patched.offline_rules[version]}))

    if not scored:
        return PolicyPatch(summary="no amendment proposed",
                           risks="No dial in this policy could be moved mechanically.")

    scored.sort(key=lambda row: row[0])
    (reversals, moved), best = scored[0]
    current_reversals = len(found["violations"])
    dial = best["dial"]

    body = _clause_body(domain, version, dial["clause"])
    rewritten = _retarget_text(body, dial["value"], best["value"])
    if rewritten is None:
        return PolicyPatch(
            summary="no amendment proposed",
            risks=f"The best setting found for clause {dial['clause']} is "
                  f"{dial['field']}={best['value']}, but the clause text does not state "
                  f"{dial['value']} as a number, so the edit cannot be written without "
                  f"rewording a sentence this proposer is not able to reword. Move it by "
                  f"hand, or draft with a model.")

    return PolicyPatch(
        summary=f"Move the {dial['field']} threshold in clause {dial['clause']} from "
                f"{dial['value']} to {best['value']}.",
        edits=[ClauseEdit(
            clause=dial["clause"],
            current_text=body,
            proposed_text=rewritten,
            rationale=f"At {best['value']} the policy reverses {reversals} human ruling(s) "
                      f"against {current_reversals} today, the fewest of the "
                      f"{len(scored)} settings searched.",
            expected_effect=f"{moved} decisions change under this policy, against "
                            f"{sum(r['flips'] for r in found['clauses'] if r['policy_driven'])} "
                            f"at the current setting.",
        )],
        expected_effect=f"Precedent reversals {current_reversals} -> {reversals}; "
                        f"policy-driven changes {moved}.",
        risks="Chosen by searching one threshold against the precedent set, which is a "
              "handful of contested cases - a setting that satisfies them is not thereby "
              "a good rule. Nothing here reads prose: only the number is moved, so a "
              "parenthetical explaining the old value, or a neighbouring clause that "
              "assumes it, is left stale for a person to fix.",
    )


def _clause_body(domain: DomainConfig, version: str, clause: str) -> str:
    """The text of one clause. One reader of a policy, in :mod:`ptm.config`."""
    return domain.clause_text(version, clause)


def _retarget_text(body: str, current: float, value: float) -> str | None:
    """Rewrite the stated number in a clause, or refuse.

    Refusing matters. A clause that says "the threshold in the schedule" has no
    number to move, and a proposer that silently appends a sentence instead has
    produced a policy nobody asked for.
    """
    if not body:
        return None
    current_text = str(int(current)) if float(current).is_integer() else str(current)
    value_text = str(int(value)) if float(value).is_integer() else str(value)
    pattern = re.compile(rf"(?<![\d.]){re.escape(current_text)}(?![\d.])")
    if not pattern.search(body):
        return None
    return pattern.sub(value_text, body, count=1)


# --------------------------------------------------------------- the markdown

def apply_to_markdown(domain: DomainConfig, version: str, patch: PolicyPatch,
                      draft_version: str) -> str:
    """The policy with the patch applied, ready to be judged.

    Edits are matched by clause number and replace that clause's text in place.
    An edit naming a clause the policy does not have is appended under its own
    section rather than dropped: a drafter proposing a new exemption is the
    normal case, and silently discarding it would make the draft disagree with
    the patch that documents it.
    """
    text = domain.policy_text(version)
    header = (f"<!-- Drafted by ptm.proposal from {domain.name}/{version} on "
              f"{datetime.now():%Y-%m-%d}. Not approved by anyone. -->\n"
              f"<!-- {patch.summary} -->\n")
    for edit in patch.edits:
        clause = edit.clause.strip()
        proposed = " ".join(edit.proposed_text.split())
        if not clause or not proposed:
            continue
        pattern = re.compile(rf"(^\s*{re.escape(clause)}\s+)(.*?)(?=^\s*\d+\.\d+\s|^#|\Z)",
                             re.MULTILINE | re.DOTALL)
        if pattern.search(text):
            # `proposed` bound as a default rather than closed over: sub() calls
            # this inside the same iteration so the behaviour was already right,
            # and a lambda reading a loop variable is one refactor away from
            # applying the last edit to every clause.
            text = pattern.sub(
                lambda m, body=proposed: f"{m.group(1)}{body}\n\n", text, count=1)
        else:
            text = text.rstrip() + f"\n\n## {clause.split('.')[0]}. Added by this draft\n" \
                                   f"{clause} {proposed}\n"
    return header + _retitle(text, version, draft_version)


def _retitle(text: str, version: str, draft_version: str) -> str:
    """Put the draft's own version in the title, so a reader cannot mistake it."""
    lines = text.splitlines()
    for i, line in enumerate(lines[:3]):
        if line.startswith("# "):
            lines[i] = re.sub(rf"\bv?{re.escape(version.lstrip('v'))}\b",
                              draft_version, line, count=1)
            if draft_version not in lines[i]:
                lines[i] = f"{line} - {draft_version} (DRAFT)"
            break
    return "\n".join(lines) + "\n"


def reload_domain(name: str):
    """Re-read a domain from disk, drafts included.

    :func:`ptm.config.load_domain` is cached per process, and every Airflow
    worker populates that cache while *parsing* the DAG file - which happens
    before any draft exists. A task asking for the domain without clearing it
    would be handed a config with no such version and fail on the draft it had
    just written itself.
    """
    load_domain.cache_clear()
    return load_domain(name)


def next_version(domain: DomainConfig, base: str) -> str:
    """``v2`` -> ``v2-draft1``, then ``v2-draft2``. Never overwrites a draft.

    Sequential rather than timestamped so the version reads as what it is - the
    third attempt at amending v2 - and so two drafts made in the same minute do
    not collide.

    The filesystem is consulted as well as the config, because the config in
    *this* process may predate a draft written by another one - and a name
    collision here does not fail, it silently overwrites somebody's draft.
    """
    folder = config.INCLUDE_DIR / DRAFTS_DIR / domain.name
    on_disk = {path.stem for path in folder.glob("*.md")} if folder.is_dir() else set()
    taken = set(domain.policies) | on_disk
    n = 1
    while f"{base}-draft{n}" in taken:
        n += 1
    return f"{base}-draft{n}"


def materialise(domain_name: str, draft_version: str, markdown: str,
                rules: list[dict] | None = None) -> dict:
    """Write a draft where :func:`ptm.config.merge_drafts` will find it.

    Two files: the policy, and optionally the offline rules that let the
    deterministic judge evaluate it. Without the rules an offline replay of the
    draft would return the most generous outcome for every case and look like a
    wildly permissive policy, which is the single most misleading thing this
    module could produce - so a caller that has rules should always pass them.
    """
    folder = config.INCLUDE_DIR / DRAFTS_DIR / domain_name
    folder.mkdir(parents=True, exist_ok=True)
    policy_path = folder / f"{draft_version}.md"
    policy_path.write_text(markdown, encoding="utf-8")
    written = [str(policy_path)]
    if rules is not None:
        rules_path = folder / f"{draft_version}.rules.yaml"
        rules_path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")
        written.append(str(rules_path))
    # The domain is cached per process and every DAG, endpoint and CLI resolves
    # versions through it. Without this the draft exists on disk and nothing in
    # the running process can see it.
    load_domain.cache_clear()
    return {"version": draft_version, "files": written,
            "offline_rules": len(rules or [])}


def discard(domain_name: str, draft_version: str) -> list[str]:
    """Delete a draft's files. Drafts are cheap to make and must be cheap to drop.

    The ``policy_drafts`` row is deliberately left behind. It is the provenance -
    what was proposed, from what evidence, and what checking it found - and a
    draft somebody looked at and rejected is a more useful record than no record
    at all. :func:`ptm.report.drafts` reports such a row as unavailable.

    The version is checked the way :func:`adopt` checks it, and for a sharper
    reason. ``adopt`` refused anything that was not a draft so that nobody could
    promote a policy over a policy; this built a path out of the argument and
    unlinked whatever was there, so ``--discard ../../policies/expenses/v1``
    deleted a policy somebody is accountable for. Drafts are cheap to drop and
    that is exactly why this command must only ever drop one.
    """
    if not _is_draft_name(draft_version):
        raise LookupError(
            f"{draft_version!r} is not a draft version name. A draft is one path "
            f"segment - no slashes, no '..' - because this deletes files, and the "
            f"only files it may delete are in include/{DRAFTS_DIR}/{domain_name}/.")
    folder = config.INCLUDE_DIR / DRAFTS_DIR / domain_name
    removed = []
    for path in (folder / f"{draft_version}.md", folder / f"{draft_version}.rules.yaml"):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    load_domain.cache_clear()
    return removed


def _is_draft_name(version: str) -> bool:
    """Whether a string can name a draft file rather than reach out of its folder."""
    version = (version or "").strip()
    return bool(version) and version not in {".", ".."} and not (
        set(version) & set("/\\") or version.startswith("."))


def drafts_on_disk(domain_name: str) -> list[dict]:
    """Every draft this domain has, from the files and from the provenance table.

    Joined rather than read from either alone, because the two go out of step in
    both directions and each direction means something different: files with no
    row were written by a hand that bypassed the DAG, and a row with no files is
    a draft somebody discarded - which is a decision, not an absence.
    """
    folder = config.INCLUDE_DIR / DRAFTS_DIR / domain_name
    on_disk = {path.stem for path in folder.glob("*.md")} if folder.is_dir() else set()
    rows = {row["version"]: row for row in store.drafts(domain_name)}
    out = []
    for version in sorted(on_disk | set(rows)):
        row = rows.get(version, {})
        verification = row.get("verification") or {}
        out.append({
            "version": version,
            "available": version in on_disk,
            "recorded": version in rows,
            "base_version": row.get("base_version", ""),
            "summary": row.get("summary", ""),
            "drafted_by": row.get("drafted_by", ""),
            "created_at": row.get("created_at", ""),
            "has_rules": (folder / f"{version}.rules.yaml").exists(),
            "reverses": len(verification.get("violations", [])),
            "fixed": len(verification.get("fixed", [])),
            "introduced": len(verification.get("introduced", [])),
            "checked": bool(verification.get("precedents")),
            "adopted_as": row.get("adopted_as", ""),
            "adopted_by": row.get("adopted_by", ""),
            "adopted_at": row.get("adopted_at", ""),
        })
    return out


def describe_drafts(domain_name: str, rows: list[dict]) -> str:
    """The draft list as the CLI prints it."""
    if not rows:
        return (f"no drafts for {domain_name}. Run `python -m ptm.proposal {domain_name} "
                f"<version> --write`, or trigger propose_{domain_name}.")
    lines = [f"{len(rows)} draft(s) for {domain_name}:"]
    for row in rows:
        if row["adopted_as"]:
            state = (f"   [adopted as {row['adopted_as']} by {row['adopted_by']} "
                     f"on {row['adopted_at'][:10]}]")
        elif row["available"]:
            state = ""
        else:
            state = "   [discarded - files gone, provenance kept]"
        lines.append(f"  {row['version']:<16} from {row['base_version'] or '?':<8} "
                     f"by {row['drafted_by'] or '?':<28} {row['created_at'][:10]}{state}")
        if row["summary"]:
            lines.append(f"      {row['summary']}")
        if row["checked"]:
            lines.append(f"      gate: reverses {row['reverses']} ruling(s), "
                         f"fixed {row['fixed']}, introduced {row['introduced']}")
        elif row["recorded"]:
            lines.append("      gate: not checked against any human ruling")
        if row["available"] and not row["has_rules"]:
            lines.append("      no offline rules: PTM_OFFLINE=1 would return the most "
                         "generous outcome for every case, and it cannot be swept")
    lines.append(f"\n  adopt one:   python -m ptm.proposal {domain_name} --adopt <version> "
                 f"--by <your name>")
    lines.append(f"  drop one:    python -m ptm.proposal {domain_name} --discard <version>")
    return "\n".join(lines)


# ----------------------------------------------------------------- adoption

def next_policy_version(domain: DomainConfig) -> str:
    """The next free ``vN`` for a domain - the name an adopted draft takes.

    Not the draft's own name. ``v2-draft1`` says "the first attempt at amending
    v2", which is the truth about a proposal and a lie about a policy in the
    book; and adopting it *as* ``v2`` would overwrite the text people are
    currently accountable for.
    """
    taken = {int(m.group(1)) for m in
             (re.fullmatch(r"v(\d+)", v) for v in domain.policies) if m}
    return f"v{max(taken, default=0) + 1}"


def _register(yaml_text: str, domain_name: str, version: str,
              rules: list[dict] | None) -> str:
    """Add a policy version to the domain YAML in place, keeping every comment.

    A round trip through ``yaml.safe_load`` and ``yaml.safe_dump`` would be two
    lines and would destroy the file. ``include/domains/*.yaml`` is mostly
    comments explaining decisions a person made - which rule stands in for which
    clause, why a threshold is where it is - and a command that silently deletes
    all of them is a command nobody runs a second time. So the edit is textual:
    one line appended to the ``policies`` block, and one block inserted under
    ``offline_rules``.
    """
    lines = yaml_text.splitlines()

    def block_end(header: str) -> int | None:
        """Index just past the last line of a top-level block, or None."""
        try:
            start = next(i for i, line in enumerate(lines)
                         if line.rstrip() == header or line.startswith(header + " "))
        except StopIteration:
            return None
        end = start + 1
        last = end
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line.startswith((" ", "\t")):
                break
            if line.strip():
                last = end + 1
            end += 1
        return last

    at = block_end("policies:")
    if at is None:
        raise ValueError(f"{domain_name}.yaml has no 'policies:' block to register into")
    lines.insert(at, f"  {version}: policies/{domain_name}/{version}.md")

    if rules:
        body = yaml.safe_dump(rules, sort_keys=False, allow_unicode=True,
                              default_flow_style=False)
        indented = ["    " + line if line.strip() else line
                    for line in body.rstrip("\n").splitlines()]
        at = block_end("offline_rules:")
        if at is None:
            lines += ["", "offline_rules:"]
            at = len(lines)
        lines.insert(at, "\n".join([f"  {version}:", *indented]))
    return "\n".join(lines) + "\n"


#: The line :func:`apply_to_markdown` stamps on a draft. Rewritten on adoption
#: rather than left in place: "approved by nobody" sitting at the top of a
#: policy in force is the single most misleading sentence this project could
#: ship, and deleting the provenance instead would lose where it came from.
DRAFT_STAMP = "Not approved by anyone."


def adopt(domain_name: str, draft_version: str, by: str,
          as_version: str | None = None, dry_run: bool = False) -> dict:
    """Promote a draft to a policy version somebody is accountable for.

    This is the step the whole pipeline defers to a person, so it asks for the
    person: ``by`` is recorded in the adopted document and is not optional.
    Everything else is mechanical - move the markdown into ``include/policies/``,
    register it and its offline rules in the domain YAML, and drop the draft -
    and doing it by hand is three fiddly edits in two directories where the
    common failure is registering the policy and forgetting the rules, which
    leaves an offline replay approving everything.

    It refuses to adopt anything that is not a draft, and refuses to write over
    a version that already exists. Adopting does not make the policy *in force*:
    that is ``in_force`` in the domain YAML, one more deliberate edit, because
    a version existing and a version governing are different claims.

    ``dry_run`` runs every check and reports every edit it would make, then
    writes nothing. This is the one irreversible act in the project - two files
    created and a hand-maintained YAML rewritten in place, with no undo - and it
    was the only one with no way to look first. ``ptm.prune`` has a dry run,
    ``ptm_retention`` has one, the replay has ``preflight=fail``; the step that
    puts a machine's draft into the policy book had none.
    """
    domain = reload_domain(domain_name)
    if draft_version not in domain.draft_versions:
        known = sorted(domain.draft_versions)
        raise LookupError(
            f"{draft_version!r} is not a draft of {domain_name}; drafts on file: "
            f"{known or 'none'}. Only a draft can be adopted - a version in the YAML "
            f"is already somebody's.")
    if not by.strip():
        raise ValueError("adopting a policy records who adopted it; pass --by <name>")

    version = (as_version or next_policy_version(domain)).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("policy version must be a filename: letters, numbers, '.', '_' or '-'")
    if version in domain.policies:
        raise LookupError(
            f"{domain_name} already has a policy version {version!r}. Adopting over it "
            f"would replace text somebody is accountable for; pass --as <version> with "
            f"a free name.")

    folder = config.INCLUDE_DIR / DRAFTS_DIR / domain_name
    markdown = (folder / f"{draft_version}.md").read_text(encoding="utf-8")
    adopted = (f"Adopted as {version} by {by} on {datetime.now():%Y-%m-%d}, "
               f"from draft {draft_version}.")
    if DRAFT_STAMP in markdown:
        markdown = markdown.replace(DRAFT_STAMP, adopted)
    else:
        # A draft written by hand rather than by the proposer carries no stamp
        # to replace, and drafts_on_disk deliberately lists those. The line
        # recording who took responsibility is the point of this command, so it
        # is added rather than skipped for want of something to overwrite.
        markdown = f"<!-- {adopted} -->\n{markdown}"
    markdown = _retitle(markdown, draft_version, version)

    rules_path = folder / f"{draft_version}.rules.yaml"
    rules = (yaml.safe_load(rules_path.read_text(encoding="utf-8")) or []
             if rules_path.exists() else [])

    policy_dir = config.INCLUDE_DIR / "policies" / domain_name
    policy_path = policy_dir / f"{version}.md"
    if policy_path.resolve().parent != policy_dir.resolve() or policy_path.exists():
        raise ValueError(f"refusing to overwrite or leave the policy directory: {policy_path}")
    yaml_path = config.INCLUDE_DIR / "domains" / f"{domain_name}.yaml"

    # Rulings made while a reviewer was looking at this draft. They name the
    # draft, and the draft is about to stop existing - so without the re-filing
    # below every one of them becomes permanently `version_gone` to
    # ptm.diff.stale_precedents, which tells the reader that what the reviewer
    # was shown "cannot be recovered" while this function is in the act of
    # copying it into policy_path. Counted before the write in both modes, so a
    # dry run reports the same number the real one will move.
    ruled_under_draft = [p.case_id for p in store.load_precedents(domain_name)
                         if p.policy_version == draft_version]

    planned = {"draft": draft_version, "version": version, "by": by,
               "policy": str(policy_path), "domain_yaml": str(yaml_path),
               "offline_rules": len(rules),
               "repointed_precedents": sorted(ruled_under_draft)}

    if dry_run:
        return {**planned, "dry_run": True, "removed": [],
                "would_remove": [str(folder / f"{draft_version}.md")]
                + ([str(rules_path)] if rules_path.exists() else [])}

    policy_dir.mkdir(parents=True, exist_ok=True)
    with policy_path.open("x", encoding="utf-8") as output:
        output.write(markdown)

    yaml_path.write_text(
        _register(yaml_path.read_text(encoding="utf-8"), domain_name, version, rules),
        encoding="utf-8")

    # Before the draft's files go, and before anything can read the domain
    # again: from here on `draft_version` resolves to nothing, and a ruling
    # still naming it is a ruling the gate enforces on text nobody can find.
    repointed = store.repoint_precedents(domain_name, draft_version, version)

    removed = discard(domain_name, draft_version)
    # Recorded beside the provenance rather than over it. The patch, the
    # evidence and the gate result are why this text exists, and an adopted
    # draft is the one case where they matter most - so adoption adds a field
    # and rewrites nothing. It is also what lets the draft list tell a version
    # somebody took responsibility for from one somebody threw away, which the
    # files alone cannot say: both leave include/drafts/ empty.
    store.mark_adopted(domain_name, draft_version, version, by)
    return {**planned, "dry_run": False, "removed": removed,
            "repointed_precedents": repointed}


def describe_adoption(result: dict) -> str:
    """What adoption did, or what it would do. One renderer for both.

    Written once rather than twice on purpose: a dry run whose output is
    assembled by different code from the real thing is a dry run that can be
    accurate about a command that has since changed, which is the failure
    :func:`ptm.store.prune_preview` exists to avoid on the other side of the
    project.
    """
    planning = result.get("dry_run")
    verb = "would adopt" if planning else "adopted"
    lines = [f"{verb} {result['draft']} as {result['version']}, by {result['by']}"]
    lines.append(f"  {'would write' if planning else 'wrote'}    {result['policy']}")
    lines.append(
        f"  {'would register' if planning else 'registered'} it in {result['domain_yaml']}"
        + (f" with {result['offline_rules']} offline rule(s)"
           if result["offline_rules"] else
           " with no offline rules - PTM_OFFLINE=1 will return the most generous "
           "outcome for every case under it"))
    moved = result.get("repointed_precedents") or []
    if moved:
        # Said out loud because it is the one edit here that touches the durable
        # artefact. Nothing about the rulings changes - not the outcome, not who
        # made it - only the name of the document they were made against, which
        # is the name this command is in the act of changing.
        lines.append(
            f"  {'would re-file' if planning else 're-filed'} {len(moved)} ruling(s) made "
            f"under {result['draft']} against {result['version']}: {moved[:10]}")
        lines.append("      the text they were ruled on is the text being adopted, so "
                     "without this the gate would enforce them forever while reporting "
                     "that what the reviewer saw cannot be recovered")
    for path in result.get("would_remove") or result.get("removed") or []:
        lines.append(f"  {'would remove' if planning else 'removed'}   {path}")
    if planning:
        lines.append("\n  nothing was written. Drop --dry-run to do it.")
    else:
        lines.append(f"\n{result['version']} is a policy version like any other now: "
                     f"lint it, replay it, gate it. It is not yet the policy *in force* "
                     f"- that is `in_force` in the domain YAML, and one more deliberate "
                     f"edit.")
    return "\n".join(lines)


# ------------------------------------------------------------- verification

def verify(domain_name: str, draft_version: str, base_version: str,
           verdicts: dict[str, Verdict] | None = None) -> dict:
    """Put the draft through the gate that guards every other policy version.

    Bounded on purpose: it re-judges the precedent cases, which are a handful,
    not the whole history. That answers "does this draft reverse a human ruling"
    - the question that fails a run - for the price of eight judgements rather
    than six hundred. The full picture still costs a replay, and this says so
    rather than implying the draft has been measured.

    ``verdicts`` lets a caller supply judgements made elsewhere (the DAG maps
    them across an LLM fan-out); offline they are computed here.
    """
    domain = reload_domain(domain_name)
    precedents = store.load_precedents(domain_name)
    if not precedents:
        return {"precedents": 0, "checked": 0, "violations": [], "fixed": [],
                "introduced": [], "base_violations": [],
                "hint": "no human rulings on file yet, so this draft has not been checked "
                        "against anything - adjudicate some flips first",
                "hint_key": "hint.draft_unchecked"}

    ids = [p.case_id for p in precedents]
    cases = store.load_cases(domain_name, until=datetime.now(), case_ids=ids)
    missing = sorted(set(ids) - {c.case_id for c in cases})
    if verdicts is None:
        verdicts = {c.case_id: offline_verdict(c, domain, draft_version) for c in cases}

    # The policy in force is read from what the gate already stored rather than
    # re-judged. Free, and - when the draft was judged by a model - it keeps
    # both sides of the comparison answered by the same judge. Falling back to
    # the offline evaluator for a case nothing has judged yet would mix two
    # judges in one comparison, so the result says which it used.
    stored = store.latest_verdicts(domain_name, base_version)
    base = {
        c.case_id: Verdict(outcome=stored[c.case_id]["outcome"],
                           rationale=stored[c.case_id]["rationale"],
                           confidence=stored[c.case_id]["confidence"],
                           policy_clause=stored[c.case_id]["policy_clause"] or "")
        for c in cases if c.case_id in stored
    }
    computed = [c for c in cases if c.case_id not in base]
    base.update({c.case_id: offline_verdict(c, domain, base_version) for c in computed})

    found = diff.precedent_violations(verdicts, precedents)
    before = diff.precedent_violations(base, precedents)
    broken = {v["case_id"] for v in found}
    was_broken = {v["case_id"] for v in before}
    return {
        "draft": draft_version,
        "base": base_version,
        "precedents": len(precedents),
        "checked": len(cases),
        "uncheckable": missing,
        "violations": found,
        "base_violations": before,
        # Which judge answered for the policy in force, so a mixed comparison
        # cannot be read as a like-for-like one.
        "base_from_stored_verdicts": len(base) - len(computed),
        "base_computed_offline": len(computed),
        # The two numbers that decide whether the draft was worth drafting.
        "fixed": sorted(was_broken - broken),
        "introduced": sorted(broken - was_broken),
        "caveat": "checked against the precedent set only. A draft that reverses no "
                  "ruling has passed the regression suite, not been measured - replay "
                  "it to find out what it does to the other cases.",
        "caveat_key": "caveat.draft_verify",
    }


def describe(patch: PolicyPatch, verification: dict | None = None) -> str:
    if not patch.edits:
        return f"drafted no amendment: {patch.risks or patch.summary}"
    lines = [f"drafted: {patch.summary}"]
    for edit in patch.edits:
        lines.append(f"  clause {edit.clause}")
        if edit.current_text:
            lines.append(f"    was: {edit.current_text[:160]}")
        lines.append(f"    now: {edit.proposed_text[:160]}")
        lines.append(f"    why: {edit.rationale}")
    if patch.expected_effect:
        lines.append(f"  expected: {patch.expected_effect}")
    if patch.risks:
        lines.append(f"  risks: {patch.risks}")
    if verification and verification.get("precedents"):
        lines.append(
            f"  gate: {len(verification['violations'])} reversal(s) against "
            f"{len(verification['base_violations'])} under {verification['base']} "
            f"- fixed {len(verification['fixed'])}, introduced "
            f"{len(verification['introduced'])}")
    elif verification:
        lines.append(f"  gate: {verification.get('hint', 'not checked')}")
    return "\n".join(lines)


USAGE = """usage:
  python -m ptm.proposal <domain> [version] [--write]
        Draft the next version of the policy from the evidence. Without
        --write nothing touches the disk.

  python -m ptm.proposal <domain> --list
        Every draft this domain has, with what the gate made of it.

  python -m ptm.proposal <domain> --discard <version>
        Delete a draft's files. The provenance row is kept.

  python -m ptm.proposal <domain> --adopt <version> --by <name> [--as <version>]
                                  [--dry-run]
        Promote a draft into include/policies/ and register it, with its
        offline rules, in the domain YAML. Records who adopted it, and re-files
        every ruling made under the draft's name against the adopted one.
        --dry-run runs every check and prints every edit, then writes nothing."""


def _flag(args: list[str], name: str) -> str | None:
    """The value after ``--name``, or None. Empty string if the flag ends the line."""
    if name not in args:
        return None
    index = args.index(name)
    value = args[index + 1] if index + 1 < len(args) else ""
    return "" if value.startswith("--") else value


def main(argv: list[str] | None = None) -> int:
    """The draft lifecycle, end to end. See :data:`USAGE`.

    Drafting prints and, with ``--write``, writes. Proposing and adopting are
    separate acts and separate flags: a command that quietly grew the policy set
    every time somebody ran it to look would be the wrong default for the one
    directory in this project a person is accountable for.

    The lint tells a reader to discard a draft "with ptm.proposal.discard" and
    the .gitignore tells them to adopt one by moving files and editing YAML.
    Both were true and neither was a command anybody could run, which is how a
    drafts folder fills up with amendments nobody will decide about.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0
    write = "--write" in args
    listing = "--list" in args
    dry_run = "--dry-run" in args
    discarding = _flag(args, "--discard")
    adopting = _flag(args, "--adopt")
    adopter = _flag(args, "--by") or ""
    adopt_as = _flag(args, "--as")
    if adopt_as == "":
        # Distinguished from absent, for the same reason ptm.report distinguishes
        # them: "" is the flag given with nothing after it, and falling back to
        # the next free version number would adopt under a name nobody typed.
        print(f"ERROR --as needs a version\n\n{USAGE}", file=sys.stderr)
        return 2

    flags = {"--write", "--list", "--dry-run", "--discard", "--adopt", "--by", "--as"}
    positional, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in flags:
            skip = arg not in {"--write", "--list", "--dry-run"}
            continue
        if arg.startswith("--"):
            print(f"ERROR unknown option {arg!r}\n\n{USAGE}", file=sys.stderr)
            return 2
        positional.append(arg)

    domain_name = positional[0] if positional else "expenses"
    version = positional[1] if len(positional) > 1 else "v2"

    store.init_db()
    try:
        domain = load_domain(domain_name)
    except FileNotFoundError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    if listing:
        print(describe_drafts(domain_name, drafts_on_disk(domain_name)))
        return 0

    if discarding is not None:
        if not discarding:
            print(f"ERROR --discard needs a version\n\n{USAGE}", file=sys.stderr)
            return 2
        try:
            removed = discard(domain_name, discarding)
        except LookupError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        if not removed:
            print(f"ERROR no draft {discarding!r} on disk for {domain_name}; "
                  f"`--list` shows what there is", file=sys.stderr)
            return 2
        for path in removed:
            print(f"removed {path}")
        print(f"{discarding} is no longer a policy version of {domain_name}. Its "
              f"provenance is kept - what was proposed and why is still on record.")
        return 0

    if adopting is not None:
        if not adopting:
            print(f"ERROR --adopt needs a version\n\n{USAGE}", file=sys.stderr)
            return 2
        try:
            result = adopt(domain_name, adopting, adopter, adopt_as, dry_run=dry_run)
        except (LookupError, ValueError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(describe_adoption(result))
        return 0

    if version not in domain.policies:
        print(f"ERROR unknown policy version {version!r}; have {sorted(domain.policies)}",
              file=sys.stderr)
        return 2

    found = evidence(domain, version)
    patch = offline_patch(domain, version, found)
    if not patch.edits:
        print(describe(patch))
        return 0

    draft_version = next_version(domain, version)
    rules = rules_for(domain, version, patch, found)
    markdown = apply_to_markdown(domain, version, patch, draft_version)
    if not write:
        print(describe(patch))
        print(f"\n(not written; re-run with --write to draft {draft_version})")
        return 0

    materialise(domain_name, draft_version, markdown, rules)
    verification = verify(domain_name, draft_version, domain.in_force)
    store.save_draft(domain_name, draft_version, version, patch.model_dump(),
                     evidence={k: v for k, v in found.items() if k not in ("curves", "grid")},
                     verification=verification, drafted_by="offline")
    print(describe(patch, verification))
    print(f"\nwrote {DRAFTS_DIR}/{domain_name}/{draft_version}.md - replay, sweep and gate "
          f"it like any other version")
    return 0


def rules_for(domain: DomainConfig, version: str, patch: PolicyPatch,
              found: dict) -> list[dict] | None:
    """Offline rules for the draft, derived from the edits the offline proposer made.

    Only the threshold moves can be derived mechanically, which is precisely the
    set the offline proposer is able to make. A model-drafted patch needs
    :mod:`ptm.rules` to write these instead - and the DAG does exactly that.
    """
    patched = domain
    moved = False
    for edit in patch.edits:
        dial = next((d for d in found["dials"] if d["clause"] == edit.clause), None)
        if not dial:
            continue
        numbers = re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])", edit.proposed_text)
        if not numbers:
            continue
        value = float(numbers[0])
        value = int(value) if value.is_integer() else value
        patched, hits = sweep_engine.variant(patched, version, dial["field"], value,
                                             clause=edit.clause)
        if not hits:
            continue
        moved = True
        # The rule's own rationale quotes the old threshold, and that sentence is
        # what a reviewer reads next to the flip. Leaving it stale would put a
        # number in front of a human that the rule beside it no longer uses.
        for rule in patched.offline_rules.get(version, []):
            if str(rule.get("clause", "")) != edit.clause:
                continue
            rewritten = _retarget_text(rule.get("because", ""), dial["value"], value)
            if rewritten:
                rule["because"] = rewritten
    return patched.offline_rules.get(version) if moved else None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
