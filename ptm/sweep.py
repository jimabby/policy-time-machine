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

from . import cli, diff
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

    def visit_Compare(self, node: ast.Compare) -> ast.Compare:
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
    """Move every number ``field`` is compared against, and say how many moved.

    A deliberately mechanical primitive: it moves *every* literal, which is
    right for the one-sided threshold a clause normally states and wrong for a
    two-sided one. ``40 < amount <= 100`` comes back as ``60 < amount <= 60``,
    a condition no case can satisfy. Callers that are drawing a curve somebody
    will act on must ask :func:`collapsing` first - see why there.
    """
    tree = ast.parse(expression, mode="eval")
    rewriter = _Retarget(field, value)
    tree = ast.fix_missing_locations(rewriter.visit(tree))
    return ast.unparse(tree), rewriter.hits


def compared_values(expression: str) -> dict[str, list]:
    """Every numeric literal each field is compared against, by field.

    Both spellings of a band land in the same place: ``40 < x <= 100`` is one
    ``Compare`` node with two constants, and ``x > 40 and x <= 100`` is two
    nodes with one each, so counting literals per field rather than per node is
    what makes the two indistinguishable here - as they are to a reader.
    """
    out: dict[str, list] = {}
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return out
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
            out.setdefault(fields[0], []).extend(numbers)
    return out


def collapsing(domain: DomainConfig, version: str, field: str = "",
               clause: str = "") -> list[dict]:
    """Rules a sweep would silently destroy rather than move.

    :func:`retarget` moves every literal a field is compared against, so a rule
    stating a *band* - ``40 < amount <= 100``, the shape of every "reimbursed
    between" clause - collapses to ``60 < amount <= 60`` at every setting
    tried. The rewrite reports two hits, nothing raises, and the sweep returns a
    curve for a rule that fires on no case at any point of it. That is the worst
    failure this module can have: not a refusal, but a confident wrong answer
    about the number a policy owner is choosing.

    So it is detected rather than caveated. ``field`` and ``clause`` narrow it
    to the dial somebody actually asked for; blank checks every dial in the
    version, which is what :mod:`ptm.lint` wants.
    """
    found: list[dict] = []
    for index, rule in enumerate(domain.offline_rules.get(version, [])):
        if clause and str(rule.get("clause", "")) != clause:
            continue
        expression = rule.get("when") or ""
        for name, values in compared_values(expression).items():
            if field and name != field:
                continue
            if len(set(values)) > 1:
                found.append({
                    "clause": str(rule.get("clause", "")),
                    "rule_index": index,
                    "field": name,
                    "values": sorted(set(values)),
                    "expression": expression,
                })
    return found


def describe_collapsing(found: list[dict], domain_name: str, version: str) -> str:
    """Why a sweep refused, in the terms the person who wrote the rule reads."""
    lines = [f"{len(found)} rule(s) in {domain_name}/{version} compare a field to more "
             f"than one number, so moving that dial would not move a threshold - it "
             f"would collapse the comparison:"]
    for row in found:
        lines.append(f"  clause {row['clause'] or '-'} rule[{row['rule_index']}]: "
                     f"{row['field']} is compared against {row['values']} in "
                     f"{row['expression']!r}")
    lines.append("  every setting swept would rewrite all of them to the same number, and "
                 "the rule would then match nothing at any point on the curve. Split the "
                 "band across two rules, or sweep a dial that states one threshold.")
    return "\n".join(lines)


