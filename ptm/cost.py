"""What a replay costs to judge.

A backfill across two years of history is thousands of model calls, so "what
will this cost me?" deserves an answer before the bill arrives rather than
after. Every replay records its own spend, and :func:`estimate_backfill` prices
a proposed backfill from the case count alone.

Honest about its own precision: the ``estimated_*`` counts here are **measured
from the prompts this project actually built** and converted at a fixed
characters-per-token ratio. That makes them an estimate with a known bias
(tokenisers vary by model and by language), which is why the fields say so. It
is the right precision for "can I afford this backfill" and the wrong precision
for reconciling an invoice.

It no longer has to stay a guess. With ``PTM_OFFLINE=0`` the judge tasks run
through :mod:`ptm.metered`, which keeps the token counts the vendor itself
reported, and :func:`from_usage` prices those as ``actual_*``. Both go in the
ledger: :func:`reconcile` is the one that matters, because the gap between them
says how wrong the forecast was *and* what characters-per-token ratio would
have made it right for your policies.

Prices are USD per million tokens and live in one table below. ``PTM_PRICE``
overrides it without touching code, for when the table has gone stale and you
would rather not wait for a release:

    PTM_PRICE=3.00/15.00    # input/output USD per million tokens

It applies to **every** model rather than to a named one. That is the right
shape for what it is for - one run, one judge, a price the table got wrong -
and the wrong shape for maintaining a price list, which belongs in the table.
"""

from __future__ import annotations

import os

#: USD per million tokens, (input, output). Kept deliberately short: this is a
#: demo's price list, not a billing system, and a stale entry here costs an
#: estimate's accuracy rather than money.
PRICES: dict[str, tuple[float, float]] = {
    "anthropic:claude-opus-5": (5.00, 25.00),
    "anthropic:claude-sonnet-5": (3.00, 15.00),
    "anthropic:claude-haiku-4-5": (1.00, 5.00),
    "openai:gpt-4.1": (2.00, 8.00),
    "openai:gpt-4.1-mini": (0.40, 1.60),
    "google:gemini-2.5-pro": (1.25, 10.00),
}
DEFAULT_PRICE = (3.00, 15.00)

#: Characters per token. Four is the usual English-prose approximation and is
#: close enough for budgeting; policy markdown and case tables are prose-like.
CHARS_PER_TOKEN = 4.0

#: A ``Verdict`` is a short structured object - a rationale of two or three
#: sentences plus three scalars. Measured across the shipped fixtures it lands
#: near this, and the output price is the expensive half, so it is worth not
#: guessing wildly.
RESPONSE_CHARS = 420


def price_for(model: str) -> tuple[float, float]:
    """Input/output USD per million tokens for a model identifier.

    ``PTM_PRICE`` overrides every model, for the case where the price list here
    has gone stale and you would rather not wait for a release.
    """
    override = os.environ.get("PTM_PRICE")
    if override:
        try:
            raw_in, raw_out = override.split("/")
            return float(raw_in), float(raw_out)
        except (ValueError, TypeError):
            pass  # a malformed override must not break a replay
    return PRICES.get(model, DEFAULT_PRICE)


def tokens(chars: int) -> int:
    return int(round(chars / CHARS_PER_TOKEN))


def estimate(prompt_chars: int, requests: int, model: str,
             response_chars: int | None = None) -> dict:
    """Price one task's worth of judging.

    ``prompt_chars`` is the total across every prompt sent, so this works the
    same for one case or for a mapped fan-out of six hundred.
    """
    in_price, out_price = price_for(model)
    out_chars = RESPONSE_CHARS * requests if response_chars is None else response_chars
    in_tokens, out_tokens = tokens(prompt_chars), tokens(out_chars)
    cost = (in_tokens * in_price + out_tokens * out_price) / 1_000_000
    return {
        "estimated_requests": requests,
        "estimated_input_tokens": in_tokens,
        "estimated_output_tokens": out_tokens,
        "estimated_cost_usd": round(cost, 4),
        "judge_model": model,
    }


