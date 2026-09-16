"""Evaluate an ``offline_rules`` condition without handing over the interpreter.

``offline_rules`` used to be YAML a person wrote, and ``eval`` with a trimmed
``__builtins__`` was a defensible shortcut for that. :mod:`ptm.proposal` changed
the threat model: the ``propose_<domain>`` DAG asks a **model** to write rules,
writes them to ``include/drafts/<domain>/<version>.rules.yaml``, and every later
replay, sweep and gate evaluates them on a worker. The text being evaluated is
now generated, and a name-level check in front of ``eval`` does not contain it:

    ().__class__.__base__.__subclasses__()[-1].__init__.__globals__[...]

reads no bare names at all, so a check built on :class:`ast.Name` nodes reports
nothing and the expression then runs with the interpreter's full reach.

So this does not call ``eval``. It walks the parsed expression and computes the
result node by node, and any construct it does not explicitly implement is a
:class:`RuleError` rather than a fallthrough. Attribute access, subscripting,
lambdas, comprehensions and f-strings are not implemented, which is what removes
the object graph the escape above walks. :func:`check` is the same whitelist run
without evaluating, so :mod:`ptm.lint` and :func:`ptm.rules.validate` reject a
rule at the point it arrives rather than the first time it fires.

The language that remains is the one the rules actually use: comparisons,
boolean and arithmetic operators, ``in``, a conditional expression, literals,
and calls to the helpers in :data:`SAFE_BUILTINS`.

**It bounds cost as well as reach.** A whitelist that cannot be escaped can
still be made to run forever, and a rule evaluates on a worker holding a mapped
task slot. :data:`MAX_EXPONENT` caps one ``**``; :data:`MAX_RESULT_SIZE` caps
what an expression may *build*, which is the bound that survives nesting and
covers ``'x' * 10**9`` as well.
"""

from __future__ import annotations

import ast
import builtins
from typing import Any

#: Helpers a rule may call. Callable by bare name only - there is no attribute
#: access in this language, so there is nothing to reach them through.
SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(builtins, name)
    for name in ("abs", "len", "min", "max", "float", "int", "str", "round", "sum")
}

#: Every name an expression may use without it being a payload field. Exported
#: for the lint, which must not report a helper call as an unknown field.
SAFE_NAMES: frozenset[str] = frozenset(SAFE_BUILTINS)

#: Ceiling on ``**``. A rule is a threshold comparison; ``9**9**9`` is not one,
#: and evaluating it would hang a worker holding a mapped task slot.
MAX_EXPONENT = 64

#: Ceiling on how large a value an expression may build - bits for an integer,
#: elements for a string, list or tuple.
#:
#: :data:`MAX_EXPONENT` on its own does not bound this, which is the gap this
#: closes. Every exponent in ``(((10**64)**64)**64)**64`` is a perfectly legal
#: 64 while the *base* grows at each step, and that expression takes fifteen
#: seconds; one nesting further is exactly the worker hang the exponent ceiling
#: was written to prevent. ``'x' * 10**9`` is the same failure with no ``**`` in
#: it at all. So the bound is on the size of the result rather than on any one
#: operator, and it is checked before the expensive operators are applied
#: rather than after.
#:
#: A megabit is four orders of magnitude above anything a threshold comparison
#: needs and still costs milliseconds to reach, so it refuses only expressions
#: that were never rules.
MAX_RESULT_SIZE = 1 << 20


class RuleError(ValueError):
    """An expression this evaluator refuses to run, with the reason."""


_BOOL_OPS = {ast.And, ast.Or}
_UNARY_OPS = {
    ast.Not: lambda v: not v,
    ast.USub: lambda v: -v,
    ast.UAdd: lambda v: +v,
}
_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}
_COMPARE_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

#: What a node type is called when refusing it, so the message names the
#: construct a rule author would recognise rather than an AST class.
_REFUSALS = {
    ast.Attribute: "attribute access",
    ast.Subscript: "subscripting",
    ast.Lambda: "a lambda",
    ast.ListComp: "a comprehension",
    ast.SetComp: "a comprehension",
    ast.DictComp: "a comprehension",
    ast.GeneratorExp: "a generator expression",
    ast.JoinedStr: "an f-string",
    ast.FormattedValue: "an f-string",
    ast.Starred: "argument unpacking",
    ast.NamedExpr: "an assignment expression",
    ast.Await: "await",
    ast.Dict: "a dict literal",
    ast.Slice: "a slice",
}


