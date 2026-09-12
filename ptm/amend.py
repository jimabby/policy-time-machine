"""Turn a drafted amendment into a policy the engine can actually judge.

The gate tells you a policy reverses a human ruling, and the drafter proposes
an edit. Until that edit is *tested* it is an opinion. This module makes it a
candidate version, so the same machinery that condemned the original can be
pointed at the fix:

    materialise  - amendment (prose) -> a registered policy version
    verify       - does the candidate clear the gate, and what did narrowing
                   it cost elsewhere?

The honest caveat lives in :func:`offline_carve_out`. Drafting policy prose
needs a model; offline, the carve-out is applied literally, case by case.
"""

from __future__ import annotations

from typing import Any

from . import diff, store
from .config import DomainConfig
from .models import Verdict

#: Clause number given to a carve-out, so coverage reports it like any other.
CARVE_OUT_CLAUSE = "A.1"


def candidate_name(version: str) -> str:
    """``v2`` -> ``v2+fix``. Stable, so a re-run replaces rather than multiplies."""
    return f"{version}+fix" if not version.endswith("+fix") else version


def amended_text(domain: DomainConfig, version: str, amendment: dict) -> str:
    """The parent policy with the proposed edits appended as an amendment block.

    Appending rather than splicing is deliberate: a policy is a legal document
    and a diff you can read beside the original is worth more than a rewrite
    that silently changes wording elsewhere.
    """
    parent = domain.policy_text(version)
    lines = [
        parent.rstrip(),
        "",
        "---",
        "",
        f"## Amendment to {version}",
        "",
        f"Drafted because {version} was found to reverse rulings that accountable humans",
        "had already made on specific cases. The clauses below take precedence over the",
        "text above where they conflict.",
        "",
        f"*Rationale:* {amendment.get('rationale', '').strip()}",
        "",
    ]
    for i, edit in enumerate(amendment.get("edits", []), start=1):
        clause = edit.get("clause") or "new"
        lines.append(f"### A.{i} (amends clause {clause})")
        lines.append("")
        if edit.get("current_text"):
            lines.append(f"> Previously: {edit['current_text'].strip()}")
            lines.append("")
        lines.append(edit.get("proposed_text", "").strip())
        lines.append("")
        if edit.get("reason"):
            lines.append(f"*Reason:* {edit['reason'].strip()}")
            lines.append("")
    if amendment.get("residual_risk"):
        lines += [f"*Residual risk:* {amendment['residual_risk'].strip()}", ""]
    return "\n".join(lines)


def offline_carve_out(violations: list[dict], parent_rules: list[dict]) -> list[dict]:
    """Rules that make the offline judge honour the precedents, case by case.

    This is not a policy. A real amendment narrows a *clause*; this pins the
    specific cases the gate objected to and leaves everything else alone, which
    is the most a deterministic stand-in can honestly do with drafted prose.
    Its one virtue is that it is a true lower bound: if even a literal carve-out
    cannot clear the gate, the precedents contradict each other.
    """
    carve = [
        {
            "when": f"case_id == {v['case_id']!r}",
            "outcome": v["established_outcome"],
            "clause": CARVE_OUT_CLAUSE,
            "confidence": 0.99,
            "because": (f"Carved out by amendment: {v.get('ruled_by', 'a reviewer')} ruled "
                        f"{v['established_outcome']!r} on this case."),
        }
        for v in violations
    ]
    # First match wins, so the carve-out must precede the rules it overrides.
    return carve + list(parent_rules)


def materialise(domain: DomainConfig, version: str, amendment: dict,
                violations: list[dict], run_id: str = "") -> str:
    """Register the amendment as a candidate version and return its name."""
    name = candidate_name(version)
    store.save_policy_version(
        domain=domain.name,
        version=name,
        parent=version,
        text=amended_text(domain, version, amendment),
        offline_rules=offline_carve_out(violations, domain.rules_for(version)),
        rationale=amendment.get("rationale", ""),
        forced_by=[v["case_id"] for v in violations],
        run_id=run_id,
    )
    return name


def verify(domain: DomainConfig, candidate: str, parent: str,
           precedent_verdicts: dict[str, Verdict],
           collateral_verdicts: dict[str, Verdict]) -> dict:
    """Did the fix work, and what did it cost?

    ``collateral_verdicts`` are the candidate's verdicts on the cases the
    *parent* already flipped. A narrowing amendment should move few of them; a
    lot of movement means the fix was not narrow at all.
    """
    precedents = store.load_precedents(domain.name)
    violations = diff.precedent_violations(precedent_verdicts, precedents)

    parent_outcomes = {
        r["case_id"]: r["new_outcome"] for r in store.flips_for_policy(domain.name, parent)
    }
    # The cases the amendment was written to fix are supposed to move. Counting
    # them as collateral would report every working fix as having side effects.
    registered = store.load_policy_version(domain.name, candidate) or {}
    intended = set(registered.get("forced_by") or [])

    changed = sorted(
        cid for cid, v in collateral_verdicts.items()
        if cid not in intended and cid in parent_outcomes and parent_outcomes[cid] != v.outcome
    )
    untouched = [cid for cid in collateral_verdicts if cid not in intended]
    return {
        "candidate": candidate,
        "parent": parent,
        "clears_gate": not violations,
        "precedents_checked": len(precedent_verdicts),
        "violations": violations,
        "parent_flips": len(parent_outcomes),
        "intended_changes": sorted(intended),
        "collateral_checked": len(untouched),
        "collateral_changed": len(changed),
        "collateral_cases": changed[:50],
        "summary": _verdict_line(candidate, parent, violations, len(changed),
                                 len(untouched), len(intended)),
    }


def _verdict_line(candidate: str, parent: str, violations: list[dict],
                  changed: int, checked: int, intended: int) -> str:
    if violations:
        return (f"{candidate} still reverses {len(violations)} ruling(s); the amendment does not "
                f"resolve the conflict and {parent} cannot ship on this basis.")
    if not changed:
        return (f"{candidate} clears every precedent, and of the {checked} other decisions "
                f"{parent} had settled it moves none - the narrowing is surgical.")
    share = changed / checked if checked else 0
    return (f"{candidate} clears every precedent, but beyond the {intended} case(s) it was written "
            f"for it also moves {changed} of {checked} decisions {parent} had settled ({share:.0%}) "
            f"- read those before adopting it.")


def payload_for_prompt(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a flips row back into something the judge can be handed."""
    import json
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
