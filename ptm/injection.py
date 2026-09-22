"""Which cases are arguing with the policy instead of being judged by it.

:mod:`ptm.judge` fences the case record so that hostile text inside it cannot
forge the prompt's own structure. That is the defence, and it is deliberately
the whole of the defence: it costs nothing, it needs no model, and it does not
depend on recognising an attack. This module is the other half, and it answers a
different question - *which cases are trying?*

The two are not substitutes. A fence that holds turns a steering attempt into an
ordinary verdict on an odd-looking claim, which is the right outcome and also a
silent one. Nobody learns that an employee wrote a fake clause into the notes
field of a GBP 4,800 dinner, and that is worth a human's attention whatever the
judge decided - it is a fact about the claim, not about the prompt.

**It reports, it does not sanitise.** Nothing here rewrites a payload. The
method this project rests on is replaying what was actually recorded, and a
replay of a cleaned-up record measures a history that never happened. A finding
is a sentence naming the case, the field and what was found in it.

**It runs before the replay is paid for**, like :mod:`ptm.preflight`, and for
the same reason: a full LLM-backed replay of the shipped fixture is about USD 4,
and the moment to discover that forty of the cases contain forged clause text is
before the bill rather than while reading the attribution afterwards.

**The severities are the interesting part.** A claimant writing *"I think clause
3.1 applies here"* is doing something completely legitimate - arguing their case
in the vocabulary of the policy, which is what an appeals process asks people to
do - and a check that flagged it would flag half of any real refund queue and
stop being read within a week, which is the argument :mod:`ptm.disparity` makes
about nine-case buckets. So a citation of a clause the policy *has* is not a
finding at all. What is a finding is text impersonating the machinery around
the case: the prompt's own section headings, a fence marker, a role label, an
instruction about which outcome to return. No honest note contains those, in
any register, in any domain.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime

from . import cli

#: What a finding looks for, as ``(name, severity, pattern, what it means)``.
#:
#: Ordered most to least serious. Every pattern is about the *shape* of the
#: prompt rather than the vocabulary of any domain - this module has to stay as
#: ignorant of expenses and refunds as everything else outside the YAML, and a
#: pattern list that had learned the word "reimbursement" would be a domain rule
#: hiding in the engine.
SIGNALS: tuple[tuple[str, str, re.Pattern, str], ...] = (
    (
        "fence",
        "error",
        re.compile(r"</?case-record", re.IGNORECASE),
        "contains a case-record fence marker, which only the prompt builder writes",
    ),
    (
        "section",
        "error",
        # The prompt's own headings. Anchored to a line start, because the
        # phrases themselves are ordinary English - "your task" in the middle of
        # a sentence is a person talking, and a line beginning "# Your task" is
        # a person building a prompt.
        re.compile(
            r"^[ \t]{0,3}#{1,6}[ \t]*(the policy|your task|rules you must follow|"
            r"the case)\b",
            re.IGNORECASE | re.MULTILINE),
        "opens a heading that impersonates one of the prompt's own sections",
    ),
    (
        "role",
        "error",
        re.compile(r"(?m)(^[ \t]{0,3}(system|assistant|human|user)[ \t]*:)"
                   r"|<\|[^|>]{1,40}\|>",
                   re.IGNORECASE),
        "carries a conversation role label or a special token delimiter",
    ),
    (
        "override",
        "error",
        re.compile(
            r"\b(ignore|disregard|forget|override|set aside)\b[^.\n]{0,40}\b"
            r"(previous|above|prior|earlier|foregoing|the policy|all instructions?|"
            r"your instructions?)\b",
            re.IGNORECASE),
        "instructs the reader to set aside what came before it",
    ),
    (
        "verdict",
        "error",
        # An instruction about the *answer*, not an argument for it. "Please
        # approve this" is a request and deliberately does not match; "return
        # the outcome approve" and "confidence: 1.0" are the machine register.
        re.compile(
            r"\b(return|output|respond with|answer with|reply with|set)\b"
            r"[^.\n]{0,30}\b(outcome|verdict|confidence)\b"
            r"|\boutcome[ \t]*[:=][ \t]*\"?\w"
            r"|\bconfidence[ \t]*[:=][ \t]*[01.]",
            re.IGNORECASE),
        "dictates the outcome or confidence the judge should return",
    ),
    (
        "heading",
        "warning",
        re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S", re.MULTILINE),
        "contains a markdown heading, which a recorded note would not normally have",
    ),
    (
        "fabricated_clause",
        "warning",
        # Only clauses the policy does not have; the caller supplies the real
        # ones. Citing one that exists is a claimant arguing their case, which
        # is legitimate and deliberately not reported.
        re.compile(r"\bclauses?[ \t]+(\d+\.\d+)", re.IGNORECASE),
        "cites a clause number this policy does not contain",
    ),
)

#: Severity that stops a build when the caller asked for a gate. A warning is
#: worth printing and not worth failing on: a markdown heading in a notes field
#: is more often somebody pasting from a document than somebody attacking one.
GATING = "error"

#: How much of the offending text a finding quotes. Enough to recognise, short
#: enough that a hundred findings still fit on a screen.
EXCERPT = 160


def _excerpt(pattern: re.Pattern, text: str) -> str:
    """The matching line, trimmed to one line and to :data:`EXCERPT`."""
    match = pattern.search(text)
    if not match:  # pragma: no cover - callers only ask after a hit
        return ""
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    line = text[start:end if end != -1 else len(text)].strip()
    return (line[:EXCERPT] + "...") if len(line) > EXCERPT else line


def scan_text(text: str, known_clauses: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """Every signal one string trips, as findings with no case attached yet.

    ``known_clauses`` are the clause numbers the policy actually states. A
    citation of one of them is not reported - see ``fabricated_clause`` above.
    """
    found: list[dict] = []
    for name, severity, pattern, meaning in SIGNALS:
        matches = pattern.findall(text)
        if not matches:
            continue
        if name == "fabricated_clause":
            invented = sorted({m for m in matches if m not in known_clauses})
            if not invented:
                continue
            meaning = (f"cites clause(s) {', '.join(invented)}, which this policy "
                       f"does not contain")
        found.append({"signal": name, "severity": severity, "detail": meaning,
                      "excerpt": _excerpt(pattern, text)})
    return found


def scan_payload(payload: dict,
                 known_clauses: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """Findings for one case payload, with the field named on each.

    Only string values are examined, and every one of them is - not a list of
    fields somebody remembered to enumerate. A domain adds a free-text field by
    editing its YAML, and a check that had to be told about it would always be
    one version behind the thing it protects.
    """
    found: list[dict] = []
    for field, value in sorted(payload.items()):
        if not isinstance(value, str) or not value.strip():
            continue
        for finding in scan_text(value, known_clauses):
            found.append({"field": field, **finding})
    return found


def scan(domain_name: str, version: str, limit: int | None = None) -> dict:
    """Every case in this domain whose recorded text argues with the machinery.

    Reads the cases a replay would read, hydrated the same way, so a finding
    here is about a prompt that would actually be built rather than about a row
    in a table.
    """
    from . import store
    from .config import load_domain

    domain = load_domain(domain_name)
    if version not in domain.policies:
        raise LookupError(f"Unknown policy version: {version}")
    known = frozenset(domain.clauses(version))

    cases = store.load_cases(domain_name, until=datetime.max, limit=limit)
    findings = []
    for case in cases:
        hits = scan_payload(case.payload, known)
        if hits:
            findings.append({
                "case_id": case.case_id,
                "decided_at": case.decided_at.isoformat(),
                "actual_outcome": case.actual_outcome,
                "impact": domain.impact_of(case.payload),
                "severity": ("error" if any(h["severity"] == "error" for h in hits)
                             else "warning"),
                "findings": hits,
            })
    errors = [f for f in findings if f["severity"] == "error"]
    return {
        "domain": domain_name,
        "version": version,
        "scanned": len(cases),
        "flagged": len(findings),
        "errors": len(errors),
        "warnings": len(findings) - len(errors),
        "impact_unit": domain.impact_unit,
        "cases": findings,
        "summary": describe(domain_name, len(cases), findings),
        "caveat": (
            "this is a report, not a defence. The case record is fenced in the "
            "prompt (ptm.judge.fence), so a case cannot forge the prompt's "
            "structure whether or not it appears here; what a finding says is "
            "that somebody tried, which is a fact about the claim and worth a "
            "human's attention on its own."),
        "caveat_key": "caveat.injection",
    }


def describe(domain_name: str, scanned: int, findings: list[dict]) -> str:
    """The scan as the CLI prints it."""
    if not scanned:
        return (f"no cases stored for {domain_name}, so there is nothing to read. "
                f"Seed or import first.")
    if not findings:
        return (f"{scanned} case(s) read; none of them contains text impersonating "
                f"the prompt's structure.")
    errors = sum(1 for f in findings if f["severity"] == "error")
    lines = [f"{len(findings)} of {scanned} case(s) carry text aimed at the machinery "
             f"rather than at the policy - {errors} serious:"]
    for row in findings:
        mark = "!!" if row["severity"] == "error" else " -"
        lines.append(f" {mark} {row['case_id']} (recorded {row['actual_outcome']}, "
                     f"{row['decided_at'][:10]})")
        for hit in row["findings"]:
            lines.append(f"      {hit['field']}: {hit['detail']}")
            lines.append(f"        {hit['excerpt']!r}")
    return "\n".join(lines)


USAGE = """usage:
  python -m ptm.injection [domain] [version] [--gate warn|fail] [--limit N] [--json]