def parse(expression: str) -> ast.Expression:
    """Parse one condition, or raise :class:`RuleError`."""
    if not isinstance(expression, str) or not expression.strip():
        return _fail("is empty")
    try:
        return ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return _fail(f"does not parse: {exc}")


def _fail(message: str) -> Any:
    raise RuleError(message)


def names_in(expression: str) -> set[str]:
    """Payload fields an expression reads, ignoring the helper functions.

    Raises :class:`RuleError` on anything outside the supported language, so a
    caller asking "which fields does this read" cannot get a confident answer
    about an expression that would be refused at evaluation time.
    """
    tree = parse(expression)
    _walk(tree.body)
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id not in SAFE_NAMES}


def check_expression(expression: str, known: set[str] | frozenset[str] | None = None) -> list[str]:
    """Problems with an expression, as sentences. Empty means it is evaluable.

    ``known`` is the set of payload fields available; when given, a name outside
    it is reported. A rule reading a field that does not exist never matches,
    and never matching is silent - which is the failure :mod:`ptm.lint` exists
    for and the one a generated rule produces most often.
    """
    try:
        used = names_in(expression)
    except RuleError as exc:
        return [str(exc)]
    if known is None:
        return []
    unknown = sorted(used - set(known))
    if unknown:
        return [f"reads unknown field(s) {unknown}; the rule would never match"]
    return []


# ------------------------------------------------- does one rule shadow another
# ``offline_rules`` are tried in order and the first match decides the case, so a
# rule whose condition is implied by an earlier one can never fire. It parses, it
# cites a real clause, it reads only real fields - every check in this project
# passes it - and it contributes nothing except an outcome that is never produced
# and a clause that is never cited. That is the same silent wrongness a rule
# reading a misspelled field has, from a source that is now far more prolific:
# ``propose_<domain>`` has a model write these.
#
# The test is deliberately one-directional and sound rather than complete. It
# reports a shadow only where it can *prove* one, so it never cries wolf on a
# rule set somebody has thought about; it will happily miss a rule made
# unreachable by two earlier rules between them, which is a harder question than
# this needs to answer.


def _conjuncts(expression: str) -> list[ast.AST]:
    """The top-level ``and`` terms of an expression, flattened."""
    out: list[ast.AST] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            for value in node.values:
                walk(value)
        else:
            out.append(node)

    walk(parse(expression).body)
    return out


#: A comparison read from the other side reads as its mirror: ``75 < amount`` is
#: ``amount > 75``, and a check that did not know that would miss half the rules
#: anybody writes.
_MIRROR = {"Lt": "Gt", "Gt": "Lt", "LtE": "GtE", "GtE": "LtE", "Eq": "Eq", "NotEq": "NotEq"}


def _term(node: ast.AST) -> tuple | None:
    """``(field, op, value)`` for a one-sided comparison against a literal, else None."""
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    op = type(node.ops[0]).__name__
    left, right = node.left, node.comparators[0]
    if isinstance(left, ast.Name) and isinstance(right, ast.Constant):
        return (left.id, op, right.value)
    if isinstance(right, ast.Name) and isinstance(left, ast.Constant) and op in _MIRROR:
        return (right.id, _MIRROR[op], left.value)
    return None


