"""A missing gate input must not be reported as an expected policy reversal."""

from types import SimpleNamespace

import pytest

import demo


@pytest.mark.parametrize(("code", "expected"), [(0, 0), (1, 0), (2, 1), (130, 1)])
def test_gate_only_accepts_success_or_a_policy_reversal(monkeypatch, code, expected):
    monkeypatch.setattr(demo, "steps", lambda *_: [
        ("Gate", "Check rulings", ["ptm.gate", "expenses", "v2"], True, False)])
    monkeypatch.setattr(demo, "find_python", lambda: demo.REPO / "python.exe")
    monkeypatch.setattr(demo.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=code))
    assert demo.main([]) == expected
