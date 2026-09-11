"""What number should the threshold actually be?

Attribution answers *which clause moves the most decisions*. The question that
immediately follows is *so what should it say instead* - and a clause like
"receipts required above GBP 75" has exactly one dial on it. This sweeps that
dial and re-runs the whole replay at each setting, so the choice of threshold
stops being an argument and becomes a curve:

    python -m ptm.sweep expenses v2              # list the dials
    python -m ptm.sweep expenses v2 1.1 amount_gbp 25,50,75,100,150,250

**This reads the offline rules, not the markdown policy.** It is a fixture-based
sweep: it can only move numbers that ``offline_rules`` expresses mechanically,
and it reports what the *rule evaluator* would do, not what a language model
reading a reworded policy would do. That makes it the cheap first pass - no
model calls, no bill - for narrowing a range you then confirm with a real
replay at one or two candidate values. :mod:`ptm.lint` is what keeps the rules
and the markdown honest with each other in the meantime.
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime

from . import diff
from .config import DomainConfig, load_domain
from .judge import offline_verdict
from .models import Case
from .store import load_cases


class _Retarget(ast.NodeTransformer):
    """Rewrite every numeric literal compared against ``field`` to ``value``."""

    def __init__(self, field: str, value: float) -> None:
        self.field = field
        self.value = value
        self.hits = 0

    def visit_Compare(self, node: ast.Compare) -> ast.Compare:  # noqa: N802
        self.generic_visit(node)
        operands = [node.left, *node.comparators]
        positions = {i for i, n in enumerate(operands)
                     if isinstance(n, ast.Name) and n.id == self.field}
        if not positions:
            return node
        for i, operand in enumerate(operands):
            if i in positions or not isinstance(operand, ast.Constant):
                continue
            if isinstance(operand.value, bool) or not isinstance(operand.value, (int, float)):
                continue
            operands[i] = ast.Constant(value=self.value)
            self.hits += 1
        node.left, node.comparators = operands[0], operands[1:]
        return node


def retarget(expression: str, field: str, value: float) -> tuple[str, int]:
    """Move every number ``field`` is compared against, and say how many moved."""
    tree = ast.parse(expression, mode="eval")
    rewriter = _Retarget(field, value)
    tree = ast.fix_missing_locations(rewriter.visit(tree))
    return ast.unparse(tree), rewriter.hits


def thresholds(domain: DomainConfig, version: str) -> list[dict]:
    """Every numeric dial the offline rules for ``version`` expose.

    This is the menu a sweep is chosen from, so it reports where each dial lives
    rather than only its value: the same field can be compared in more than one
    clause, and moving "the receipt threshold" may mean moving all of them.
    """
    found: list[dict] = []
    for index, rule in enumerate(domain.offline_rules.get(version, [])):
        expression = rule.get("when") or ""
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            fields = [n.id for n in operands if isinstance(n, ast.Name)]
            numbers = [n.value for n in operands
                       if isinstance(n, ast.Constant)
                       and isinstance(n.value, (int, float))
                       and not isinstance(n.value, bool)]
            if len(fields) == 1 and numbers:
                found.append({
                    "clause": str(rule.get("clause", "")),
                    "rule_index": index,
                    "field": fields[0],
                    "value": numbers[0],
                    "outcome": rule.get("outcome", ""),
                    "expression": expression,
                })
    return found


def variant(domain: DomainConfig, version: str, field: str, value: float,
            clause: str = "") -> tuple[DomainConfig, int]:
    """A copy of the domain whose ``version`` rules use ``value`` for ``field``.

    ``clause`` narrows the rewrite to the rules citing it, which is what you
    want when a field appears in two clauses not meant to move together. Blank
    moves every occurrence in the version.
    """
    patched = domain.model_copy(deep=True)
    hits = 0
    for rule in patched.offline_rules.get(version, []):
        if clause and str(rule.get("clause", "")) != clause:
            continue
        try:
            rewritten, n = retarget(rule.get("when") or "", field, value)
        except SyntaxError:
            continue
        if n:
            rule["when"] = rewritten
            hits += n
    return patched, hits


def sweep(domain_name: str, version: str, field: str, values: list[float],
          clause: str = "", baseline_version: str | None = None,
          cases: list[Case] | None = None) -> dict:
    """Re-run the replay at each candidate threshold.

    The baseline is judged once and reused: it is the policy already in force,
    which no candidate threshold changes, so re-judging it per value would only
    make the sweep slower and let the deviation counts drift between points that
    are supposed to be comparable.
    """
    domain = load_domain(domain_name)
    if cases is None:
        cases = load_cases(domain_name, until=datetime.now())
    if not cases:
        raise LookupError(f"no {domain_name} cases on file; seed the history first")

    baseline_version = domain.in_force if baseline_version is None else baseline_version
    baseline = None
    if baseline_version:
        baseline = {c.case_id: offline_verdict(c, domain, baseline_version) for c in cases}

    dials = thresholds(domain, version)
    current = next((t["value"] for t in dials
                    if t["field"] == field and (not clause or t["clause"] == clause)), None)

    points = []
    for value in values:
        patched, hits = variant(domain, version, field, value, clause)
        if not hits:
            where = f" clause {clause}" if clause else ""
            raise LookupError(
                f"no rule in {domain_name}/{version}{where} compares {field!r} to a number; "
                f"available dials: {sorted({(t['clause'], t['field']) for t in dials})}"
            )
        verdicts = {c.case_id: offline_verdict(c, patched, version) for c in cases}
        found = diff.flips(cases, verdicts, patched, baseline=baseline)
        summary = diff.summarise(found, len(cases), patched)
        points.append({
            "value": value,
            "is_current": current is not None and float(value) == float(current),
            "flips": summary["flips"],
            "flip_rate": summary["flip_rate"],
            "loosening": summary["loosening"],
            "tightening": summary["tightening"],
            "net_impact": summary["net_impact"],
            "policy_driven_flips": summary["policy_driven_flips"],
            "policy_driven_net_impact": summary["policy_driven_net_impact"],
            "deviation_flips": summary["deviation_flips"],
        })

    return {
        "domain": domain_name,
        "version": version,
        "field": field,
        "clause": clause,
        "current_value": current,
        "baseline_version": baseline_version or "",
        "cases": len(cases),
        "impact_unit": domain.impact_unit,
        "points": points,
    }


def parse_values(raw: str) -> list[float]:
    out: list[float] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            number = float(chunk)
            out.append(int(number) if number.is_integer() else number)
    return out


USAGE = (
    "usage: python -m ptm.sweep <domain> <version>                        list the dials\n"
    "       python -m ptm.sweep <domain> <version> <field> <v1,v2,...>\n"
    "       python -m ptm.sweep <domain> <version> <clause> <field> <v1,v2,...>"
)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 2:
        print(USAGE)
        return 2

    domain_name, version = args[0], args[1]
    domain = load_domain(domain_name)
    if len(args) == 2:
        print(f"numeric dials in {domain_name}/{version}:")
        for t in thresholds(domain, version):
            print(f"  clause {t['clause'] or '-':<6} {t['field']:<24} = {t['value']:<10} "
                  f"-> {t['outcome']}")
        return 0
    if len(args) == 4:
        clause, field, raw = "", args[2], args[3]
    elif len(args) >= 5:
        clause, field, raw = args[2], args[3], args[4]
    else:
        print(USAGE)
        return 2

    result = sweep(domain_name, version, field, parse_values(raw), clause=clause)
    unit = result["impact_unit"]
    where = f"clause {clause} " if clause else ""
    print(f"sweeping {where}{field} in {domain_name}/{version} over {result['cases']} cases "
          f"(baseline {result['baseline_version'] or 'none'})")
    header = f"{field:>14}{'flips':>8}{'rate':>8}{'loosen':>8}{'tighten':>9}"
    print(header + f"{'net ' + unit:>13}{'policy-driven':>15}")
    for p in result["points"]:
        mark = "  <- current" if p["is_current"] else ""
        print(f"{p['value']:>14}{p['flips']:>8}{p['flip_rate']:>7.1%}{p['loosening']:>8}"
              f"{p['tightening']:>9}{p['net_impact']:>13,.0f}"
              f"{p['policy_driven_flips']:>15}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