def _interval(op: str, value: Any) -> tuple | None:
    """A numeric comparison as ``(lo, lo_closed, hi, hi_closed)``, else None.

    Only numbers. ``category == 'travel'`` is a point on an unordered set and
    implication between two of those is equality, which the exact-match path
    above already covers.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    inf = float("inf")
    return {
        "Gt": (value, False, inf, False),
        "GtE": (value, True, inf, False),
        "Lt": (-inf, False, value, False),
        "LtE": (-inf, False, value, True),
        "Eq": (value, True, value, True),
    }.get(op)


def _within(inner: tuple, outer: tuple) -> bool:
    """Whether every value satisfying ``inner`` also satisfies ``outer``."""
    i_lo, i_lo_closed, i_hi, i_hi_closed = inner
    o_lo, o_lo_closed, o_hi, o_hi_closed = outer
    low_ok = i_lo > o_lo or (i_lo == o_lo and (o_lo_closed or not i_lo_closed))
    high_ok = i_hi < o_hi or (i_hi == o_hi and (o_hi_closed or not i_hi_closed))
    return low_ok and high_ok


def implies(narrow: str, wide: str) -> bool:
    """Whether every case matching ``narrow`` also matches ``wide``.

    Proved term by term: ``wide`` holds whenever each of its ``and`` terms is
    guaranteed by one of ``narrow``'s. A term ``narrow`` does not state at all
    cannot be guaranteed, so ``amount > 100`` implies ``amount > 75`` and
    neither implies ``amount > 75 and receipt == 'no'``.

    Unparsable input is not an implication. It is reported elsewhere, by
    :func:`check_expression`, and guessing here would turn a syntax error into a
    claim about reachability.
    """
    try:
        narrow_terms = _conjuncts(narrow)
        wide_terms = _conjuncts(wide)
    except RuleError:
        return False
    narrow_text = {ast.unparse(t) for t in narrow_terms}
    narrow_parsed = [_term(t) for t in narrow_terms]
    for term in wide_terms:
        if ast.unparse(term) in narrow_text:
            continue
        wanted = _term(term)
        if wanted is None:
            return False
        outer = _interval(wanted[1], wanted[2])
        if outer is None:
            return False
        if not any(have is not None and have[0] == wanted[0]
                   and (inner := _interval(have[1], have[2])) is not None
                   and _within(inner, outer)
                   for have in narrow_parsed):
            return False
    return True


def shadowed(rules: list[dict]) -> list[dict]:
    """Rules an earlier rule in the list makes unreachable.

    ``rules`` are ``offline_rules`` entries. Each finding names the dead rule,
    the rule that eats it, and whether the two would have given the same answer
    - which is the difference between a duplicate somebody can delete and a
    replay that is quietly producing the wrong outcome for every case the dead
    rule was written to catch.
    """
    found: list[dict] = []
    for index, rule in enumerate(rules):
        condition = (rule.get("when") or "").strip()
        if not condition:
            continue
        for earlier_index, earlier in enumerate(rules[:index]):
            before = (earlier.get("when") or "").strip()
            if not before or not implies(condition, before):
                continue
            same = (rule.get("outcome") == earlier.get("outcome")
                    and str(rule.get("clause", "")) == str(earlier.get("clause", "")))
            found.append({
                "rule_index": index,
                "clause": str(rule.get("clause", "")),
                "outcome": rule.get("outcome", ""),
                "expression": condition,
                "shadowed_by": earlier_index,
                "shadowed_by_clause": str(earlier.get("clause", "")),
                "shadowed_by_outcome": earlier.get("outcome", ""),
                "shadowed_by_expression": before,
                # Same answer from the same clause is dead weight. A different
                # answer, or the same answer credited to a different sentence, is
                # a replay reporting something that is not true.
                "harmless": same,
            })
            break  # one proof is enough; the rule is dead either way
    return found


def describe_shadowed(found: list[dict], where: str = "") -> str:
    """Shadowed rules as the lint and the proposal DAG report them."""
    if not found:
        return f"no rule in {where or 'this set'} is unreachable"
    lines = [f"{len(found)} rule(s) in {where or 'this set'} can never fire - an earlier "
             f"rule matches every case they would:"]
    for row in found:
        lines.append(
            f"  rule[{row['rule_index']}] (clause {row['clause'] or '-'} -> "
            f"{row['outcome']}) is shadowed by rule[{row['shadowed_by']}] "
            f"(clause {row['shadowed_by_clause'] or '-'} -> {row['shadowed_by_outcome']}): "
            f"{row['expression']!r} implies {row['shadowed_by_expression']!r}")
        if not row["harmless"]:
            lines.append(f"      every case rule[{row['rule_index']}] was written for is "
                         f"decided {row['shadowed_by_outcome']!r} by clause "
                         f"{row['shadowed_by_clause'] or '-'} instead. Move it earlier in "
                         f"the list, or narrow the rule above it.")
    return "\n".join(lines)


def evaluate(expression: str, scope: dict[str, Any]) -> Any:
    """Compute an expression against ``scope``. Never runs arbitrary Python."""
    return _eval(parse(expression).body, scope)


# ------------------------------------------------------------------ the walker

def _refuse_chained_power(node: ast.BinOp) -> None:
    """Refuse ``(b ** m) ** n`` - the escalation the exponent ceiling cannot see.

    Every exponent in ``((b**64)**64)**64`` is a legal 64 while the *base* grows
    at each step, so a check on the exponents alone reports nothing and the
    expression runs for as long as it likes. Chained exponentiation is never a
    threshold comparison, so refusing the shape is both sound and something a
    rule author can act on - unlike a size ceiling tripped at judging time.

    Applied by :func:`_walk` and by :func:`_eval` alike, so an expression cannot
    be refused by the lint and then quietly evaluated by a worker.
    """
    if isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Pow):
        _fail("raises a power to a power, which rule expressions may not do: each "
              "exponent stays under the ceiling while the base grows, so the result is "
              "unbounded. Write the exponent out as one number.")


def _walk(node: ast.AST) -> None:
    """Refuse anything outside the supported language, without evaluating."""
    for kind, what in _REFUSALS.items():
        if isinstance(node, kind):
            _fail(f"uses {what}, which rule expressions may not do")
    if isinstance(node, ast.Name):
        return
    if isinstance(node, ast.Constant):
        return
    if isinstance(node, ast.BoolOp):
        if type(node.op) not in _BOOL_OPS:
            _fail(f"uses an unsupported boolean operator {type(node.op).__name__}")
        for value in node.values:
            _walk(value)
        return
    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            _fail(f"uses an unsupported unary operator {type(node.op).__name__}")
        _walk(node.operand)
        return
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS and not isinstance(node.op, ast.Pow):
            _fail(f"uses an unsupported operator {type(node.op).__name__}")
        if isinstance(node.op, ast.Pow):
            _refuse_chained_power(node)
            if isinstance(node.right, ast.Constant):
                # Only the literal case is knowable here; the evaluator enforces
                # the same ceiling on a computed exponent. Reporting the literal
                # one statically is what lets a rule be rejected on arrival
                # rather than the first time it fires on a worker.
                exponent = node.right.value
                if not isinstance(exponent, (int, float)) or abs(exponent) > MAX_EXPONENT:
                    _fail(f"raises to the power of {exponent!r}; the ceiling is {MAX_EXPONENT}")
        _walk(node.left)
        _walk(node.right)
        return
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _COMPARE_OPS:
                _fail(f"uses an unsupported comparison {type(op).__name__}")
        _walk(node.left)
        for operand in node.comparators:
            _walk(operand)
        return
    if isinstance(node, ast.IfExp):
        _walk(node.test)
        _walk(node.body)
        _walk(node.orelse)
        return
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for element in node.elts:
            _walk(element)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            _fail("calls something other than a named helper")
        if node.func.id not in SAFE_BUILTINS:
            _fail(f"calls {node.func.id!r}, which is not one of the helpers "
                  f"{sorted(SAFE_BUILTINS)}")
        if node.keywords:
            _fail("passes keyword arguments to a helper, which is not supported")
        for arg in node.args:
            _walk(arg)
        return
    _fail(f"uses {type(node).__name__}, which rule expressions may not do")


#: Types whose size is a length rather than a magnitude, and which therefore
#: blow up through repetition rather than through arithmetic.
_SEQUENCES = (str, bytes, list, tuple, set, frozenset)


def _size_of(value: Any) -> int:
    """How big a value is, in the unit its own type grows in.

    Bits for an integer and elements for a sequence, because those are the two
    things an expression here can make arbitrarily large. Floats overflow to an
    exception on their own and everything else is a scalar, so both are zero.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value.bit_length()
    if isinstance(value, _SEQUENCES):
        return len(value)
    return 0


