"""The engine runs on Python 3.10, and a newer interpreter will not say so.

`requirements-dev.txt` is pydantic, PyYAML and pytest - the engine is meant to
run anywhere, and CI pins the engine job to 3.10 to keep it honest. The problem
is that a developer on 3.12 or later gets no signal at all: the parser changed
in 3.12 (PEP 701) and accepts f-strings that 3.10 rejects outright, so a module
can pass every local test and then fail to *import* in CI.

That is not hypothetical - it is why this file exists. A table header written as

    f"{'(rows \\\\ columns)':<{width}}"

imported fine on 3.14 and was a SyntaxError on 3.10, taking nine test modules
with it.

`ast.parse(..., feature_version=(3, 10))` does not catch this: the flag gates a
few grammar features and not the f-string tokenizer. So the check is written
against the token stream instead, which is where the difference actually lives.
"""

from __future__ import annotations

import io
import pathlib
import sys
import tokenize

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

#: The oldest interpreter the engine claims to support. Keep in step with the
#: `python-version` of the engine job in .github/workflows/ci.yml.
MINIMUM = (3, 10)


def python_files() -> list[pathlib.Path]:
    return sorted(
        p for p in REPO.rglob("*.py")
        if ".venv" not in p.parts and "__pycache__" not in p.parts
        and ".git" not in p.parts)


def backslashes_in_fstring_expressions(source: str) -> list[tuple[int, str]]:
    """Lines where a backslash sits inside an f-string's ``{...}`` part.

    Legal from 3.12, a SyntaxError before it. A backslash in the *literal* half
    of an f-string has always been fine and is not reported - that is the whole
    reason this reads tokens rather than grepping for a character.
    """
    found: list[tuple[int, str]] = []
    depth = 0
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        name = tokenize.tok_name[token.type]
        if name == "FSTRING_START":
            depth += 1
        elif name == "FSTRING_END":
            depth = max(depth - 1, 0)
        elif depth and name != "FSTRING_MIDDLE" and "\\" in token.string:
            found.append((token.start[0], token.string))
    return found


class TestItStillRunsOnTheOldestSupportedPython:
    def test_the_file_list_is_not_empty(self):
        """A scan that silently found nothing to scan is the failure mode every
        check in this project is written to avoid."""
        assert len(python_files()) > 20

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="before 3.12 the interpreter rejects these itself, and emits no "
               "FSTRING_* tokens for this check to read")
    def test_no_f_string_carries_a_backslash_in_an_expression(self):
        offenders = {}
        for path in python_files():
            try:
                found = backslashes_in_fstring_expressions(
                    path.read_text(encoding="utf-8"))
            except tokenize.TokenError:  # pragma: no cover - unparseable source
                continue
            if found:
                offenders[str(path.relative_to(REPO))] = found
        assert not offenders, (
            f"these are a SyntaxError on Python {MINIMUM[0]}.{MINIMUM[1]}, so the module "
            f"will not import in CI even though it runs here: {offenders}. "
            f"Build the string outside the f-string.")

    def test_no_module_uses_syntax_newer_than_the_floor(self):
        """The coarse half of the same question.

        Deliberately paired with a note about its own limits: feature_version
        catches match statements and the like, and is blind to the f-string case
        above. Two checks because neither one subsumes the other.
        """
        import ast

        offenders = {}
        for path in python_files():
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                          feature_version=MINIMUM)
            except SyntaxError as exc:
                offenders[str(path.relative_to(REPO))] = f"line {exc.lineno}: {exc.msg}"
        assert not offenders, offenders
