"""Static guards on the dashboard page.

There is no browser here, so these check the properties that were actually
wrong rather than trying to render anything: every value interpolated into HTML
goes through an escaper, and the page pulls in nothing from the network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "plugins" / "dashboard.html"
HTML = PAGE.read_text()
SCRIPT = re.search(r"<script>\n(.*)</script>", HTML, re.S).group(1)
STYLE = re.search(r"<style>\n(.*?)</style>", HTML, re.S).group(1)

#: Values that arrive from SQLite or from model output and land inside markup.
#: Every one of these was interpolated raw before, which is the bug being pinned.
UNTRUSTED = [
    "r.case_id", "r.actual_outcome", "r.new_outcome", "r.direction", "r.precedent",
    "r.rationale", "r.actual_rationale", "r.policy_clause", "r.correct_outcome",
    "r.ruled_by", "r.note", "r.month", "b.headline", "b.summary", "x.name",
    "x.explanation", "x.direction", "x.clause", "e.clause", "e.proposed_text",
    "e.current_text", "e.reason", "a.rationale", "a.residual_risk",
]


def interpolations(field: str) -> list[str]:
    """Every `${...}` expression in the page that reads ``field``."""
    return [m for m in re.findall(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}", HTML)
            if re.search(r"\b" + re.escape(field) + r"\b", m)]


@pytest.mark.parametrize("field", UNTRUSTED)
def test_untrusted_values_are_escaped_wherever_they_reach_markup(field):
    """Regression: case_id, outcomes, direction and precedent were interpolated raw."""
    found = interpolations(field)
    assert found, f"{field} is no longer interpolated - drop it from UNTRUSTED"
    for expr in found:
        assert "esc(" in expr or "money(" in expr or "n(" in expr, \
            f"unescaped {field} in ${{{expr}}}"


def test_case_payloads_are_escaped():
    """Payload keys and values are the least trusted data on the page."""
    assert "esc(caseText(r.payload))" in HTML


def test_the_escaper_covers_quotes_and_angle_brackets():
    """Attribute contexts need quotes escaped, not just < and >."""
    fn = re.search(r"function esc\(s\)\{(.+?)\n\}", HTML, re.S).group(1)
    for entity in ["&amp;", "&lt;", "&gt;", "&quot;", "&#39;"]:
        assert entity in fn, f"esc() does not produce {entity}"
    assert "??" in fn, "null and undefined must render empty, not as the word null"


def test_the_page_loads_nothing_from_the_network():
    """It is served inside Airflow, possibly offline. No CDNs, no fonts."""
    for pattern in [r"<script[^>]+src=", r"<link[^>]+href=", r"@import", r"//cdn", r"https?://"]:
        assert not re.search(pattern, HTML), f"external resource matched {pattern!r}"


def test_every_api_call_targets_a_real_plugin_route():
    """A typo'd path is a silently empty panel, so pin the two lists together."""
    plugin = (PAGE.parent / "policy_time_machine_plugin.py").read_text()
    routes = {r.rstrip("/") for r in re.findall(r'@app\.get\("([^"]+)"', plugin)}
    for call in re.findall(r"api\(`?(/api/[^`')]+)", HTML):
        # Strip the template placeholders back to the route's parameter shape.
        shape = re.sub(r"\$\{[^}]+\}", "{p}", call).rstrip("/")
        pattern = re.sub(r"\{p\}", "{[a-z_]+}", re.escape(shape).replace(r"\{p\}", "{p}"))
        assert any(re.fullmatch(re.sub(r"\{[a-z_]+\}", "{p}", r), shape) for r in routes) or \
               any(re.fullmatch(pattern, r) for r in routes), f"no route for {call}"


def test_the_page_is_responsive_and_theme_aware():
    assert 'name="viewport"' in HTML, "needed for any sensible mobile rendering"
    assert "prefers-color-scheme:dark" in HTML, "it sits inside either Airflow theme"
    assert "max-width:700px" in HTML, "the wide flips table needs a narrow-screen form"
    assert "overflow-x:auto" in HTML, "wide content must scroll itself, not the body"


def test_no_css_has_leaked_into_the_script():
    """Regression: a section comment appears in both blocks, so an edit keyed on
    it inserted a stylesheet into <script> and the whole page stopped running.

    A CSS rule at the start of a line inside the script is never valid JS.
    """
    for i, line in enumerate(SCRIPT.splitlines(), start=1):
        assert not re.match(r"^[.#@][\w-]+[\s{,]", line), \
            f"CSS rule at script line {i}: {line[:60]!r}"


def test_no_script_has_leaked_into_the_stylesheet():
    for i, line in enumerate(STYLE.splitlines(), start=1):
        assert not re.match(r"^\s*(function|const|let|var)\s", line), \
            f"JS at stylesheet line {i}: {line[:60]!r}"


def test_the_script_parses_as_javascript():
    """Uses node when it is available; skipped rather than faked when it is not."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(SCRIPT)
        path = fh.name
    try:
        done = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    finally:
        os.unlink(path)