def from_usage(rows: list[dict], model: str) -> dict:
    """Price what the vendor says actually happened. See :mod:`ptm.metered`.

    ``rows`` are the usage dicts a metered judge task pushed - requests, input
    tokens and output tokens as counted by the model, not by us. The shape
    matches :func:`estimate` so the two can sit in one ledger and be compared
    field by field; the names carry ``actual_`` rather than ``estimated_``
    because this is the only number here that was measured rather than derived.
    """
    requests = sum(int(r.get("requests") or 0) for r in rows)
    in_tokens = sum(int(r.get("input_tokens") or 0) for r in rows)
    out_tokens = sum(int(r.get("output_tokens") or 0) for r in rows)
    in_price, out_price = price_for(model)
    return {
        "actual_requests": requests,
        "actual_input_tokens": in_tokens,
        "actual_output_tokens": out_tokens,
        "actual_cost_usd": round((in_tokens * in_price + out_tokens * out_price) / 1_000_000, 4),
        "measured_calls": len([r for r in rows if r]),
    }


def reconcile(ledger: dict, prompt_chars: int = 0) -> dict:
    """The estimate against the measurement, and what the gap implies.

    Two numbers come out of this and they answer different questions. ``error``
    is how wrong the forecast was, which is what somebody deciding whether to
    launch a backfill needs. ``implied_chars_per_token`` is *why* it was wrong:
    the ratio that would have made the estimate right for these prompts, against
    the :data:`CHARS_PER_TOKEN` of 4 the estimate assumed. A policy in a
    language that tokenises badly, or one heavy with markdown punctuation, moves
    that ratio a long way - and until something measured it, the four was a
    constant nobody could be shown to be wrong about.

    Returns ``{"measured": False}`` when nothing was measured, rather than a
    tidy zero: an offline run and a run whose usage reporting failed must not
    read as a forecast that came in exactly right.
    """
    actual_in = int(ledger.get("actual_input_tokens") or 0)
    actual_cost = float(ledger.get("actual_cost_usd") or 0)
    if not actual_in and not actual_cost:
        return {"measured": False,
                "hint": "no usage reported; the ledger is the estimate alone. Usage is "
                        "collected by ptm.metered and is only available with PTM_OFFLINE=0.",
                "hint_key": "hint.cost_unmetered"}
    estimated_in = int(ledger.get("estimated_input_tokens") or 0)
    estimated_cost = float(ledger.get("estimated_cost_usd") or 0)
    return {
        "measured": True,
        "estimated_cost_usd": round(estimated_cost, 4),
        "actual_cost_usd": round(actual_cost, 4),
        "cost_error_usd": round(actual_cost - estimated_cost, 4),
        "cost_error": round((actual_cost - estimated_cost) / estimated_cost, 4)
        if estimated_cost else None,
        "estimated_input_tokens": estimated_in,
        "actual_input_tokens": actual_in,
        "token_error": round((actual_in - estimated_in) / estimated_in, 4)
        if estimated_in else None,
        "assumed_chars_per_token": CHARS_PER_TOKEN,
        # The ratio that would have made the estimate right. Only computable
        # when the caller knows how many characters it sent, which is why it is
        # an argument rather than derived back out of the token count.
        "implied_chars_per_token": round(prompt_chars / actual_in, 2)
        if prompt_chars and actual_in else None,
        "note": "actual_* are the vendor's counts; estimated_* are this project's, "
                "measured from prompt size. The gap is the forecast's error, not a "
                "second bill.",
    }


def zero(model: str = "offline") -> dict:
    """The offline judge's ledger entry. It costs nothing, and says so."""
    return {
        "estimated_requests": 0,
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "judge_model": model,
    }


def estimate_backfill(n_cases: int, policy_chars: int, case_chars: int,
                      model: str) -> dict:
    """Price a backfill before launching it.

    Every prompt carries the whole policy plus one rendered case, so the total
    is dominated by ``n_cases * policy_chars`` - which is exactly why a cheap
    model and a long policy can be the wrong trade.
    """
    per_prompt = policy_chars + case_chars + 800  # 800 ~= the fixed instructions
    return estimate(per_prompt * n_cases, n_cases, model)
