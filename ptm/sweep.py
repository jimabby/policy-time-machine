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
import functools
import json
import math
import sys

from . import cli, diff
from .config import DomainConfig, load_domain
from .judge import offline_verdict
from .models import Case
from .store import load_cases, now_utc

#: Which side of the field a literal sits on, read from the operator between
#: them. ``amount < 100`` puts 100 *above* the field and ``100 < amount`` puts
#: it below, so the same operator means opposite things depending on which side
#: of it the field was written. Both spellings are ordinary and a check that
#: knew only one would move the wrong end of half the bands anybody writes.
_FIELD_ON_LEFT = {ast.Lt: "upper", ast.LtE: "upper", ast.Gt: "lower",
                  ast.GtE: "lower", ast.Eq: "point"}
_FIELD_ON_RIGHT = {ast.Lt: "lower", ast.LtE: "lower", ast.Gt: "upper",
                   ast.GtE: "upper", ast.Eq: "point"}

#: Operators whose bound includes its own value, which is the difference
#: between ``40 < x <= 40`` (empty) and ``40 <= x <= 40`` (one value).
_CLOSED = (ast.LtE, ast.GtE, ast.Eq)

#: The two ends a caller may ask for by name.
EDGES = ("lower", "upper")

#: Where a literal sits when the shape of the comparison does not say. A number
#: in the same node as the field but not adjacent to it - ``5 < 10 < amount`` -
#: is one, and so is anything else this cannot place. It is kept rather than
#: dropped, because a bound nobody can classify is the one case where moving
#: "the lower end" would move something else entirely.
LOOSE = "loose"


def _is_number(node: ast.AST) -> bool:
    """A numeric literal, with ``True``/``False`` excluded as they are elsewhere here."""
    return (isinstance(node, ast.Constant) and not isinstance(node.value, bool)
            and isinstance(node.value, (int, float)))


def _sides(node: ast.Compare, field: str) -> dict[int, tuple[str, bool]]:
    """Which side of ``field`` each literal in one comparison sits on.

    Keyed by position in ``[left, *comparators]``, valued ``(side, closed)``.
    Chained and conjunctive spellings of a band land on the same answer, which
    is the property :func:`compared_values` already has and the reason the two
    are read together: ``40 < x <= 100`` is one node with two literals, and
    ``x > 40 and x <= 100`` is two nodes with one each.

    Only a comparison mentioning exactly one field is classified, which is the
    same gate :func:`compared_values` applies - ``amount > other`` states a
    relation between two unknowns and has no dial on it.
    """
    operands = [node.left, *node.comparators]
    names = [n.id for n in operands if isinstance(n, ast.Name)]
    if len(names) != 1 or names[0] != field:
        return {}
    out: dict[int, tuple[str, bool]] = {}
    for i, op in enumerate(node.ops):
        left, right = operands[i], operands[i + 1]
        closed = isinstance(op, _CLOSED)
        if isinstance(left, ast.Name) and left.id == field and _is_number(right):
            side = _FIELD_ON_LEFT.get(type(op))
            if side:
                out[i + 1] = (side, closed)
        elif isinstance(right, ast.Name) and right.id == field and _is_number(left):
            side = _FIELD_ON_RIGHT.get(type(op))
            if side:
                out[i] = (side, closed)
    return out


def bounds_in(expression: str, field: str) -> dict[str, list[dict]]:
    """Every number ``field`` is compared against, grouped by which end it states.

    This is what makes a band sweepable one end at a time. ``compared_values``
    answers *how many numbers is this field compared against*, which is all that
    is needed to refuse a dial; moving one end of it needs to know **which**
    number is the floor and which is the ceiling.

    Four buckets: ``lower``, ``upper``, ``point`` (an ``==``, which is both ends
    at once and therefore not an end anybody can move on its own) and
    :data:`LOOSE` for a literal whose position this cannot establish. Callers
    treat a non-empty ``loose`` bucket as "do not touch this rule" - see
    :func:`collapsing`.
    """
    out: dict[str, list[dict]] = {"lower": [], "upper": [], "point": [], LOOSE: []}
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        names = [n.id for n in operands if isinstance(n, ast.Name)]
        numbers = [i for i, n in enumerate(operands) if _is_number(n)]
        if len(names) != 1 or names[0] != field or not numbers:
            continue
        sides = _sides(node, field)
        for i in numbers:
            side, closed = sides.get(i, (LOOSE, False))
            out[side].append({"value": operands[i].value, "closed": closed})
    return out


