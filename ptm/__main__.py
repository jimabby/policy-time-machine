"""One command, so an installed copy of this project has something on PATH.

``pyproject.toml`` has made ``ptm`` an installable package since the day the
Makefile stopped being the only way in, and ``pip install -e .`` still put
nothing anywhere a shell could find it. Everything was reachable as
``python -m ptm.<something>`` - nineteen of them - which works perfectly in a
checkout and reads, to anybody who installed the package, exactly like a
package with no commands in it.

So this is the dispatcher, and it is deliberately thin: each command is the
module's own ``main`` called with the rest of the line, so ``ptm gate expenses
v2 --json`` and ``python -m ptm.gate expenses v2 --json`` are the same code
reached two ways and cannot disagree about anything, including their exit
codes. Nothing here parses a domain, a version or a flag.

**It also absorbs what manage.py was for.** That script exists because every
measurement reads ``PTM_DB`` and ``PTM_INCLUDE_DIR`` from the environment, and
somebody who imported two years of their own history into a second database had
no supported way to point a command at it. ``--db`` and ``--include`` are the
same lever, available wherever the package is installed rather than only where
the repository is checked out. They are set into the environment *before* the
command's module is imported, because :mod:`ptm.config` reads both at import
time - which is also why every import below is lazy.
"""

from __future__ import annotations

import os
import sys

#: command -> (module, attribute). The attribute is named because
#: :mod:`ptm.selftest` calls its entry point ``cli_main`` - it has a ``main``
#: that means something else, and papering over that here would make the
#: dispatcher and the module disagree about what running it does.
COMMANDS: dict[str, tuple[str, str]] = {
    "calibration": ("ptm.calibration", "main"),
    "crosscheck": ("ptm.crosscheck", "main"),
    "disparity": ("ptm.disparity", "main"),
    "gate": ("ptm.gate", "main"),
    "import": ("ptm.ingest", "main"),
    "injection": ("ptm.injection", "main"),
    "lint": ("ptm.lint", "main"),
    "pit-check": ("ptm.pit_check", "main"),
    "precedents": ("ptm.precedents", "main"),
    "preflight": ("ptm.preflight", "main"),
    "proposal": ("ptm.proposal", "main"),
    "provenance": ("ptm.provenance", "main"),
    "prune": ("ptm.prune", "main"),
    "replay": ("ptm.replay", "main"),
    "report": ("ptm.report", "main"),
    "rules": ("ptm.rules", "main"),
    "seed": ("ptm.seed", "main"),
    "selftest": ("ptm.selftest", "cli_main"),
    "stability": ("ptm.stability", "main"),
    "sweep": ("ptm.sweep", "main"),
}

#: One line each, for the command list. Deliberately shorter than each module's
#: own ``USAGE``: this is the page somebody reads to find out which command they
#: want, and twenty paragraphs is a page nobody finishes.
SUMMARY: dict[str, str] = {
    "calibration": "is the judge right? scored against the humans who ruled",
    "crosscheck": "a second, independent judge on the same cases",
    "disparity": "who the change lands on far harder than everyone else",
    "gate": "hold a policy to the human rulings on file",
    "import": "validate and load your own history from CSV or JSON",
    "injection": "which recorded cases argue with the judge, not the policy",
    "lint": "check a domain's offline fixtures against its policies",
    "pit-check": "prove the replay cannot see the future",
    "precedents": "export, import or record one human ruling",
    "preflight": "read the policy before paying to replay it",
    "proposal": "draft an amendment from the evidence, and list drafts",
    "provenance": "replay coverage, archived inputs, stranded runs",
    "prune": "reclaim disk; retention, vacuum, cache",
    "replay": "replay stored history under a policy, offline",
    "report": "the bundle, a comparison, the history - JSON, CSV or HTML",
    "rules": "do the offline rules implement the policy?",
    "seed": "build the synthetic demo history",
    "selftest": "the whole loop end to end, no Airflow and no key",
    "stability": "the error bar on a flip rate: judge noise",
    "sweep": "re-run the replay at each candidate threshold",
}

