"""A missing gate input must not be reported as an expected policy reversal."""

import platform
from types import SimpleNamespace

import pytest

import demo


@pytest.fixture(autouse=True)
def _platform_is_already_known():
    """Ask the platform its name before ``subprocess.run`` is replaced.

    The patch below stands in for ``demo.subprocess.run``, and ``demo.subprocess``
    is the real module - so for the duration of the test *every* caller of
    ``subprocess.run`` gets a stub that answers with a return code and nothing
    else. ``demo.main`` then prints a banner containing ``platform.system()``,
    which on Windows is not a constant: it shells out to ``ver`` through
    ``subprocess.check_output``, meets the stub, and dies unpacking a version
    string it never received.

    It only ever failed some of the time, which is the worst way for a test to
    be wrong. ``platform.uname()`` memoises, so whether this blew up depended on
    whether anything earlier in the session had already asked - true for the
    whole suite, false for this file on its own, and false for anybody bisecting
    a failure down to it. On Linux it never shelled out at all, so CI was always
    green.

    Warming the cache here rather than loosening the patch keeps the test
    asserting exactly what it asserted before: that ``demo.main`` maps a step's
    exit code to its own.
    """
    platform.uname()


@pytest.mark.parametrize(("code", "expected"), [(0, 0), (1, 0), (2, 1), (130, 1)])
def test_gate_only_accepts_success_or_a_policy_reversal(monkeypatch, code, expected):
    monkeypatch.setattr(demo, "steps", lambda *_: [
        ("Gate", "Check rulings", ["ptm.gate", "expenses", "v2"], True, False)])
    monkeypatch.setattr(demo, "find_python", lambda: demo.REPO / "python.exe")
    monkeypatch.setattr(demo.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=code))
    assert demo.main([]) == expected
