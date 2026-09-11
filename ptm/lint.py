"""Check a domain's configuration against the policies it claims to implement.

Two kinds of drift this catches, both silent at runtime:

**A fixture citing a clause the policy does not contain.** ``offline_rules`` are
maintained by hand, next to but separate from the markdown policy. Nothing
stops them diverging, and when they do, the offline demo stops implementing the
policy it says it implements while still producing confident output.

**A rule referencing a field that does not exist.** This is the dangerous one.
:func:`ptm.judge.offline_verdict` deliberately swallows exceptions, so a rule
that says ``grade`` where the payload says ``employee_grade`` does not crash -
it simply never matches, and the replay quietly comes out wrong.

    python -m ptm.lint            # every domain
    python -m ptm.lint expenses   # one domain

Exits non-zero if any ERROR is found. Warnings do not fail the lint: some are
legitimate, such as a discretion clause that no mechanical rule can express.
"""

from __future__ import annotations

import ast
import string
import sys
from dataclasses import dataclass

from .config import DomainConfig, available_domains, load_domain
from .judge import SAFE_BUILTINS, SAFE_NAMES


@dataclass
class Problem:
    level: str  # "ERROR" | "WARN"
    domain: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level:5} {self.domain}/{self.where}: {self.message}"


def template_fields(domain: DomainConfig) -> set[str]:
    """Field names the case template actually renders.

    The case template is the contract with the judge: it is precisely what the
    model is shown, so a rule reasoning about anything outside it is reasoning
    about data the real judge never sees.

    Deliberately *excludes* ``pit_field``. The two sets are different questions -
    "is this in the payload" versus "is the judge shown it" - and conflating them
    is what let the pit_field check below silently never fire.
    """
    return {
        name for _, name, _, _ in string.Formatter().parse(domain.case_template)
        if name
    }


def payload_fields(domain: DomainConfig) -> set[str]:
    """Fields a hydrated payload can be expected to carry.

    The rendered template plus the point-in-time fact, which :func:`
    ptm.store.load_cases` merges in per case rather than storing on the record.
    """
    rendered = template_fields(domain)
    return rendered | ({domain.pit_field} if domain.pit_field else set())


def _names_in(expression: str) -> set[str]:
    """Identifiers an expression reads, ignoring the safe-builtin helpers."""
    tree = ast.parse(expression, mode="eval")
    return {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in SAFE_NAMES
    }