def _power_size(base: Any, exponent: Any) -> int:
    """Bits in ``base ** exponent``, without computing it."""
    if not isinstance(base, int) or isinstance(base, bool):
        return 0
    if not isinstance(exponent, int) or isinstance(exponent, bool) or exponent <= 0:
        return 0
    return max(abs(base).bit_length(), 1) * exponent


def _repeat_size(left: Any, right: Any) -> int:
    """Elements in ``left * right`` when it is sequence repetition, else 0."""
    for sequence, count in ((left, right), (right, left)):
        if isinstance(sequence, _SEQUENCES) and isinstance(count, int) \
                and not isinstance(count, bool):
            return len(sequence) * max(count, 0)
    return 0


def _guard(size: int, what: str, unit: str) -> None:
    """Refuse a value past :data:`MAX_RESULT_SIZE`, naming what it was building."""
    if size > MAX_RESULT_SIZE:
        _fail(f"builds {what} of {size:,} {unit}, past the ceiling of "
              f"{MAX_RESULT_SIZE:,}. A rule is a threshold comparison; evaluating this "
              f"would hang a worker holding a mapped task slot.")


def _eval(node: ast.AST, scope: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        if node.id in SAFE_BUILTINS:
            return SAFE_BUILTINS[node.id]
        _fail(f"reads {node.id!r}, which this case does not have")
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = _eval(value, scope)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = _eval(value, scope)
            if result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):
        operator = _UNARY_OPS.get(type(node.op))
        if operator is None:
            _fail(f"uses an unsupported unary operator {type(node.op).__name__}")
        return operator(_eval(node.operand, scope))
    if isinstance(node, ast.BinOp):
        # Before the operands are computed, not after: the whole point of
        # refusing the shape is that evaluating it is the expensive part.
        if isinstance(node.op, ast.Pow):
            _refuse_chained_power(node)
        left, right = _eval(node.left, scope), _eval(node.right, scope)
        if isinstance(node.op, ast.Pow):
            # Bounded, not refused: a rule may legitimately square something,
            # and an unbounded exponent is a worker hang rather than a wrong
            # answer - the failure mode a mapped fan-out handles worst.
            if not isinstance(right, (int, float)) or abs(right) > MAX_EXPONENT:
                _fail(f"raises to the power of {right!r}; the ceiling is {MAX_EXPONENT}")
            _guard(_power_size(left, right), "an integer", "bits")
            return left ** right
        operator = _BIN_OPS.get(type(node.op))
        if operator is None:
            _fail(f"uses an unsupported operator {type(node.op).__name__}")
        if isinstance(node.op, ast.Mult):
            # Checked before multiplying, not after: 'x' * 10**9 is a gigabyte
            # allocated by the time a check on the result could see it.
            _guard(_repeat_size(left, right), "a sequence", "elements")
        result = operator(left, right)
        _guard(_size_of(result), "a value", "units")
        return result
    if isinstance(node, ast.Compare):
        left = _eval(node.left, scope)
        for op, comparator in zip(node.ops, node.comparators):
            operator = _COMPARE_OPS.get(type(op))
            if operator is None:
                _fail(f"uses an unsupported comparison {type(op).__name__}")
            right = _eval(comparator, scope)
            if not operator(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return (_eval(node.body, scope) if _eval(node.test, scope)
                else _eval(node.orelse, scope))
    if isinstance(node, ast.List):
        return [_eval(e, scope) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, scope) for e in node.elts)
    if isinstance(node, ast.Set):
        return {_eval(e, scope) for e in node.elts}
    if isinstance(node, ast.Call):
        # Re-checked here rather than trusted from _walk: evaluate() is reachable
        # without a prior check, and this is the node where getting it wrong
        # hands over the interpreter.
        if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_BUILTINS:
            _fail("calls something other than a named helper")
        if node.keywords:
            _fail("passes keyword arguments to a helper, which is not supported")
        arguments = [_eval(a, scope) for a in node.args]
        try:
            result = SAFE_BUILTINS[node.func.id](*arguments)
        except RuleError:
            raise
        except Exception as exc:
            # A helper refusing its own argument is still this evaluator
            # refusing the expression, and callers are promised RuleError.
            # ``int('1' * 100000)`` is the live example: CPython caps
            # integer-string conversion, so the helper raises ValueError from
            # inside a language this module claims to have bounded. Letting it
            # through means offline_verdict swallows it as "does not match"
            # while ptm.rules.validate and ptm.lint - which only ever ask
            # statically - report the rule as sound.
            _fail(f"calls {node.func.id}() with an argument it refuses: {exc}")
        # str() of a large integer is the one helper that can grow its argument
        # rather than shrink it, so the same ceiling applies on the way out.
        _guard(_size_of(result), "a value", "units")
        return result
    for kind, what in _REFUSALS.items():
        if isinstance(node, kind):
            _fail(f"uses {what}, which rule expressions may not do")
    _fail(f"uses {type(node).__name__}, which rule expressions may not do")