def thresholds(domain: DomainConfig, version: str) -> list[dict]:
    """Every numeric dial the offline rules for ``version`` expose.

    This is the menu a sweep is chosen from, so it reports where each dial lives
    rather than only its value: the same field can be compared in more than one
    clause, and moving "the receipt threshold" may mean moving all of them.
    """
    found: list[dict] = []
    for index, rule in enumerate(domain.offline_rules.get(version, [])):
        expression = rule.get("when") or ""
        for name, values in compared_values(expression).items():
            found.append({
                "clause": str(rule.get("clause", "")),
                "rule_index": index,
                "field": name,
                "value": values[0],
                "outcome": rule.get("outcome", ""),
                "expression": expression,
                # A dial listed so it can be refused, not so it can be swept.
                # The menu has to show it - a field that silently vanished from
                # the list is a reader concluding the policy has no such
                # threshold - and it has to say that sweeping it is not a thing
                # this can do. See :func:`collapsing`.
                "collapses": len(set(values)) > 1,
                "values": sorted(set(values)),
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

    # Before anything is measured: a band rule does not move, it collapses, and
    # a curve drawn over one is wrong rather than missing. See :func:`collapsing`.
    broken = collapsing(domain, version, field, clause)
    if broken:
        raise LookupError(describe_collapsing(broken, domain_name, version))

    dials = thresholds(domain, version)
    current = next((t["value"] for t in dials
                    if t["field"] == field and (not clause or t["clause"] == clause)), None)

    points = []
    signatures = []
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
        # Which cases moved, not how many. Two settings can reach the same count
        # through different cases, and calling that "no effect" would be the same
        # confident wrong answer the collapsing check exists to prevent.
        signatures.append(_signature(found))
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
        "inert": (_reading(len(set(signatures)) == 1, field, clause)
                  if len(signatures) > 1 else _unmeasured()),
    }


def _signature(found: list) -> frozenset:
    """Which cases moved and where to - the thing a setting either changes or not."""
    return frozenset((f.case_id, f.new_outcome) for f in found)


def _unmeasured() -> dict:
    """Too few settings to say anything, said rather than defaulted to 'moves'."""
    return {"measured": False,
            "inert": False,
            "hint": "a dial needs at least two settings before anything can be said about "
                    "whether it moves decisions",
            "hint_key": "hint.dial_needs_two"}


def _reading(inert: bool, field: str, clause: str = "") -> dict:
    """Whether a dial moved any decision at all, across the settings tried.

    The finding a flat curve is actually making, said out loud. A dial can be
    perfectly sweepable, rewrite cleanly at every setting, and still not change
    one outcome anywhere on its range - because a rule before it in the order
    decides those cases first, or because the outcome it would give is the one
    they fall through to anyway. The shipped fixture has exactly that: clause
    6.1's ``grade`` exemption is only ever reached by cases clause 1.1 has
    already declined to decide, and the outcome it gives is the default. Moving
    it changes which clause is *cited* and nothing else.

    A column of identical numbers is not wrong there, it just reads as "this
    threshold is not very sensitive" - a far weaker claim than "this threshold
    decides nothing". The first invites somebody to pick a round number off the
    curve; the second sends them to look at rule order.
    """
    where = f"clause {clause} " if clause else ""
    return {
        "measured": True,
        "inert": inert,
        "field": field,
        "clause": clause,
        "note": (f"no setting tried moves a single decision: {where}{field} changes which "
                 f"clause is cited, not what anybody gets. The fix is in the rules before "
                 f"it in the order - one of them is deciding these cases first - not in "
                 f"the number."
                 if inert else
                 f"{where}{field} moves decisions across the range swept"),
        "note_key": "note.dial_inert" if inert else "note.dial_moves",
        "note_args": {"field": field, "clause": clause},
    }


def joint(domain_name: str, version: str, first: dict, second: dict,
          baseline_version: str | None = None,
          cases: list[Case] | None = None) -> dict:
    """Two dials at once, as a grid rather than two curves.

    One-at-a-time sweeping answers *"what happens if I move this"* and quietly
    assumes the answer does not depend on where the other dial is sitting.
    Policy thresholds are exactly where that assumption fails: an exemption and
    the restriction it carves out of interact by construction, so the best
    setting for one, found with the other held still, can be the wrong setting
    once both move. A curve cannot show that and a grid can.

    ``first`` and ``second`` are ``{"field", "values", "clause"}``. Everything
    else works like :func:`sweep` - the baseline is judged once and reused,
    because no candidate setting changes the policy already in force.

    Cheap for the same reason the single sweep is: pure rule evaluation over
    cases already on file, no model and no bill. It is |A| x |B| replays, so the
    caller is expected to have narrowed the range with two single sweeps first.
    """
    domain = load_domain(domain_name)
    if cases is None:
        cases = load_cases(domain_name, until=datetime.now())
    if not cases:
        raise LookupError(f"no {domain_name} cases on file; seed the history first")
    if first.get("field") == second.get("field") and \
            str(first.get("clause") or "") == str(second.get("clause") or ""):
        raise LookupError(
            "both axes name the same dial, so every point on the diagonal would be the "
            "only real measurement. Sweep one axis on its own instead.")

    for axis in (first, second):
        broken = collapsing(domain, version, axis.get("field", ""),
                            str(axis.get("clause") or ""))
        if broken:
            raise LookupError(describe_collapsing(broken, domain_name, version))

    baseline_version = domain.in_force if baseline_version is None else baseline_version
    baseline = None
    if baseline_version:
        baseline = {c.case_id: offline_verdict(c, domain, baseline_version) for c in cases}

    dials = thresholds(domain, version)

    def current_of(axis: dict):
        clause = str(axis.get("clause") or "")
        return next((d["value"] for d in dials
                     if d["field"] == axis["field"] and (not clause or d["clause"] == clause)),
                    None)

    current_first, current_second = current_of(first), current_of(second)
    points = []
    signatures: dict[tuple, frozenset] = {}
    for a in first["values"]:
        patched_a, hits_a = variant(domain, version, first["field"], a,
                                    str(first.get("clause") or ""))
        if not hits_a:
            raise LookupError(_no_dial(domain_name, version, first, dials))
        for b in second["values"]:
            patched, hits_b = variant(patched_a, version, second["field"], b,
                                      str(second.get("clause") or ""))
            if not hits_b:
                raise LookupError(_no_dial(domain_name, version, second, dials))
            verdicts = {c.case_id: offline_verdict(c, patched, version) for c in cases}
            found = diff.flips(cases, verdicts, patched, baseline=baseline)
            summary = diff.summarise(found, len(cases), patched)
            signatures[(a, b)] = _signature(found)
            points.append({
                first["field"]: a,
                second["field"]: b,
                "first_value": a,
                "second_value": b,
                "is_current": (current_first is not None and float(a) == float(current_first)
                               and current_second is not None
                               and float(b) == float(current_second)),
                "flips": summary["flips"],
                "flip_rate": summary["flip_rate"],
                "loosening": summary["loosening"],
                "tightening": summary["tightening"],
                "net_impact": summary["net_impact"],
                "policy_driven_flips": summary["policy_driven_flips"],
                "policy_driven_net_impact": summary["policy_driven_net_impact"],
                "deviation_flips": summary["deviation_flips"],
            })

    # Whether the two dials interact at all, in one number. If moving the second
    # dial changed the first's effect by nothing, the grid was not worth running
    # and two curves would have told the whole story - saying so is what stops
    # this becoming a panel people look at out of habit.
    return {
        "domain": domain_name,
        "version": version,
        "first": {**first, "current": current_first},
        "second": {**second, "current": current_second},
        "baseline_version": baseline_version or "",
        "cases": len(cases),
        "impact_unit": domain.impact_unit,
        "points": points,
        "interaction": _interaction(points, first["values"], second["values"]),
        # Each axis on its own. A grid of identical columns reports
        # ``independent``, which is true and is read as the opposite of the
        # sentence a reader needs: that one of these two dials decides nothing
        # at any setting of the other.
        "first_inert": _axis_inert(signatures, first, second, "first"),
        "second_inert": _axis_inert(signatures, first, second, "second"),
    }


def _axis_inert(signatures: dict, first: dict, second: dict, axis: str) -> dict:
    """Whether one axis of a grid moves anything, at every setting of the other.

    Every setting rather than one of them, which is the only reading that
    survives two dials interacting: a threshold that does nothing while its
    neighbour sits at 25 and a great deal at 250 is not an inert dial, and a
    single row or column would report it as one.
    """
    moving, holding = (first, second) if axis == "first" else (second, first)
    field, clause = moving["field"], str(moving.get("clause") or "")
    if len(moving["values"]) < 2:
        return _unmeasured()
    measured = False
    for held in holding["values"]:
        along = [signatures.get((a, held) if axis == "first" else (held, a))
                 for a in moving["values"]]
        along = [sig for sig in along if sig is not None]
        if len(along) < 2:
            continue
        measured = True
        if len(set(along)) > 1:  # it moved somewhere, which settles it
            return _reading(False, field, clause)
    return _reading(True, field, clause) if measured else _unmeasured()


def _no_dial(domain_name: str, version: str, axis: dict, dials: list[dict]) -> str:
    where = f" clause {axis['clause']}" if axis.get("clause") else ""
    return (f"no rule in {domain_name}/{version}{where} compares {axis['field']!r} to a "
            f"number; available dials: {sorted({(d['clause'], d['field']) for d in dials})}")


def _interaction(points: list[dict], firsts: list, seconds: list) -> dict:
    """How much the effect of one dial depends on where the other one is.

    Measured as the spread of the *first* dial's effect across the second dial's
    settings: for each column, how far the flip count moves from the top of the
    first axis to the bottom, and then how much those movements differ from each
    other. Zero means the dials are independent and two separate curves say
    everything this grid does.
    """
    if len(firsts) < 2 or len(seconds) < 2:
        return {"measured": False,
                "hint": "a grid needs at least two settings on each axis to say whether "
                        "the dials interact",
                "hint_key": "hint.grid_needs_two"}
    by_pair = {(p["first_value"], p["second_value"]): p["flips"] for p in points}
    spans = []
    for b in seconds:
        column = [by_pair.get((a, b)) for a in firsts]
        column = [v for v in column if v is not None]
        if len(column) > 1:
            spans.append(max(column) - min(column))
    if not spans:
        return {"measured": False, "hint": "the grid is incomplete",
                "hint_key": "hint.grid_incomplete"}
    spread = max(spans) - min(spans)
    return {
        "measured": True,
        # The first dial's effect, at its weakest and strongest position of the
        # second. Reported as counts because that is what the rest of the panel
        # is in, and a normalised index nobody can check is worse than a number.
        "effect_min_flips": min(spans),
        "effect_max_flips": max(spans),
        "interaction_flips": spread,
        "independent": spread == 0,
        "note": "how far the first dial moves the decision base, at the second dial's "
                "least and most favourable setting. Equal means the two are independent "
                "and two single sweeps would have told you the same thing.",
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
    "       python -m ptm.sweep <domain> <version> <clause> <field> <v1,v2,...>\n"
    "       python -m ptm.sweep <domain> <version> --joint <clause>:<field>=<v1,v2,...> "
    "<clause>:<field>=<v1,v2,...>\n"
    "           two dials at once, as a grid. The clause is optional: ':field=..' or "
    "'field=..' moves every\n"
    "           occurrence of the field in the version."
)


def parse_axis(raw: str) -> dict:
    """``1.1:amount=25,50,75`` - or ``amount=25,50`` for every clause using it."""
    if "=" not in raw:
        raise ValueError(f"axis {raw!r} needs the form [clause:]field=v1,v2,...")
    head, values = raw.split("=", 1)
    clause, _, field = head.rpartition(":")
    if not field:
        raise ValueError(f"axis {raw!r} names no field")
    return {"clause": clause, "field": field, "values": parse_values(values)}


def _print_joint(result: dict) -> None:
    unit = result["impact_unit"]
    first, second = result["first"], result["second"]
    print(f"sweeping {first['field']} x {second['field']} in {result['domain']}/"
          f"{result['version']} over {result['cases']} cases "
          f"(baseline {result['baseline_version'] or 'none'})")
    seconds = list(dict.fromkeys(p["second_value"] for p in result["points"]))
    by_pair = {(p["first_value"], p["second_value"]): p for p in result["points"]}
    # The row label is the first dial's setting; the column label is the
    # second's. One prefix width for both, so the grid lines up under its own
    # header - a table a reader has to count across is a table nobody reads.
    # Both labels built outside the f-string. A backslash inside an f-string's
    # expression part is a SyntaxError before Python 3.12, and the engine job
    # runs 3.10 - so this module would not import there at all.
    label = "{} \\ {}".format(first["field"][:11], second["field"][:11])
    subhead = "(rows \\ columns)"
    width = max(len(label) + 2, 20)
    print(f"{label:<{width}}" + "".join(f"{v:>12}" for v in seconds))
    print(f"{subhead:<{width}}" + "".join(f"{'flips':>12}" for _ in seconds))
    for a in dict.fromkeys(p["first_value"] for p in result["points"]):
        cells = []
        for b in seconds:
            point = by_pair.get((a, b))
            mark = "*" if point and point["is_current"] else " "
            cells.append(f"{point['flips'] if point else '-':>11}{mark}")
        print(f"{a:<{width}}" + "".join(cells))
    print(f"  (* = the settings in force. net {unit} and the policy-driven split are in "
          f"the JSON form of this result.)")
    for side in ("first_inert", "second_inert"):
        reading = result.get(side) or {}
        if reading.get("measured") and reading.get("inert"):
            print(f"\n  WARNING {reading['note']}")
    interaction = result["interaction"]
    if interaction.get("measured"):
        print(f"\ninteraction: moving {first['field']} changes "
              f"{interaction['effect_min_flips']}-{interaction['effect_max_flips']} "
              f"decisions depending on where {second['field']} sits "
              f"({interaction['interaction_flips']} apart)")
        print("  " + ("the two dials are independent here, so two single sweeps would "
                      "have told you the same thing"
                      if interaction["independent"] else
                      "the dials interact: the best setting for one depends on the other, "
                      "which is what a pair of single sweeps cannot show"))


def _warn_about_the_rules(domain_name: str, version: str) -> None:
    """Say what the curve above is worth, next to the curve above.

    The sweep is arithmetic over ``offline_rules``, so it is only ever as good
    as the rules' agreement with the judge. Printing the caveat somewhere else
    means it is read by somebody other than the person acting on the numbers.
    """
    from . import report as report_module

    try:
        result = report_module.rule_agreement(domain_name, version)
    except LookupError:
        return
    if result.get("inert"):
        print("\n  these curves are computed from the offline rules, and the verdicts "
              "they were last scored against came from those same rules - so nothing "
              "here has been checked against a real judge")
        return
    if not result.get("compared"):
        print("\n  these curves are computed from the offline rules, which have never "
              "been scored against a judge - run the replay, then python -m ptm.rules")
        return
    print(f"\n  computed from offline rules agreeing with the judge on "
          f"{result['rate']:.1%} of {result['compared']} case(s), citing the same clause "
          f"{result['clause_agreement']:.1%} of the time")
    from .config import load_domain as _load

    for problem in report_module.rules_engine.gate(result, _load(domain_name)):
        print(f"  WARNING {problem}")


def _is_option(arg: str) -> bool:
    """Whether an argument is a flag rather than a value.

    Deliberately narrower than ``startswith("-")``, which is what every other
    entry point here can afford to use because none of them takes a number
    positionally. This one does: ``python -m ptm.sweep expenses v2 balance
    -50,0,50`` sweeps a threshold that is legitimately negative, and reading the
    leading minus as a flag would refuse a sweep that used to work. Two dashes,
    or the help flag, and nothing else.
    """
    return arg.startswith("--") or arg in cli.HELP_FLAGS


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0
    # The positional arguments, taken apart from the flags before any of them is
    # read as a name. This module was the last entry point still interpreting
    # argv by its raw length, which is why `--joint` had to be filtered out of
    # the axis list by hand and why a stray flag shifted `field` onto `values`.
    joint_mode = "--joint" in args
    positional = [a for a in args if not _is_option(a)]
    unknown = [a for a in args if _is_option(a) and a != "--joint"]
    if unknown:
        print(f"ERROR unknown option {unknown[0]!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    if len(positional) < 2:
        print(USAGE)
        return 2

    domain_name, version = positional[0], positional[1]
    # Both resolved here, before anything is computed, and both reported the way
    # every sibling entry point reports a refusal. An unknown domain used to
    # arrive as a FileNotFoundError traceback - exit 1, where the rest of this
    # project reserves 2 for "could not be run" - and an unknown *version* was
    # worse: it printed an empty dial list and exited 0, which reads as a policy
    # that has no thresholds rather than a policy that does not exist. That is
    # the same confident-wrong-answer failure `thresholds()` refuses to make
    # about a band rule, made about the whole version.
    try:
        domain = load_domain(domain_name)
    except FileNotFoundError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if version not in domain.policies:
        print(f"ERROR unknown policy version {version!r} for {domain_name}; have "
              f"{sorted(domain.policies)}", file=sys.stderr)
        return 2

    if joint_mode:
        axes = positional[2:]
        if len(axes) != 2:
            print(USAGE)
            return 2
        try:
            # parse_axis and parse_values raise ValueError, which every caller
            # here used to let through as a traceback: the message was already
            # the right sentence, it simply arrived as a crash.
            first, second = parse_axis(axes[0]), parse_axis(axes[1])
        except ValueError as exc:
            print(f"ERROR {exc}\n\n{USAGE}", file=sys.stderr)
            return 2
        try:
            _print_joint(joint(domain_name, version, first, second))
        except LookupError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        _warn_about_the_rules(domain_name, version)
        return 0
    if len(positional) == 2:
        print(f"numeric dials in {domain_name}/{version}:")
        found = thresholds(domain, version)
        for t in found:
            note = (f"  [not sweepable: compared against {t['values']}]"
                    if t["collapses"] else "")
            print(f"  clause {t['clause'] or '-':<6} {t['field']:<24} = {t['value']:<10} "
                  f"-> {t['outcome']}{note}")
        if not found:
            # An empty list is a finding about the policy, and it is the one the
            # old unknown-version path produced by accident. Said out loud now
            # that it can only mean what it says.
            print("  none: this version's offline rules compare no field against a "
                  "number, so there is no dial to sweep")
        return 0
    if len(positional) == 4:
        clause, field, raw = "", positional[2], positional[3]
    elif len(positional) >= 5:
        clause, field, raw = positional[2], positional[3], positional[4]
    else:
        print(USAGE)
        return 2

    try:
        values = parse_values(raw)
    except ValueError as exc:
        print(f"ERROR the settings to sweep must be comma-separated numbers: {exc}\n\n"
              f"{USAGE}", file=sys.stderr)
        return 2
    if not values:
        print(f"ERROR a sweep needs at least one setting to try\n\n{USAGE}",
              file=sys.stderr)
        return 2
    try:
        result = sweep(domain_name, version, field, values, clause=clause)
    except LookupError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
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
    # Ahead of the caveat about the rules, because it is the stronger statement:
    # a flat curve is not a curve anybody should be picking a round number off.
    inert = result.get("inert") or {}
    if inert.get("measured") and inert.get("inert"):
        print(f"\n  WARNING {inert['note']}")
    # After the table, not before it: the caveat is about the numbers a reader
    # has just seen, and above them it is read as preamble and skipped.
    _warn_about_the_rules(domain_name, version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