USAGE = """usage:
  ptm [--db PATH] [--include PATH] <command> [arguments...]
  python -m ptm.__main__ ...          the same thing, from a checkout

Every measurement in this project, without Airflow and without an API key.
`ptm <command> --help` is that command's own usage; the arguments are passed
through untouched, so anything documented as `python -m ptm.<command> ...`
works here with the same flags and the same exit code.

  --db PATH       the database to read and write (PTM_DB)
  --include PATH  the folder holding domains/ and policies/ (PTM_INCLUDE_DIR)

commands:
{commands}

Both default to the environment - PTM_INCLUDE_DIR and PTM_DB - and, failing
that, to the container paths the compose file mounts. From a checkout that
means `--include ./include` is worth typing, or exporting the variable once.
Point --db at a second database to measure history you imported rather than the
demo fixture."""


def usage() -> str:
    width = max(len(name) for name in COMMANDS)
    return USAGE.format(commands="\n".join(
        f"  {name:<{width}}  {SUMMARY.get(name, '')}" for name in sorted(COMMANDS)))


def _take(args: list[str], flag: str) -> tuple[str | None, bool]:
    """``--flag value`` removed from ``args``. Returns (value, ok)."""
    if flag not in args:
        return None, True
    index = args.index(flag)
    value = args[index + 1] if index + 1 < len(args) else ""
    if not value or value.startswith("-"):
        return None, False
    del args[index:index + 2]
    return value, True


def main(argv: list[str] | None = None) -> int:
    """``ptm <command> [arguments...]``; the command's own exit code.

    Options before the command are this dispatcher's; everything from the
    command onwards belongs to the command, which is why the scan below stops
    at the first bare word. ``ptm report --db x`` therefore passes ``--db x``
    to :mod:`ptm.report`, where it is an unknown option and reported as one -
    the alternative is a global flag silently eaten out of the middle of
    somebody else's argument list.
    """
    args = list(argv if argv is not None else sys.argv[1:])

    # Where the command name is. Nothing before it may be a positional, so an
    # unknown leading flag is an error here rather than a mystery inside a
    # module that never saw it.
    head: list[str] = []
    while args and args[0].startswith("-"):
        head.append(args.pop(0))
        if head[-1] in {"--db", "--include"} and args:
            head.append(args.pop(0))

    for flag, variable in (("--db", "PTM_DB"), ("--include", "PTM_INCLUDE_DIR")):
        value, ok = _take(head, flag)
        if not ok:
            print(f"ERROR {flag} needs a path\n\n{usage()}", file=sys.stderr)
            return 2
        if value is not None:
            # Set before the command's module is imported - ptm.config reads
            # both at import time, so doing this afterwards would change the
            # environment and nothing else.
            os.environ[variable] = os.path.abspath(value)

    # Help asked for before any command, or after something that is not one.
    # `ptm gate --help` is deliberately *not* caught here: the flag belongs to
    # the command, and answering it with this page instead would make the
    # dispatcher the one thing standing between a reader and the usage they
    # asked for. `ptm nonsense --help` is caught, because there is no command
    # to hand it to and a usage page is what was wanted.
    asked = any(flag in head for flag in ("-h", "--help"))
    if not asked and args and args[0] not in COMMANDS:
        asked = any(flag in args for flag in ("-h", "--help"))
    if asked or not args:
        # No command is not an error: somebody typing `ptm` is asking what this
        # is, and the answer is the list. Exit 0 for the same reason every
        # module here exits 0 on --help - a shell checking the status should not
        # read "here is how to use me" as a broken install.
        print(usage())
        return 0
    if head:
        print(f"ERROR unknown option {head[0]!r} before the command\n\n{usage()}",
              file=sys.stderr)
        return 2

    name, rest = args[0], args[1:]
    if name not in COMMANDS:
        near = [c for c in sorted(COMMANDS) if c.startswith(name[:3])]
        hint = f" Did you mean {' or '.join(repr(c) for c in near)}?" if near else ""
        print(f"ERROR no command {name!r}.{hint}\n\n{usage()}", file=sys.stderr)
        return 2

    import importlib
    module_name, attribute = COMMANDS[name]
    return getattr(importlib.import_module(module_name), attribute)(rest)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