def check_domain(name: str) -> list[Problem]:
    problems: list[Problem] = []

    def err(where: str, message: str) -> None:
        problems.append(Problem("ERROR", name, where, message))

    def warn(where: str, message: str) -> None:
        problems.append(Problem("WARN", name, where, message))

    try:
        domain = load_domain(name)
    except Exception as exc:
        return [Problem("ERROR", name, "config", f"will not load: {exc}")]

    rendered = template_fields(domain)
    known = payload_fields(domain)

    # --- the domain's own contract ----------------------------------------
    if len(set(domain.outcomes)) != len(domain.outcomes):
        err("outcomes", f"contains duplicates: {domain.outcomes}")
    if len(domain.outcomes) < 2:
        err("outcomes", "needs at least two outcomes to have anything to diff")
    if domain.impact_field and domain.impact_field not in known:
        err("impact_field", f"{domain.impact_field!r} is not in case_template; "
                            f"impact will silently read as 0")
    if domain.pit_field and domain.pit_field not in rendered:
        err("pit_field", f"{domain.pit_field!r} is not rendered in case_template, so the "
                          f"judge is asked to respect a fact it is never shown")
    for field in domain.segment_fields:
        if field not in known:
            err("segment_fields", f"{field!r} is not a case field; that segment would be "
                                  f"entirely 'unknown'")
    for field in domain.conflicts.key:
        if field not in known:
            err("conflicts.key", f"{field!r} is not a case field; conflict detection would "
                                 f"never match two precedents")
        elif field == domain.pit_field:
            # Tested against pit_field directly, not against the template. A
            # pit_field absent from the template is already an error above, so
            # a "in the payload but not rendered" test could never fire and
            # this check silently passed every domain it was written to catch.
            err("conflicts.key", f"{field!r} is the point-in-time fact, which load_cases "
                                 f"merges in per case and which is not part of the stored "
                                 f"case record. Conflict detection compares cases as filed, "
                                 f"so this reads as 'unknown' for every precedent and "
                                 f"coarsens every signature")

    # A payload field named like a helper shadows it during name resolution, so
    # a rule calling that helper silently stops working - and offline_verdict
    # swallows the resulting exception, which is the failure mode this whole
    # lint exists for.
    shadowed = sorted(known & set(SAFE_BUILTINS))
    if shadowed:
        warn("case_template", f"field(s) {shadowed} shadow the rule helpers of the same "
                              f"name; a 'when' expression calling one would silently never "
                              f"match")
    if not 0.0 <= domain.review.below_confidence <= 1.0:
        err("review", f"below_confidence {domain.review.below_confidence} is outside 0..1")
    if domain.review.max_reviews < 1:
        err("review", "max_reviews below 1 means no flip ever reaches a human")

    # --- the policies ------------------------------------------------------
    clauses_by_version: dict[str, set[str]] = {}
    for version in sorted(domain.policies):
        where = f"policies/{version}"
        try:
            text = domain.policy_text(version)
        except OSError as exc:
            err(where, f"cannot be read: {exc}")
            continue
        if not text.strip():
            err(where, "is empty")
        clauses_by_version[version] = set(domain.clauses(version))
        if not clauses_by_version[version]:
            warn(where, "declares no numbered clauses (expected lines like '1.1 ...'), "
                        "so clause attribution will report everything as unattributed")

    # --- the offline fixtures against those policies -----------------------
    for version, rules in sorted(domain.offline_rules.items()):
        where = f"offline_rules/{version}"
        if version not in domain.policies:
            err(where, f"has no matching policy version; known versions are "
                       f"{sorted(domain.policies)}")
            continue
        declared = clauses_by_version.get(version, set())
        cited: set[str] = set()
        produced: set[str] = set()

        for i, rule in enumerate(rules):
            at = f"{where}[{i}]"
            expression = rule.get("when")
            if not expression:
                err(at, "has no 'when' expression")
                continue
            try:
                names = _names_in(expression)
            except SyntaxError as exc:
                err(at, f"'when' does not parse: {exc}")
                continue
            unknown = sorted(names - known)
            if unknown:
                err(at, f"'when' reads unknown field(s) {unknown}; the rule would never "
                        f"match and the replay would be silently wrong. Known fields: "
                        f"{sorted(known)}")

            outcome = rule.get("outcome")
            if outcome not in domain.outcomes:
                err(at, f"outcome {outcome!r} is not one of {domain.outcomes}")
            else:
                produced.add(outcome)

            clause = str(rule.get("clause", "")).strip()
            if not clause:
                warn(at, "cites no clause; its flips will show as '(unattributed)'")
            else:
                cited.add(clause)
                if declared and clause not in declared:
                    err(at, f"cites clause {clause!r}, which policy {version} does not "
                            f"contain. Declared clauses: {sorted(declared)}")

            confidence = rule.get("confidence", 0.9)
            try:
                if not 0.0 <= float(confidence) <= 1.0:
                    err(at, f"confidence {confidence} is outside 0..1")
            except (TypeError, ValueError):
                err(at, f"confidence {confidence!r} is not a number")

        uncited = sorted(declared - cited)
        if uncited:
            warn(where, f"policy clause(s) {uncited} are not exercised by any offline rule. "
                        f"Expected for discretion clauses; a bug for substantive ones.")
        # The default outcome is reachable without a rule, hence the discount.
        unreachable = sorted(set(domain.outcomes) - produced - {domain.outcomes[0]})
        if unreachable:
            warn(where, f"no rule can ever produce outcome(s) {unreachable}")

    for version in sorted(domain.policies):
        if version not in domain.offline_rules:
            warn(f"offline_rules/{version}", "has no offline rules, so PTM_OFFLINE=1 will "
                                             "return the default outcome for every case")

    return problems


def main(argv: list[str] | None = None) -> int:
    names = (argv if argv is not None else sys.argv[1:]) or available_domains()
    if not names:
        print("no domains found to lint")
        return 1

    problems: list[Problem] = []
    for name in names:
        problems += check_domain(name)

    errors = [p for p in problems if p.level == "ERROR"]
    warnings = [p for p in problems if p.level == "WARN"]
    for p in problems:
        print(p)
    print(f"\n{len(names)} domain(s) checked: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
