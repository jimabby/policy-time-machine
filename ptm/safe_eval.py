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


def evaluate(expression: str, scope: dict[str, Any]) -> Any:
    """Compute an expression against ``scope``. Never runs arbitrary Python."""
    return _eval(parse(expression).body, scope)


# ------------------------------------------------------------------ the walker

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
        if isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant):
            # Only the literal case is knowable here; the evaluator enforces the
            # same ceiling on a computed exponent. Reporting the literal one
            # statically is what lets a rule be rejected on arrival rather than
            # the first time it fires on a worker.
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
        left, right = _eval(node.left, scope), _eval(node.right, scope)
        if isinstance(node.op, ast.Pow):
            # Bounded, not refused: a rule may legitimately square something,
            # and an unbounded exponent is a worker hang rather than a wrong
            # answer - the failure mode a mapped fan-out handles worst.
            if not isinstance(right, (int, float)) or abs(right) > MAX_EXPONENT:
                _fail(f"raises to the power of {right!r}; the ceiling is {MAX_EXPONENT}")
            return left ** right
        operator = _BIN_OPS.get(type(node.op))
        if operator is None:
            _fail(f"uses an unsupported operator {type(node.op).__name__}")
        return operator(left, right)
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
        return SAFE_BUILTINS[node.func.id](*[_eval(a, scope) for a in node.args])
    for kind, what in _REFUSALS.items():
        if isinstance(node, kind):
            _fail(f"uses {what}, which rule expressions may not do")
    _fail(f"uses {type(node).__name__}, which rule expressions may not do")