class _Retarget(ast.NodeTransformer):
    """Rewrite the numeric literals compared against ``field`` to ``value``.

    ``edge`` narrows that to one end of a band - ``lower`` or ``upper`` - and
    leaves the other where it is. Blank moves every literal, which is the
    behaviour every caller had before ends could be named and the right one for
    the one-sided threshold a clause normally states.
    """

    def __init__(self, field: str, value: float, edge: str = "") -> None:
        self.field = field
        self.value = value
        self.edge = edge
        self.hits = 0

    def visit_Compare(self, node: ast.Compare) -> ast.Compare:
        self.generic_visit(node)
        operands = [node.left, *node.comparators]
        positions = {i for i, n in enumerate(operands)
                     if isinstance(n, ast.Name) and n.id == self.field}
        if not positions:
            return node
        sides = _sides(node, self.field) if self.edge else {}
        for i, operand in enumerate(operands):
            if i in positions or not isinstance(operand, ast.Constant):
                continue
            if isinstance(operand.value, bool) or not isinstance(operand.value, (int, float)):
                continue
            if self.edge and sides.get(i, (LOOSE, False))[0] != self.edge:
                continue
            operands[i] = ast.Constant(value=self.value)
            self.hits += 1
        node.left, node.comparators = operands[0], operands[1:]
        return node


def retarget(expression: str, field: str, value: float, edge: str = "") -> tuple[str, int]:
    """Move the numbers ``field`` is compared against, and say how many moved.

    A deliberately mechanical primitive. Without ``edge`` it moves *every*
    literal, which is right for the one-sided threshold a clause normally
    states and wrong for a two-sided one: ``40 < amount <= 100`` comes back as
    ``60 < amount <= 60``, a condition no case can satisfy. Callers that are
    drawing a curve somebody will act on must ask :func:`collapsing` first -
    see why there.

    ``edge`` is the way to move a band rather than destroy it. ``"lower"``
    moves only the floor and ``"upper"`` only the ceiling, so the same rule
    comes back as ``60 < amount <= 100`` - which is the rule the policy owner
    was actually asking about. It does not make the result *sensible* on its
    own: pushing a floor past its ceiling still empties the rule, and that is a
    real setting with a real meaning rather than a rewrite artefact, so it is
    reported by :func:`emptied` rather than refused here.
    """
    tree = ast.parse(expression, mode="eval")
    rewriter = _Retarget(field, value, edge)
    tree = ast.fix_missing_locations(rewriter.visit(tree))
    return ast.unparse(tree), rewriter.hits


