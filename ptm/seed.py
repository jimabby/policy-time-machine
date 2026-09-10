"""Generate a synthetic but internally consistent decision history.

Two properties matter for the demo:

1. The recorded outcomes are what a human following policy v1 would mostly
   have decided - with about 8% deviation, because real reviewers are
   inconsistent. Without that noise the replay is a pure rule diff and proves
   nothing.
2. Employee grade changes over time, and some employees are promoted to
   grade 3 late in the period. Policy v2 exempts grade 3+ from receipts, so
   replaying with *today's* grade silently approves claims that should have
   been denied. That is the point-in-time trap the engine is built to avoid.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from .config import load_domain
from .judge import offline_verdict
from .models import Case
from .store import conn, init_db

NAMES = [
    "A. Okafor", "B. Lindqvist", "C. Mwangi", "D. Ferreira", "E. Nakamura",
    "F. Costa", "G. Aziz", "H. Novak", "I. Delacroix", "J. Byrne",
    "K. Salvatore", "L. Petrov", "M. Haddad", "N. Olsen", "O. Ramaswamy",
    "P. Duarte", "Q. Fitzgerald", "R. Bergstrom", "S. Adeyemi", "T. Kowalski",
]
CATEGORIES = ["travel", "meals", "accommodation", "software", "client_entertainment"]


def seed_expenses(n_cases: int = 600, months: int = 24, seed: int = 7) -> dict:
    rng = random.Random(seed)
    domain = load_domain("expenses")
    init_db()

    end = datetime(2026, 9, 1)
    start = end - timedelta(days=months * 30)

    # --- employees, with grades that change over time -----------------------
    facts: list[tuple[str, str, str, str]] = []
    employees = []
    for i, name in enumerate(NAMES):
        sid = f"emp-{i:03d}"
        grade = rng.choice([1, 1, 2, 2, 2])
        facts.append((sid, "grade", str(grade), start.isoformat()))
        # Half are promoted late in the window, most of them into grade 3 -
        # the grade that policy v2 exempts from receipts. Their earlier claims
        # are the point-in-time trap.
        if rng.random() < 0.5:
            promo = start + timedelta(days=rng.randint(int(months * 30 * 0.6), months * 30 - 30))
            facts.append((sid, "grade", "3", promo.isoformat()))
        employees.append((sid, name))

    # --- cases ---------------------------------------------------------------
    rows = []
    for i in range(n_cases):
        sid, name = rng.choice(employees)
        decided_at = start + timedelta(days=rng.randint(0, months * 30), hours=rng.randint(9, 18))
        category = rng.choice(CATEGORIES)
        amount = round(abs(rng.gauss(90, 160)) + 8, 2)
        if category == "meals":
            amount = round(abs(rng.gauss(45, 22)) + 6, 2)
        if rng.random() < 0.07:
            amount = round(rng.uniform(500, 1800), 2)

        payload = {
            "case_id": f"exp-{i:04d}",
            "submitted_by": name,
            "category": category,
            "amount_gbp": amount,
            "receipt": "no" if rng.random() < 0.35 else "yes",
            "days_notice": rng.choice([0, 1, 3, 5, 8, 14, 21, 30]),
            "director_approval": "yes" if rng.random() < 0.18 else "no",
            "alcohol": "yes" if category == "client_entertainment" and rng.random() < 0.5 else "no",
            "client_driven": "yes" if rng.random() < 0.3 else "no",
            "note": _note(rng, category),
        }
        # Grade as of the decision date, resolved the same way replay does.
        payload["grade"] = _grade_as_of(facts, sid, decided_at)

        case = Case(
            case_id=payload["case_id"], domain="expenses", decided_at=decided_at,
            payload=payload, actual_outcome="approve",
        )
        v = offline_verdict(case, domain, "v1")
        outcome = v.outcome
        rationale = v.rationale
        # Human inconsistency: reviewers overrode the rulebook about 8% of the time.
        if rng.random() < 0.08:
            alt = [o for o in domain.outcomes if o != outcome]
            outcome = rng.choice(alt)
            rationale = rng.choice([
                "Approved at reviewer's discretion; long-standing employee.",
                "Reviewer applied a stricter reading than the policy requires.",
                "Escalated to finance lead, who overrode the default outcome.",
            ])

        payload.pop("grade")  # grade is a point-in-time fact, not part of the case record
        rows.append((
            case.case_id, "expenses", sid, decided_at.isoformat(),
            json.dumps(payload), outcome, rationale,
        ))

    with conn() as c:
        c.execute("DELETE FROM cases WHERE domain = 'expenses'")
        c.executemany(
            "INSERT INTO cases (case_id, domain, subject_id, decided_at, payload, actual_outcome, actual_rationale) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        c.executemany(
            "INSERT OR REPLACE INTO subject_facts (subject_id, key, value, known_from) VALUES (?,?,?,?)",
            facts,
        )
    return {"cases": len(rows), "employees": len(employees), "facts": len(facts)}


def _grade_as_of(facts, sid: str, when: datetime) -> int:
    applicable = [int(v) for s, k, v, kf in facts if s == sid and k == "grade" and datetime.fromisoformat(kf) <= when]
    return applicable[-1] if applicable else 1


def _note(rng: random.Random, category: str) -> str:
    return rng.choice({
        "travel": ["Client moved the meeting forward.", "Rail replacement, had to taxi.", "Booked once the deal was confirmed."],
        "meals": ["Team dinner after the launch.", "Working lunch with the vendor.", "Overnight, evening meal."],
        "accommodation": ["Two nights on site.", "Hotel near the client office.", "Extended stay, snow."],
        "software": ["Annual licence renewal.", "Seat for the new starter.", "One-off tooling purchase."],
        "client_entertainment": ["Dinner with the client team.", "Post-signing drinks.", "Hosted the buyer."],
    }[category])




MEMBERS = [
    "Ashworth", "Baptiste", "Chowdhury", "Delgado", "Eriksen", "Fontaine",
    "Gallagher", "Hasegawa", "Ivanova", "Jankowski", "Kaur", "Lombardi",
    "Marchetti", "Nwosu", "Ostrowski", "Pereira", "Quinlan", "Rasmussen",
]


def seed_refunds(n_cases: int = 400, months: int = 24, seed: int = 11) -> dict:
    """Second domain, to prove the engine carries no domain knowledge.

    The point-in-time trap here is membership tier: policy v2 exempts premium
    members from the minimum-term forfeit, and members upgrade over time.
    """
    rng = random.Random(seed)
    domain = load_domain("refunds")
    init_db()

    end = datetime(2026, 9, 1)
    start = end - timedelta(days=months * 30)

    facts, members = [], []
    for i, name in enumerate(MEMBERS):
        sid = f"mem-{i:03d}"
        facts.append((sid, "tier", "standard", start.isoformat()))
        if rng.random() < 0.45:
            up = start + timedelta(days=rng.randint(int(months * 30 * 0.5), months * 30 - 20))
            facts.append((sid, "tier", "premium", up.isoformat()))
        members.append((sid, name))

    rows = []
    for i in range(n_cases):
        sid, name = rng.choice(members)
        decided_at = start + timedelta(days=rng.randint(0, months * 30), hours=rng.randint(9, 19))
        contract = rng.choice([12, 12, 12, 6, 24])
        elapsed = rng.randint(1, contract + 8)
        reason = rng.choice(["dissatisfaction", "financial", "medical", "relocation", "other"])
        payload = {
            "case_id": f"ref-{i:04d}",
            "member": name,
            "contract_months": contract,
            "months_elapsed": elapsed,
            "notice_days": rng.choice([0, 3, 7, 10, 14, 21, 30, 45]),
            "reason": reason,
            "distance_moved_miles": rng.choice([0, 0, 5, 12, 18, 30, 120]) if reason == "relocation" else 0,
            "documentation": "yes" if rng.random() < 0.55 else "no",
            "outstanding_balance_gbp": round(max(0, contract - elapsed) * rng.uniform(28, 62), 2),
            "note": rng.choice([
                "Moving for work.", "Injured my shoulder.", "Can't afford it any more.",
                "Never use it.", "Classes I joined for were cancelled.",
            ]),
        }
        payload["tier"] = _tier_as_of(facts, sid, decided_at)

        case = Case(case_id=payload["case_id"], domain="refunds", decided_at=decided_at,
                    payload=payload, actual_outcome="no_refund")
        v = offline_verdict(case, domain, "v1")
        outcome, rationale = v.outcome, v.rationale
        if rng.random() < 0.09:
            outcome = rng.choice([o for o in domain.outcomes if o != outcome])
            rationale = rng.choice([
                "Manager waived the forfeit as a goodwill gesture.",
                "Retention team overrode after the member threatened a chargeback.",
                "Front desk applied a stricter reading than the policy requires.",
            ])

        payload.pop("tier")
        rows.append((case.case_id, "refunds", sid, decided_at.isoformat(),
                     json.dumps(payload), outcome, rationale))

    with conn() as c:
        c.execute("DELETE FROM cases WHERE domain = 'refunds'")
        c.executemany(
            "INSERT INTO cases (case_id, domain, subject_id, decided_at, payload, actual_outcome, actual_rationale) VALUES (?,?,?,?,?,?,?)",
            rows)
        c.executemany(
            "INSERT OR REPLACE INTO subject_facts (subject_id, key, value, known_from) VALUES (?,?,?,?)",
            facts)
    return {"cases": len(rows), "members": len(members), "facts": len(facts)}


def _tier_as_of(facts, sid: str, when: datetime) -> str:
    vals = [v for s, k, v, kf in facts if s == sid and k == "tier" and datetime.fromisoformat(kf) <= when]
    return vals[-1] if vals else "standard"


def seed_all() -> dict:
    return {"expenses": seed_expenses(), "refunds": seed_refunds()}


if __name__ == "__main__":
    print(seed_all())
