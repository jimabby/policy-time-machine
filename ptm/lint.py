"""Check a domain's configuration against the policies it claims to implement.

Two kinds of drift this catches, both silent at runtime:

**A fixture citing a clause the policy does not contain.** ``offline_rules`` are
maintained by hand, next to but separate from the markdown policy. Nothing
stops them diverging, and when they do, the offline demo stops implementing the
policy it says it implements while still producing confident output.

**A rule referencing a field that does not exist.** This is the dangerous one.
:func:`ptm.judge.offline_verdict` deliberately swallows exceptions, so a rule
that says ``grade`` where the payload says ``employee_grade`` does not crash -
it simply never matches, and the replay quietly comes out wrong. The same check
now also refuses a rule written in constructs :mod:`ptm.safe_eval` will not
evaluate, which fails in exactly the same silent way.

**A rule an earlier rule has already eaten.** Rules are tried in order and the
first match decides the case, so a general restriction written above the
exemption it was meant to carve out of makes that exemption unreachable. It
parses, it reads real fields, it cites a real clause - and it never fires, so
its clause is never cited and every case it was written for is decided by
something else. :func:`ptm.safe_eval.shadowed` proves it statically; the failure
is silent in exactly the same way as the one above, and now arrives from a model
as well as from a person, because ``propose_<domain>`` writes these.

    python -m ptm.lint            # every domain
    python -m ptm.lint expenses   # one domain

Exits non-zero if any ERROR is found. Warnings do not fail the lint: some are
legitimate, such as a discretion clause that no mechanical rule can express.
"""

from __future__ import annotations

import string
import sys
from dataclasses import dataclass

from . import cli, preflight
from . import sweep as sweep_engine
from .config import DomainConfig, available_domains, load_domain
from .judge import SAFE_BUILTINS
from .safe_eval import check_expression, names_in
from .safe_eval import shadowed as shadowed_rules


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


