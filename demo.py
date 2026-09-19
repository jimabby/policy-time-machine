#!/usr/bin/env python3
"""Run the whole Policy Time Machine engine, end to end, with one command.

The Makefile is this project's task runner and it is the right one on macOS and
Linux. On Windows there is no ``make`` at all on a default box - Python, git and
Docker are there and it is not - which made the one thing a reader is told to
start with the one thing they could not run.

So this is the same tour, in Python, because Python is the one interpreter this
project already requires everywhere. One file rather than a ``.sh`` and a
``.ps1``, for the reason the rest of the codebase keeps making: two definitions
of one thing drift, and the half that drifts is the half nobody runs.

    python demo.py                 # Windows
    python3 demo.py                # macOS, Linux
    make tour                      # wherever make exists

No Airflow, no API key, no network: ``PTM_OFFLINE=1`` puts a deterministic rule
evaluator where the model goes, which is how CI runs it and how the demo is
rehearsed.

**Some steps exit non-zero on purpose.** ``ptm.gate expenses v2`` FAILS on the
shipped fixture, and that is the correct answer rather than a broken install:
policy v1 reverses the same two rulings, so the proposal introduces neither, and
the step after it proves that with ``--introduced-only``. A runner that called
that a failure would teach the wrong lesson on the first run anybody does, so
those steps are marked and reported as ``ok*``. Anything else exiting non-zero
fails the run.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent

#: Where a virtualenv keeps its interpreter, which is the one thing about this
#: that is genuinely per-platform. ``.venv`` is what the Makefile builds;
#: ``.venv-af`` is the heavier one with Airflow in it, which serves the engine
#: just as well. Neither is checked in, so a fresh clone needs --setup once.
VENV_BIN = "Scripts" if os.name == "nt" else "bin"
VENV_EXE = "python.exe" if os.name == "nt" else "python"
CANDIDATES = (".venv", ".venv-af")


def find_python() -> Path | None:
    for name in CANDIDATES:
        candidate = REPO / name / VENV_BIN / VENV_EXE
        if candidate.exists():
            return candidate
    return None


def setup() -> Path:
    """Build ``.venv`` and install the engine's dependencies. No Airflow."""
    say("creating .venv and installing requirements-dev.txt ...", CYAN)
    subprocess.run([sys.executable, "-m", "venv", str(REPO / ".venv")], check=True)
    python = REPO / ".venv" / VENV_BIN / VENV_EXE
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                   check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", "-r",
                    str(REPO / "requirements-dev.txt")], check=True)
    say("done.\n", CYAN)
    return python


# ------------------------------------------------------------------- colour
# Enabled only for a terminal that will render it. Windows consoles need VT
# processing turned on explicitly; where that fails, or where output is being
# piped into a file, the markers below carry the whole meaning on their own.
def _ansi_works() -> bool:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        return bool(kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7))
    except Exception:
        return False


COLOUR = _ansi_works()
GREY, CYAN, GREEN, YELLOW, RED, WHITE, RESET = (
    ("\033[90m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[97m", "\033[0m")
    if COLOUR else ("", "", "", "", "", "", ""))


def say(message: str = "", colour: str = "") -> None:
    print(f"{colour}{message}{RESET}" if colour else message, flush=True)


