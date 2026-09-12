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

from . import config, diff, store, sweep as sweep_engine
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
    dials = sweep_engine.thresholds(domain, version)
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
        "impact_unit": domain.impact_unit,
    }


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
    """The text of one clause, joined across its continuation lines."""
    text = domain.policy_text(version)
    match = re.search(rf"^\s*{re.escape(clause)}\s+(.*?)(?=^\s*\d+\.\d+\s|^#|\Z)",
                      text, re.MULTILINE | re.DOTALL)
    return " ".join(match.group(1).split()) if match else ""


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
            text = pattern.sub(lambda m: f"{m.group(1)}{proposed}\n\n", text, count=1)
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
                lines[i] = f"{line} — {draft_version} (DRAFT)"
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
    """Delete a draft's files. Drafts are cheap to make and must be cheap to drop."""
    folder = config.INCLUDE_DIR / DRAFTS_DIR / domain_name
    removed = []
    for path in (folder / f"{draft_version}.md", folder / f"{draft_version}.rules.yaml"):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    load_domain.cache_clear()
    return removed


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
                        "against anything - adjudicate some flips first"}

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


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.proposal [domain] [version] [--write]``.

    Prints the offline draft. Without ``--write`` nothing touches the disk:
    proposing and adopting are separate acts, and a command that quietly grew
    the policy set every time somebody ran it to look would be the wrong default
    for the one directory in this project a person is accountable for.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    write = "--write" in args
    args = [a for a in args if not a.startswith("--")]
    domain_name = args[0] if args else "expenses"
    version = args[1] if len(args) > 1 else "v2"

    store.init_db()
    try:
        domain = load_domain(domain_name)
    except FileNotFoundError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
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
                     evidence={k: v for k, v in found.items() if k != "curves"},
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