Which recorded cases contain text aimed at the judge rather than at the policy -
forged section headings, fence markers, role labels, instructions about which
outcome to return. Read from the cases already stored, so it costs nothing and
needs no key.

  domain        defaults to 'expenses'
  version       defaults to 'v2'
  --gate MODE   'fail' exits non-zero when a case trips a serious signal;
                'warn' (the default) reports and returns 0.
  --limit N     read only the first N cases, for a very large history.
  --json        the whole finding set as JSON, for a report or a PR comment.

The prompt fences the case record either way - see ptm.judge. This says who
tried, which is a fact about the claim and not only about the prompt."""


def _option(args: list[str], flag: str, allowed: set[str] | None) -> tuple[str | None, bool]:
    """One ``--flag value`` pair, removed from ``args``. Returns (value, ok)."""
    if flag not in args:
        return None, True
    index = args.index(flag)
    raw = args[index + 1] if index + 1 < len(args) else ""
    if raw.startswith("-") or (allowed is not None and raw not in allowed):
        return raw, False
    args.pop(index + 1)
    return raw, True


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.injection [domain] [version]``; non-zero only under --gate fail.

    The fence in :mod:`ptm.judge` is applied whether or not anybody runs this,
    which is why the default is ``warn``. Reporting is the point; gating is for
    a pipeline that has decided a claim trying to write its own verdict should
    stop the line.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0

    as_json = "--json" in args

    mode, ok = _option(args, "--gate", {"warn", "fail"})
    if not ok:
        print(f"ERROR --gate takes 'warn' or 'fail', got {mode or 'nothing'!r}\n\n"
              f"{USAGE}", file=sys.stderr)
        return 2
    raw_limit, ok = _option(args, "--limit", None)
    limit = None
    if ok and raw_limit is not None:
        ok = raw_limit.isdigit() and int(raw_limit) > 0
        limit = int(raw_limit) if ok else None
    if not ok:
        print(f"ERROR --limit takes a positive integer, got {raw_limit or 'nothing'!r}"
              f"\n\n{USAGE}", file=sys.stderr)
        return 2

    unknown = [a for a in args
               if a.startswith("-") and a not in {"--json", "--gate", "--limit"}]
    if unknown:
        print(f"ERROR unknown option {unknown[0]!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    positional = [a for a in args if not a.startswith("-")]
    domain_name = positional[0] if positional else "expenses"
    version = positional[1] if len(positional) > 1 else "v2"

    from . import store
    store.init_db()
    try:
        result = scan(domain_name, version, limit)
    except (LookupError, FileNotFoundError) as exc:
        # The refusal is a document too, on the same contract as ptm.sweep and
        # ptm.gate: a caller parsing stdout gets an object saying why rather
        # than an empty pipe it has to tell apart from a crash.
        if as_json:
            print(json.dumps({"ran": False, "code": 2, "error": str(exc),
                              "domain": domain_name, "version": version}, indent=2))
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps({**result, "ran": True, "code": 0, "gate": mode or "warn"},
                         indent=2, default=str))
    else:
        print(result["summary"])
        print(f"  {result['caveat']}")

    if result["errors"] and mode == "fail":
        print(f"\nGATE FAILS: {result['errors']} case(s) contain text impersonating the "
              f"prompt's structure, and --gate is 'fail'.", file=sys.stderr)
        return 1
    if result["errors"]:
        # Not silent, and not an exit code - the same argument ptm.disparity
        # makes. A question nobody prints is a question nobody is asked.
        print(f"\n{result['errors']} case(s) would fail a 'fail' gate; this run is "
              f"'warn', so they are reported and not enforced.", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
