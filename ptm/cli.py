"""One definition of ``--help``, shared by every ``python -m ptm.*`` entry point.

Each module here parses its own positional arguments by hand - they take a
domain and a version and little else, and an argparse parser per module would
be more machinery than the interface deserves. What that cost was ``--help``:
eight entry points and only :mod:`ptm.sweep` answered it, because its usage
string happened to be what it printed when given too few arguments.

The rest read ``--help`` as a *domain name*. ``python -m ptm.preflight --help``
answered ``no domain config '--help'``, ``ptm.lint --help`` reported it as a
lint error against a domain by that name, and ``ptm.selftest --help`` got as far
as trying to seed it and exited on an uncaught ``KeyError`` - a traceback, from
the one command whose whole job is to prove the project runs cleanly.

So the check lives here rather than in nine places: one flag set, applied
first, before any argument is interpreted as a name. A module supplies its
``USAGE`` string and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Spellings that mean "tell me how to use this". Deliberately not the bare word
#: ``help``: a domain is named on the command line positionally, and a domain
#: called ``help`` is a perfectly legal YAML file. Anything starting with ``-``
#: can never be one.
HELP_FLAGS = frozenset({"-h", "--help"})


def wants_help(args: Iterable[str]) -> bool:
    """Whether the caller asked for usage rather than for work.

    Checked before the arguments are interpreted, so it answers the same way
    whether or not the rest of the line makes sense - ``--help`` on a command
    with a missing or misspelled domain is exactly when it is most wanted.
    """
    return any(arg in HELP_FLAGS for arg in args)