#: Kept as the module's own name because the lint's reader expects it here.
#: The implementation lives in :mod:`ptm.safe_eval` alongside the evaluator, so
#: "which fields does this read" and "will this actually run" can never answer
#: from two different ideas of what a rule expression is.
_names_in = names_in


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
    # --- the disparity check's own configuration -------------------------
    for field in domain.disparity.fields:
        if field not in domain.segment_fields:
            err("disparity.fields", f"{field!r} is not in segment_fields, so no blast "
                                    f"radius row exists for it and it would never be "
                                    f"compared")
    if domain.disparity.gate not in {"warn", "fail"}:
        err("disparity.gate", f"{domain.disparity.gate!r} is not 'warn' or 'fail'")
    if domain.disparity.max_ratio <= 1:
        err("disparity.max_ratio", f"{domain.disparity.max_ratio} means every segment "
                                   f"moving at all is a finding, which is the same as "
                                   f"having no check")
    if domain.disparity.min_cases < 2:
        warn("disparity.min_cases", f"{domain.disparity.min_cases} compares segments with "
                                    f"almost no cases in them; one case out of one is a "
                                    f"100% flip rate and no evidence at all")

    if domain.rules.gate not in {"warn", "fail"}:
        err("rules.gate", f"{domain.rules.gate!r} is not 'warn' or 'fail'")
    # Not `name`: that is this function's domain argument, which err() and warn()
    # close over, and rebinding it here relabelled every later finding with a
    # field name instead of the domain it came from.
    for setting in ("min_outcome_agreement", "min_clause_agreement"):
        value = getattr(domain.rules, setting)
        if not 0.0 <= value <= 1.0:
            err(f"rules.{setting}", f"{value} is outside 0..1")
        elif value == 1.0:
            warn(f"rules.{setting}", "1.0 requires the offline rules to match the judge "
                                     "on every single case, which no real judge will do - "
                                     "the gate would fail permanently rather than catch "
                                     "drift")

    if domain.calibration.gate not in {"warn", "fail"}:
        err("calibration.gate", f"{domain.calibration.gate!r} is not 'warn' or 'fail'")
    if not 0.0 <= domain.calibration.min_accuracy <= 1.0:
        err("calibration.min_accuracy",
            f"{domain.calibration.min_accuracy} is outside 0..1")
    elif domain.calibration.min_accuracy == 1.0:
        warn("calibration.min_accuracy",
             "1.0 requires the judge to agree with every human ruling on file. Precedents "
             "are the contested cases by construction, so this fails permanently rather "
             "than catching a judge that has got worse")
    if not 0.0 <= domain.calibration.max_overconfidence <= 1.0:
        err("calibration.max_overconfidence",
            f"{domain.calibration.max_overconfidence} is outside 0..1")
    if domain.calibration.min_judged < 1:
        err("calibration.min_judged",
            f"{domain.calibration.min_judged} would score the judge on no rulings at all")
    elif domain.calibration.min_judged < 5:
        warn("calibration.min_judged",
             f"{domain.calibration.min_judged} gates on a handful of contested cases, "
             f"whose accuracy band runs most of the way from 0 to 1; the gate would fire "
             f"on the sample size rather than on the judge")

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
        if version in domain.draft_versions:
            # Not a fault - drafts are meant to be here. Named because a lint
            # that lists a machine's draft alongside approved policy without
            # saying which is which is the one place this project could quietly
            # launder one into the other.
            warn(where, "is a drafted amendment from include/drafts/, written by "
                        "ptm.proposal and approved by nobody. Replay and gate it like any "
                        "candidate; delete it with ptm.proposal.discard when done with it.")
        # The structural problems the real judge would hit, reported here so one
        # command covers both halves of the drift. See ptm/preflight.py.
        for finding in preflight.structural(domain, version):
            (err if finding.severity == "error" else warn)(
                where, f"[{finding.kind}] {finding.detail}")

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
            # One check, covering both halves: a field that does not exist, and
            # a construct the evaluator will not run. The second used to be
            # unreachable here - the old name-level scan saw nothing wrong with
            # an expression made entirely of attribute access, and the rule then
            # failed silently at judging time, which is the failure this lint is
            # for. See ptm/safe_eval.py.
            for problem in check_expression(expression, known):
                err(at, f"'when' {problem}. Known fields: {sorted(known)}")

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

        # A rule stating a band rather than a threshold. Not a fault in the rule
        # - a band is often exactly what the policy says - but ptm.sweep moves
        # every literal a field is compared against, so sweeping this dial
        # rewrites both ends to the same number and the rule then matches
        # nothing at any point on the curve. The sweep refuses such a dial
        # outright; this is where somebody finds out before they ask for one.
        for row in sweep_engine.collapsing(domain, version):
            warn(f"{where}[{row['rule_index']}]",
                 f"compares {row['field']!r} against {row['values']} in one rule. That is "
                 f"a band, not a threshold, so {row['field']!r} cannot be swept in clause "
                 f"{row['clause'] or '-'} - ptm.sweep would collapse both ends onto one "
                 f"number and draw a curve for a rule that fires on nothing. Split it "
                 f"across two rules if you want to move either end.")

        # A rule an earlier rule makes unreachable. Every other check in this
        # module passes it - it parses, it reads real fields, it cites a real
        # clause - and it decides nothing, so its outcome is never produced and
        # its clause never cited. An error when the two rules disagree, because
        # then the replay is reporting a different answer from a different
        # sentence for every case the dead rule was written to catch; a warning
        # when they agree, because that is only dead weight.
        for row in shadowed_rules(rules):
            at = f"{where}[{row['rule_index']}]"
            message = (f"can never fire: rule[{row['shadowed_by']}] "
                       f"({row['shadowed_by_expression']!r}) already matches every case "
                       f"{row['expression']!r} would")
            if row["harmless"]:
                warn(at, f"{message}, and gives the same outcome from the same clause - "
                         f"so it is dead weight rather than a wrong answer. Delete it.")
            else:
                err(at, f"{message}, and decides them {row['shadowed_by_outcome']!r} from "
                        f"clause {row['shadowed_by_clause'] or '-'} instead of "
                        f"{row['outcome']!r} from clause {row['clause'] or '-'}. Move the "
                        f"specific rule above the general one.")

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


USAGE = """usage:
  python -m ptm.lint [domain ...]

Check every domain YAML against the policies it claims to implement: rules
reading fields no case has, rules citing clauses the policy does not contain,
rules an earlier rule makes unreachable, outcomes no rule can reach, dials a
sweep would collapse rather than move.

  domain ...   defaults to every domain in include/domains/

Exits non-zero on an error. Warnings are reported and do not fail."""


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0
    names = args or available_domains()
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