# -------------------------------------------------------------------- the tour
# Ordered the way the project's argument builds rather than alphabetically, and
# each step states the question it answers before it answers it: a wall of
# output nobody can read is the failure this project is about.
#
# (title, question, arguments, expect_nonzero, slow)
def steps(domain: str, version: str) -> list[tuple]:
    # What the candidate is compared against. The tour is normally v2 against
    # the v1 it amends; running it *on* v1 would ask for v1 against itself,
    # which agrees on every case by construction and which ptm.report refuses
    # rather than print as a finding. Comparing against v2 instead keeps the
    # step meaningful whichever version the tour is pointed at.
    against = "v2" if version == "v1" else "v1"
    return [
        ("Lint",
         "Do the offline rules still implement the policies they cite?",
         ["ptm.lint"], False, False),
        ("Preflight",
         "What is wrong with the policy before a replay is paid for?",
         ["ptm.preflight", domain], False, False),
        ("Selftest",
         "The whole loop: replay, attribution, blast radius, confirmation, gate, proposal.",
         ["ptm.selftest", domain, version], False, True),
        ("Point-in-time check",
         "How many cases does a naive backtest get wrong? (all in one direction)",
         ["ptm.pit_check", domain, version], False, False),
        ("Precedent gate",
         "Does this policy reverse a ruling a human already made?",
         ["ptm.gate", domain, version], True, False),
        ("Precedent gate (introduced only)",
         "...and how many of those reversals is the PROPOSAL responsible for?",
         ["ptm.gate", domain, version, "--introduced-only"], False, False),
        ("Threshold sweep",
         "So what should the number actually be?",
         ["ptm.sweep", domain, version, "1.1", "amount_gbp", "25,50,75,100,150,250"],
         False, False),
        ("Threshold sweep (a dial that decides nothing)",
         "A flat curve is not an insensitive threshold. Watch for the WARNING.",
         ["ptm.sweep", domain, version, "6.1", "grade", "2,3,4,6"], False, False),
        ("Joint sweep",
         "Two dials at once - one curve cannot show them interacting.",
         ["ptm.sweep", domain, version, "--joint",
          "1.1:amount_gbp=25,50,75,100,150", "3.1:days_notice=3,7,14,21"], False, True),
        ("Sample size",
         "How big a change could this much history even detect?",
         ["ptm.report", domain, version, "--power", "--target", "0.20"], False, False),
        ("Rules vs the judge",
         "Do the offline rules agree with the judge the sweep rests on?",
         ["ptm.rules", domain, version], False, False),
        ("Calibration",
         "Is the judge RIGHT? Scored against the humans who ruled.",
         ["ptm.calibration", domain, version], False, False),
        ("Who it lands on",
         "Is this change concentrated on one group? The question compliance asks first.",
         ["ptm.disparity", domain, version], False, False),
        ("Proposal",
         "Draft the next version of the policy from the evidence (writes nothing).",
         ["ptm.proposal", domain, version], False, False),
        # The question the whole loop is for, and the one the tour never showed:
        # every step above measures one version. These two put two of them side
        # by side, which is the only way "did the edit help?" gets an answer.
        ("Every version, side by side",
         "Did the edit help? What each version moves, causes, and reverses.",
         ["ptm.report", domain, "--history"], False, False),
        ("The two of them, case by case",
         "Which decisions actually differ - a count of differences is not a list.",
         ["ptm.report", domain, "--compare", against, version], False, False),
        ("Export",
         "Every panel with its caveats attached, as one file.",
         ["ptm.report", domain, version, "-o", f"ptm-{domain}-{version}.json"],
         False, False),
        ("Precedents",
         "The only output that cannot be recomputed, in a form you can move.",
         ["ptm.precedents", domain, "-o", f"ptm-{domain}-precedents.json"], False, False),
        ("Retention (dry run)",
         "Which rows have stopped earning their disk?",
         ["ptm.prune", "--dry-run"], False, False),
    ]