def _and_terms(node: ast.AST) -> list[ast.AST]:
    """The top-level ``and`` terms of an expression, flattened."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        out: list[ast.AST] = []
        for value in node.values:
            out.extend(_and_terms(value))
        return out
    return [node]


def _mentions(node: ast.AST, field: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == field for n in ast.walk(node))


def empty_for(expression: str, field: str) -> bool:
    """Whether no value of ``field`` can satisfy this rule any more.

    The failure this exists to catch is a floor pushed above its own ceiling.
    ``40 < amount <= 100`` swept on its lower edge to 150 becomes
    ``150 < amount <= 100``, which is a perfectly legal expression that matches
    nothing - and a flat row of zeroes on a curve reads as "this threshold is
    not very sensitive" rather than as "this clause has stopped existing".

    Sound rather than complete, the same way :func:`ptm.safe_eval.implies` is.
    It reasons only over the top-level ``and`` terms, because those are the ones
    that must *all* hold; a term that mentions the field under an ``or`` or
    inside anything else makes the question unanswerable here, and it answers
    ``False`` rather than guessing. Being wrong in this direction costs a
    warning nobody sees. Being wrong in the other would refuse a live rule.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return False
    low, low_closed = float("-inf"), True
    high, high_closed = float("inf"), True
    constrained = False
    for term in _and_terms(tree.body):
        if not isinstance(term, ast.Compare):
            if _mentions(term, field):
                return False  # a shape this cannot reason about soundly
            continue
        sides = _sides(term, field)
        if not sides and _mentions(term, field):
            continue
        operands = [term.left, *term.comparators]
        for i, (side, closed) in sides.items():
            value = operands[i].value
            if side in ("lower", "point") and (value > low or (value == low and not closed)):
                low, low_closed = value, closed if side == "lower" else True
                constrained = True
            if side in ("upper", "point") and (value < high or (value == high and not closed)):
                high, high_closed = value, closed if side == "upper" else True
                constrained = True
    if not constrained:
        return False
    if low > high:
        return True
    # Equal ends are empty unless both of them include the value itself:
    # ``40 <= x <= 40`` admits exactly 40, ``40 < x <= 40`` admits nothing.
    return low == high and not (low_closed and high_closed)


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
               clause: str = "", edge: str = "") -> list[dict]:
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

    ``edge`` is what turns the refusal back into a sweep. A band has two ends
    and each of them *is* a single threshold, so ``lower`` or ``upper`` asks for
    one of them by name, and the rule is reported only when that end is still
    ambiguous: two floors stated in one rule, or a literal :func:`bounds_in`
    could not place on either end. Naming an end the rule does not state is not
    reported here - it produces no rewrite, and :func:`sweep` already refuses a
    dial it cannot move, with a message naming the dials that exist.
    """
    found: list[dict] = []
    for index, rule in enumerate(domain.offline_rules.get(version, [])):
        if clause and str(rule.get("clause", "")) != clause:
            continue
        expression = rule.get("when") or ""
        for name, values in compared_values(expression).items():
            if field and name != field:
                continue
            if len(set(values)) <= 1:
                continue
            if edge in EDGES:
                sides = bounds_in(expression, name)
                # One number on the end being moved, and every number in the
                # rule placed on some end: the ambiguity that made this a
                # refusal is gone, and what is left is an ordinary threshold.
                if len({b["value"] for b in sides[edge]}) <= 1 and not sides[LOOSE]:
                    continue
            found.append({
                "clause": str(rule.get("clause", "")),
                "rule_index": index,
                "field": name,
                "values": sorted(set(values)),
                "expression": expression,
                "edge": edge,
            })
    return found


def describe_collapsing(found: list[dict], domain_name: str, version: str) -> str:
    """Why a sweep refused, in the terms the person who wrote the rule reads."""
    edge = next((row.get("edge") for row in found if row.get("edge")), "")
    lines = [f"{len(found)} rule(s) in {domain_name}/{version} compare a field to more "
             f"than one number, so moving that dial would not move a threshold - it "
             f"would collapse the comparison:"]
    for row in found:
        lines.append(f"  clause {row['clause'] or '-'} rule[{row['rule_index']}]: "
                     f"{row['field']} is compared against {row['values']} in "
                     f"{row['expression']!r}")
    if edge:
        # The caller has already named an end, so the advice below would be
        # telling them to do the thing they just did. Say what is ambiguous
        # about the end they asked for instead.
        lines.append(f"  more than one {edge} bound is stated here, or one of these "
                     f"numbers sits on neither end, so '--edge {edge}' does not name a "
                     f"single threshold in this rule. Split the band across two rules to "
                     f"move its ends independently.")
    else:
        lines.append("  every setting swept would rewrite all of them to the same number, "
                     "and the rule would then match nothing at any point on the curve. "
                     "Sweep one end of it instead: '--edge lower' and '--edge upper' each "
                     "move one bound and leave the other where it is. Split the band "
                     "across two rules to move them independently.")
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
                # A dial the whole-rule rewrite cannot move. The menu has to
                # show it - a field that silently vanished from the list is a
                # reader concluding the policy has no such threshold - and it
                # has to say so. See :func:`collapsing`.
                "collapses": len(set(values)) > 1,
                "values": sorted(set(values)),
                # ...and which of its ends *can* be moved one at a time. A band
                # is two thresholds written in one sentence, so "not sweepable"
                # was always too strong a thing to say about it: it is the pair
                # that cannot move together, not either end on its own.
                "edges": sweepable_edges(expression, name),
                # The number sitting on each of those ends. Reported rather than
                # left to be inferred from the sorted pair above: "the lower end
                # is the smaller number" is true of a band and not of a dial
                # that states only a ceiling, and a caller that guessed would
                # label the wrong column on exactly the rules this is for.
                "edge_values": {edge: value for edge in EDGES
                                if (value := _edge_value(expression, name, edge))
                                is not None},
            })
    return found


def sweepable_edges(expression: str, field: str) -> list[str]:
    """Which ends of a dial ``--edge`` can move on their own, in menu order.

    An end qualifies when the rule states exactly one bound there and every
    number in the rule sits on one end or the other - the same test
    :func:`collapsing` applies, asked in the affirmative so the dial list can
    offer the route rather than only refuse the other one.

    A plain one-sided threshold reports the single end it states. That is not
    a suggestion to use ``--edge`` on it - the whole-rule rewrite already moves
    it correctly - but it is true, and a dial list that said a threshold had no
    ends would be a stranger thing to read.
    """
    sides = bounds_in(expression, field)
    if sides[LOOSE]:
        return []
    return [edge for edge in EDGES if len({b["value"] for b in sides[edge]}) == 1]


def variant(domain: DomainConfig, version: str, field: str, value: float,
            clause: str = "", edge: str = "") -> tuple[DomainConfig, int]:
    """A copy of the domain whose ``version`` rules use ``value`` for ``field``.

    ``clause`` narrows the rewrite to the rules citing it, which is what you
    want when a field appears in two clauses not meant to move together. Blank
    moves every occurrence in the version. ``edge`` narrows it to one end of a
    band - see :func:`retarget`.
    """
    patched = domain.model_copy(deep=True)
    hits = 0
    for rule in patched.offline_rules.get(version, []):
        if clause and str(rule.get("clause", "")) != clause:
            continue
        try:
            rewritten, n = retarget(rule.get("when") or "", field, value, edge)
        except SyntaxError:
            continue
        if n:
            rule["when"] = rewritten
            hits += n
    return patched, hits


def emptied(before: DomainConfig, after: DomainConfig, version: str,
            field: str) -> list[dict]:
    """Rules this setting has just stopped from matching anything.

    Moving one end of a band past the other is a legal rewrite that produces a
    legal rule: ``150 < amount <= 100`` parses, evaluates, and is false for
    every case there has ever been. On a curve that arrives as a row of zeroes,
    which reads as *this threshold is not very sensitive* rather than as *this
    clause no longer exists* - the same confident wrong answer
    :func:`collapsing` refuses to make, arriving one step later.

    So it is reported per setting rather than refused outright, because unlike
    a collapse it is not an artefact: a floor above its own ceiling is what the
    number the caller typed actually means, and somebody sweeping past the end
    of a band is entitled to see where that happens. Only rules the rewrite
    *changed* are reported - a rule already empty before the sweep is a lint
    finding, not a fact about this setting.
    """
    old_rules = before.offline_rules.get(version, [])
    new_rules = after.offline_rules.get(version, [])
    found: list[dict] = []
    for index, rule in enumerate(new_rules):
        expression = rule.get("when") or ""
        was = old_rules[index].get("when") or "" if index < len(old_rules) else ""
        if expression == was or not empty_for(expression, field):
            continue
        if empty_for(was, field):
            continue
        found.append({
            "clause": str(rule.get("clause", "")),
            "rule_index": index,
            "outcome": rule.get("outcome", ""),
            "expression": expression,
            "was": was,
        })
    return found


def sweep(domain_name: str, version: str, field: str, values: list[float],
          clause: str = "", baseline_version: str | None = None,
          cases: list[Case] | None = None, edge: str = "") -> dict:
    """Re-run the replay at each candidate threshold.

    The baseline is judged once and reused: it is the policy already in force,
    which no candidate threshold changes, so re-judging it per value would only
    make the sweep slower and let the deviation counts drift between points that
    are supposed to be comparable.

    ``edge`` sweeps one end of a band - ``lower`` or ``upper`` - leaving the
    other where the policy put it. Without it a band is refused rather than
    swept, because moving both ends to the same number destroys the rule; see
    :func:`collapsing`.
    """
    if edge and edge not in EDGES:
        raise LookupError(f"edge must be one of {list(EDGES)}, not {edge!r}")
    domain = load_domain(domain_name)
    if cases is None:
        cases = load_cases(domain_name, until=now_utc())
    if not cases:
        raise LookupError(f"no {domain_name} cases on file; seed the history first")

    baseline_version = domain.in_force if baseline_version is None else baseline_version
    baseline = None
    if baseline_version:
        baseline = {c.case_id: offline_verdict(c, domain, baseline_version) for c in cases}

    # Before anything is measured: a band rule does not move, it collapses, and
    # a curve drawn over one is wrong rather than missing. See :func:`collapsing`.
    broken = collapsing(domain, version, field, clause, edge)
    if broken:
        raise LookupError(describe_collapsing(broken, domain_name, version))

    dials = thresholds(domain, version)
    current = next((t["value"] for t in dials
                    if t["field"] == field and (not clause or t["clause"] == clause)), None)
    if edge:
        # With an end named, "the setting in force" is the number on that end
        # rather than the first number in the rule - otherwise the band's floor
        # gets the <- current marker while the ceiling is being swept.
        current = next((_edge_value(t["expression"], t["field"], edge) for t in dials
                        if t["field"] == field and (not clause or t["clause"] == clause)
                        and _edge_value(t["expression"], t["field"], edge) is not None),
                       None)

    points = []
    signatures = []
    dead: list[dict] = []
    for value in values:
        patched, hits = variant(domain, version, field, value, clause, edge)
        if not hits:
            where = f" clause {clause}" if clause else ""
            what = (f"states a {edge} bound on {field!r}" if edge
                    else f"compares {field!r} to a number")
            raise LookupError(
                f"no rule in {domain_name}/{version}{where} {what}; "
                f"available dials: {sorted({(t['clause'], t['field']) for t in dials})}"
            )
        gone = emptied(domain, patched, version, field) if edge else []
        verdicts = {c.case_id: offline_verdict(c, patched, version) for c in cases}
        found = diff.flips(cases, verdicts, patched, baseline=baseline)
        summary = diff.summarise(found, len(cases), patched)
        # Which cases moved, not how many. Two settings can reach the same count
        # through different cases, and calling that "no effect" would be the same
        # confident wrong answer the collapsing check exists to prevent.
        signatures.append(_signature(found))
        if gone:
            dead.append({"value": value, "rules": gone})
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
            # This setting put the band's ends the wrong way round, so the rule
            # matched nothing. The row is real and the number is right; what it
            # counts is a policy with that clause switched off.
            "empties_rule": bool(gone),
        })

    return {
        "domain": domain_name,
        "version": version,
        "field": field,
        "clause": clause,
        "edge": edge,
        "current_value": current,
        "baseline_version": baseline_version or "",
        "cases": len(cases),
        "impact_unit": domain.impact_unit,
        "points": points,
        "emptied": dead,
        "inert": (_reading(len(set(signatures)) == 1, field, clause)
                  if len(signatures) > 1 else _unmeasured()),
    }


def _edge_value(expression: str, field: str, edge: str):
    """The number stated on one end of a dial, or None when it states no such end."""
    values = {b["value"] for b in bounds_in(expression, field)[edge]}
    return values.pop() if len(values) == 1 else None


def describe_emptied(dead: list[dict], field: str, edge: str) -> str:
    """The settings that switched a clause off, said rather than left in a column."""
    settings = ", ".join(str(row["value"]) for row in dead)
    clauses = sorted({r["clause"] or "-" for row in dead for r in row["rules"]})
    return (f"at {settings} the {edge} bound crosses the other end, so clause(s) "
            f"{', '.join(clauses)} match no case at all. Those rows count a policy with "
            f"that clause switched off, not a threshold on {field} that stopped "
            f"mattering - read them as the end of the band rather than as points on "
            f"the curve.")


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
        cases = load_cases(domain_name, until=now_utc())
    if not cases:
        raise LookupError(f"no {domain_name} cases on file; seed the history first")
    if first.get("field") == second.get("field") and \
            str(first.get("clause") or "") == str(second.get("clause") or "") and \
            str(first.get("edge") or "") == str(second.get("edge") or ""):
        raise LookupError(
            "both axes name the same dial, so every point on the diagonal would be the "
            "only real measurement. Sweep one axis on its own instead.")

    for axis in (first, second):
        edge = str(axis.get("edge") or "")
        if edge and edge not in EDGES:
            raise LookupError(f"edge must be one of {list(EDGES)}, not {edge!r}")
        broken = collapsing(domain, version, axis.get("field", ""),
                            str(axis.get("clause") or ""), edge)
        if broken:
            raise LookupError(describe_collapsing(broken, domain_name, version))

    baseline_version = domain.in_force if baseline_version is None else baseline_version
    baseline = None
    if baseline_version:
        baseline = {c.case_id: offline_verdict(c, domain, baseline_version) for c in cases}

    dials = thresholds(domain, version)

    def current_of(axis: dict):
        clause = str(axis.get("clause") or "")
        edge = str(axis.get("edge") or "")
        matching = [d for d in dials if d["field"] == axis["field"]
                    and (not clause or d["clause"] == clause)]
        if not edge:
            return next((d["value"] for d in matching), None)
        # With an end named, the setting in force is the number on *that* end.
        # Reading d["value"] instead would mark the column at the band's floor
        # while its ceiling is the thing being moved.
        return next((value for d in matching
                     if (value := _edge_value(d["expression"], d["field"], edge)) is not None),
                    None)

    current_first, current_second = current_of(first), current_of(second)
    points = []
    signatures: dict[tuple, frozenset] = {}
    for a in first["values"]:
        patched_a, hits_a = variant(domain, version, first["field"], a,
                                    str(first.get("clause") or ""),
                                    str(first.get("edge") or ""))
        if not hits_a:
            raise LookupError(_no_dial(domain_name, version, first, dials))
        for b in second["values"]:
            patched, hits_b = variant(patched_a, version, second["field"], b,
                                      str(second.get("clause") or ""),
                                      str(second.get("edge") or ""))
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
    what = (f"states a {axis['edge']} bound on {axis['field']!r}" if axis.get("edge")
            else f"compares {axis['field']!r} to a number")
    return (f"no rule in {domain_name}/{version}{where} {what}; available dials: "
            f"{sorted({(d['clause'], d['field']) for d in dials})}")


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
    """Candidate thresholds from the comma-separated string a caller supplies.

    ``float()`` accepts ``nan``, ``inf`` and ``-inf``, and all three used to
    come straight through to a curve. Each one is a threshold no comparison can
    ever satisfy, so the rule stops firing entirely and the sweep reports the
    resulting decision base with the same confidence as a real number - while
    ``empties_rule``, the flag whose whole job is to say "this setting leaves a
    rule matching nothing", reads ``False``, because it compares against the
    rule's *other* bound rather than asking whether the threshold is a number.

    And ``json.dumps`` writes them as the bare tokens ``NaN`` and ``Infinity``,
    which are not JSON. ``/api/sweep?values=nan`` returned a body that
    ``JSON.parse`` rejects, so the dashboard panel died on a parse error with
    nothing on screen to say why. :mod:`ptm.ingest` already refuses non-finite
    numbers on the way in, for the same reason; this is the other door.
    """
    out: list[float] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            number = float(chunk)
            if not math.isfinite(number):
                raise ValueError(
                    f"{chunk!r} is not a threshold. A rule compares a field against a "
                    f"number, and no value is less than nan or greater than inf, so "
                    f"this would report the decision base for a rule that never fires.")
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
    "           occurrence of the field in the version.\n"
    "\n"
    "options:\n"
    "  --edge lower|upper   move one end of a band and leave the other alone. A rule "
    "like\n"
    "                       '40 < amount <= 100' states two thresholds, and without "
    "this the\n"
    "                       sweep refuses it rather than rewriting both ends to the "
    "same number.\n"
    "                       On a grid, name the end on the axis instead: "
    "'2.1:amount@lower=30,45'.\n"
    "  --json               write the whole result to stdout as JSON and the prose to\n"
    "                       stderr. This is the one measurement here whose output is a\n"
    "                       shape rather than a sentence, and a column of numbers meant\n"
    "                       to be plotted had to be scraped back out of a fixed-width\n"
    "                       table. The warnings travel as fields, so a reader parsing\n"
    "                       stdout cannot lose the caveat the numbers came with."
)


def parse_axis(raw: str) -> dict:
    """``1.1:amount=25,50,75`` - or ``amount=25,50`` for every clause using it.

    ``1.1:amount@lower=25,50`` moves one end of a band, the same thing
    ``--edge`` does for a single sweep. It is spelled on the axis rather than as
    a flag because a grid has two of them and they need not be the same end.
    """
    if "=" not in raw:
        raise ValueError(f"axis {raw!r} needs the form [clause:]field[@edge]=v1,v2,...")
    head, values = raw.split("=", 1)
    head, _, edge = head.partition("@")
    clause, _, field = head.rpartition(":")
    if not field:
        raise ValueError(f"axis {raw!r} names no field")
    if edge and edge not in EDGES:
        raise ValueError(f"axis {raw!r} names edge {edge!r}; it must be one of {list(EDGES)}")
    return {"clause": clause, "field": field, "edge": edge,
            "values": parse_values(values)}


def _print_joint(result: dict, out=None) -> None:
    """The grid as prose.

    ``out`` is where every line of it goes, and it exists because of
    ``--json``: with that flag stdout carries nothing but the document, so the
    prose has to be redirectable in one place rather than at each of the dozen
    calls below.
    """
    say = functools.partial(print, file=sys.stdout if out is None else out)
    unit = result["impact_unit"]
    first, second = result["first"], result["second"]
    say(f"sweeping {first['field']} x {second['field']} in {result['domain']}/"
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
    say(f"{label:<{width}}" + "".join(f"{v:>12}" for v in seconds))
    say(f"{subhead:<{width}}" + "".join(f"{'flips':>12}" for _ in seconds))
    for a in dict.fromkeys(p["first_value"] for p in result["points"]):
        cells = []
        for b in seconds:
            point = by_pair.get((a, b))
            mark = "*" if point and point["is_current"] else " "
            cells.append(f"{point['flips'] if point else '-':>11}{mark}")
        say(f"{a:<{width}}" + "".join(cells))
    say(f"  (* = the settings in force. net {unit} and the policy-driven split are "
        f"in the JSON form of this result - add --json.)")
    for side in ("first_inert", "second_inert"):
        reading = result.get(side) or {}
        if reading.get("measured") and reading.get("inert"):
            say(f"\n  WARNING {reading['note']}")
    interaction = result["interaction"]
    if interaction.get("measured"):
        say(f"\ninteraction: moving {first['field']} changes "
            f"{interaction['effect_min_flips']}-{interaction['effect_max_flips']} "
            f"decisions depending on where {second['field']} sits "
            f"({interaction['interaction_flips']} apart)")
        say("  " + ("the two dials are independent here, so two single sweeps would "
                    "have told you the same thing"
                    if interaction["independent"] else
                    "the dials interact: the best setting for one depends on the "
                    "other, which is what a pair of single sweeps cannot show"))


def _warn_about_the_rules(domain_name: str, version: str, out=None) -> list[str]:
    """Say what the curve above is worth, next to the curve above.

    The sweep is arithmetic over ``offline_rules``, so it is only ever as good
    as the rules' agreement with the judge. Printing the caveat somewhere else
    means it is read by somebody other than the person acting on the numbers.

    Returns the sentences as well as printing them, which is what lets
    ``--json`` carry them as a field. A caveat that exists only in the prose
    stream is a caveat a reader who parsed stdout has already lost, and this is
    the one this module would least like to be dropped.
    """
    say = functools.partial(print, file=sys.stdout if out is None else out)
    from . import report as report_module

    try:
        result = report_module.rule_agreement(domain_name, version)
    except LookupError:
        return []
    if result.get("inert"):
        note = ("these curves are computed from the offline rules, and the verdicts "
                "they were last scored against came from those same rules - so nothing "
                "here has been checked against a real judge")
        say(f"\n  {note}")
        return [note]
    if not result.get("compared"):
        note = ("these curves are computed from the offline rules, which have never "
                "been scored against a judge - run the replay, then python -m ptm.rules")
        say(f"\n  {note}")
        return [note]
    notes = [f"computed from offline rules agreeing with the judge on "
             f"{result['rate']:.1%} of {result['compared']} case(s), citing the same "
             f"clause {result['clause_agreement']:.1%} of the time"]
    say(f"\n  {notes[0]}")
    from .config import load_domain as _load

    for problem in report_module.rules_engine.gate(result, _load(domain_name)):
        say(f"  WARNING {problem}")
        notes.append(problem)
    return notes


def _take_edge(args: list[str]) -> tuple[list[str], str]:
    """Pull ``--edge lower`` (or ``--edge=lower``) out of argv, with its value.

    Returns the remaining arguments and the end named, which is ``""`` when the
    flag was not given. Raising rather than defaulting on a bad value: ``--edge
    higher`` is somebody reaching for the feature and missing, and silently
    sweeping both ends is the exact failure the flag exists to prevent.
    """
    out: list[str] = []
    edge = ""
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--edge":
            if not rest:
                raise ValueError("--edge needs a value: lower or upper")
            edge = rest.pop(0)
        elif arg.startswith("--edge="):
            edge = arg.split("=", 1)[1]
        else:
            out.append(arg)
            continue
        if edge not in EDGES:
            raise ValueError(f"--edge must be lower or upper, not {edge!r}")
    return out, edge


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
    as_json = "--json" in args
    # Where the prose goes. With --json stdout carries nothing but the
    # document, exactly as in ptm.gate, so the reader who wanted `| jq` and the
    # reader watching the run both get what they came for.
    prose = sys.stderr if as_json else sys.stdout
    say = functools.partial(print, file=prose)
    # --edge takes a value, so it cannot simply be filtered out of the
    # positional list the way --joint is: doing that would leave 'lower'
    # sitting where the sweep expects a field name. Taken out with its
    # argument, before anything else reads argv.
    try:
        args, edge = _take_edge(args)
    except ValueError as exc:
        print(f"ERROR {exc}\n\n{USAGE}", file=sys.stderr)
        return 2
    positional = [a for a in args if not _is_option(a)]
    unknown = [a for a in args if _is_option(a) and a not in ("--joint", "--json")]
    if unknown:
        print(f"ERROR unknown option {unknown[0]!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    if edge and joint_mode:
        print("ERROR a grid names its ends on the axes, not with --edge: "
              "'2.1:amount@lower=30,45'. The two axes need not use the same end, "
              f"which one flag could not say.\n\n{USAGE}", file=sys.stderr)
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
    def refuse(message: str, usage: bool = False) -> int:
        """A refusal, in whichever form the caller asked the result in.

        With ``--json`` a consumer parses stdout and nothing else, so an empty
        pipe would be the only difference between "could not be run" and
        "crashed". The same argument ``ptm.gate`` makes, and the same shape of
        document, so one reader handles both.

        ``usage`` is for the refusals that are about the command line rather
        than about the policy - a misspelled setting is somebody who wants the
        usage string, and an unknown version is somebody who does not.
        """
        if as_json:
            json.dump({"error": message, "domain": domain_name, "version": version,
                       "code": 2, "ran": False}, sys.stdout, indent=2)
            print()
        tail = f"\n\n{USAGE}" if usage else ""
        print(f"ERROR {message}{tail}", file=sys.stderr)
        return 2

    try:
        domain = load_domain(domain_name)
    except FileNotFoundError as exc:
        return refuse(str(exc))
    if version not in domain.policies:
        return refuse(f"unknown policy version {version!r} for {domain_name}; have "
                      f"{sorted(domain.policies)}")

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
            grid = joint(domain_name, version, first, second)
        except LookupError as exc:
            return refuse(str(exc))
        _print_joint(grid, out=prose)
        notes = _warn_about_the_rules(domain_name, version, out=prose)
        if as_json:
            json.dump({**grid, "ran": True, "code": 0, "rule_agreement_notes": notes},
                      sys.stdout, indent=2, default=str)
            print()
        return 0
    if len(positional) == 2:
        say(f"numeric dials in {domain_name}/{version}:")
        found = thresholds(domain, version)
        for t in found:
            note = ""
            if t["collapses"]:
                # Listed with the route rather than only the refusal. "Not
                # sweepable" was true of the whole-rule rewrite and never true
                # of the band itself, which is two thresholds and has each of
                # them movable on its own.
                ends = t["edges"]
                note = (f"  [a band, compared against {t['values']}: sweep one end with "
                        f"--edge {' or --edge '.join(ends)}]" if ends else
                        f"  [not sweepable: compared against {t['values']}, and no single "
                        f"end of it can be told from the others]")
            say(f"  clause {t['clause'] or '-':<6} {t['field']:<24} = {t['value']:<10} "
                f"-> {t['outcome']}{note}")
        if not found:
            # An empty list is a finding about the policy, and it is the one the
            # old unknown-version path produced by accident. Said out loud now
            # that it can only mean what it says.
            say("  none: this version's offline rules compare no field against a "
                "number, so there is no dial to sweep")
        if as_json:
            json.dump({"domain": domain_name, "version": version, "dials": found,
                       "ran": True, "code": 0}, sys.stdout, indent=2, default=str)
            print()
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
        return refuse(f"the settings to sweep must be comma-separated numbers: {exc}",
                      usage=True)
    if not values:
        return refuse("a sweep needs at least one setting to try", usage=True)
    try:
        result = sweep(domain_name, version, field, values, clause=clause, edge=edge)
    except LookupError as exc:
        return refuse(str(exc))
    unit = result["impact_unit"]
    where = f"clause {clause} " if clause else ""
    end = f"the {edge} end of " if edge else ""
    say(f"sweeping {end}{where}{field} in {domain_name}/{version} over "
        f"{result['cases']} cases (baseline {result['baseline_version'] or 'none'})")
    header = f"{field:>14}{'flips':>8}{'rate':>8}{'loosen':>8}{'tighten':>9}"
    say(header + f"{'net ' + unit:>13}{'policy-driven':>15}")
    for p in result["points"]:
        mark = "  <- current" if p["is_current"] else ""
        # The marker goes on the row rather than only in the note below it: a
        # reader scanning the column for a round number has to meet it there.
        if p.get("empties_rule"):
            mark = f"{mark}  [clause matches nothing at this setting]"
        say(f"{p['value']:>14}{p['flips']:>8}{p['flip_rate']:>7.1%}{p['loosening']:>8}"
            f"{p['tightening']:>9}{p['net_impact']:>13,.0f}"
            f"{p['policy_driven_flips']:>15}{mark}")
    # Before the inert reading, which would otherwise describe a flat run of
    # rows as an insensitive threshold when what it actually is is a clause
    # that stopped applying.
    warnings: list[str] = []
    if result.get("emptied"):
        warnings.append(describe_emptied(result["emptied"], field, edge))
        say(f"\n  WARNING {warnings[-1]}")
    # Ahead of the caveat about the rules, because it is the stronger statement:
    # a flat curve is not a curve anybody should be picking a round number off.
    inert = result.get("inert") or {}
    if inert.get("measured") and inert.get("inert"):
        warnings.append(inert["note"])
        say(f"\n  WARNING {warnings[-1]}")
    # After the table, not before it: the caveat is about the numbers a reader
    # has just seen, and above them it is read as preamble and skipped.
    notes = _warn_about_the_rules(domain_name, version, out=prose)
    if as_json:
        # The warnings travel *in* the document rather than only beside it.
        # Every one of them is a reason not to read a row of this curve at face
        # value, and a consumer that took stdout and dropped stderr would have
        # the numbers and none of the reasons.
        json.dump({**result, "ran": True, "code": 0, "warnings": warnings,
                   "rule_agreement_notes": notes}, sys.stdout, indent=2, default=str)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