RULE = "=" * 78


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Policy Time Machine engine end to end, with no Airflow.",
        epilog="Some steps exit non-zero on purpose; see the module docstring.")
    parser.add_argument("--domain", default="expenses",
                        help="which domain to run against (default: expenses)")
    parser.add_argument("--version", default="v2",
                        help="the candidate policy version (default: v2)")
    parser.add_argument("--setup", action="store_true",
                        help="create .venv and install requirements-dev.txt first")
    parser.add_argument("--step", action="store_true",
                        help="pause after each section, so it can be talked over")
    parser.add_argument("--quick", action="store_true",
                        help="skip the slow steps (selftest, the joint grid)")
    parser.add_argument("--list", action="store_true",
                        help="print the steps without running any of them")
    args = parser.parse_args(argv)

    plan = steps(args.domain, args.version)

    if args.list:
        for title, question, arguments, expect_nonzero, slow in plan:
            marks = "".join([" [slow]" if slow else "",
                             " [exits non-zero]" if expect_nonzero else ""])
            say(f"  {title}{marks}", WHITE)
            say(f"      {question}", GREY)
            say(f"      python -m {' '.join(arguments)}", GREY)
        return 0

    python = setup() if args.setup else find_python()
    if python is None:
        say("No virtualenv found.", RED)
        say(f"Run:  {Path(sys.executable).name} {Path(__file__).name} --setup", RED)
        say("(it creates .venv and installs pydantic, PyYAML, pytest and ruff - "
            "no Airflow)")
        return 2

    # Relative, like the Makefile's. Without these ptm.config falls back to
    # /opt/airflow/include, which exists only inside the container - the symptom
    # is "no domain config 'expenses'" with an empty list of available ones,
    # which reads as a broken checkout rather than as an unset variable.
    environment = {
        **os.environ,
        "PTM_INCLUDE_DIR": "./include",
        "PTM_DB": "./include/ptm.db",
        "PTM_OFFLINE": "1",
    }

    say("")
    say("Policy Time Machine - the engine, end to end", WHITE)
    say(f"  platform    : {platform.system()} {platform.machine()}")
    say(f"  interpreter : {python.relative_to(REPO)}")
    say(f"  domain      : {args.domain} / {args.version}")
    say("  judge       : offline (deterministic rules; no API key, no network)")

    results: list[tuple[str, int, float, bool]] = []
    for title, question, arguments, expect_nonzero, slow in plan:
        if args.quick and slow:
            say("")
            say(f"-- {title} (skipped: --quick)", GREY)
            continue

        say("")
        say(RULE, GREY)
        say(f"  {title}", CYAN)
        say(f"  {question}", GREY)
        say(f"  $ python -m {' '.join(arguments)}", GREY)
        say(RULE, GREY)

        started = time.monotonic()
        completed = subprocess.run([str(python), "-m", *arguments],
                                   cwd=REPO, env=environment)
        elapsed = time.monotonic() - started
        ok = completed.returncode == 0 or (expect_nonzero and completed.returncode == 1)
        results.append((title, completed.returncode, elapsed, ok))

        if not ok:
            say("")
            say(f"  ^ {title} exited {completed.returncode}, which it should not have.", RED)

        if args.step:
            say("")
            try:
                input("  [enter] for the next step ")
            except (EOFError, KeyboardInterrupt):
                say("")
                return 130

    say("")
    say(RULE, GREY)
    say("  Summary", WHITE)
    say(RULE, GREY)
    for title, code, elapsed, ok in results:
        if not ok:
            mark, colour = "FAIL", RED
        elif code != 0:
            mark, colour = "ok* ", YELLOW
        else:
            mark, colour = "ok  ", GREEN
        say(f"  {mark} {title:<44} exit {code}  {elapsed:5.1f}s", colour)
    say("  ok* = exited non-zero, which is the correct answer for that step", GREY)

    failed = [title for title, _, _, ok in results if not ok]
    say("")
    if failed:
        say(f"{len(failed)} step(s) failed: {', '.join(failed)}", RED)
        return 1

    say(f"Everything ran. Wrote ptm-{args.domain}-{args.version}.json and "
        f"ptm-{args.domain}-precedents.json.", GREEN)
    say("")
    say("Next, the half that needs Airflow - the DAGs, the backfill that is the", WHITE)
    say("simulation engine, the human-in-the-loop queue and the Diff Explorer:", WHITE)
    say("")
    say("  1. Start Docker" + (" Desktop" if os.name == "nt" else ""))
    say("  2. docker compose up --build -d")
    say("  3. http://localhost:8080/ptm/   (the Diff Explorer; no login)")
    say(f"  4. docker compose exec airflow airflow backfill create "
        f"--dag-id replay_{args.domain} \\")
    say("       --from-date 2024-09-01 --to-date 2026-09-01 --run-backwards")
    say("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
