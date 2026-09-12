"""What a replay costs to judge.

A backfill across two years of history is thousands of model calls, so "what
will this cost me?" deserves an answer before the bill arrives rather than
after. Every replay records its own spend, and :func:`estimate_backfill` prices
a proposed backfill from the case count alone.

Honest about its own precision: the token counts here are **measured from the
prompts this project actually built** and converted at a fixed characters-per-
token ratio, not read back from the vendor's usage reporting. That makes them
an estimate with a known bias (tokenisers vary by model and by language), which
is why every field and column is named ``estimated_*``. It is the right
precision for "can I afford this backfill" and the wrong precision for
reconciling an invoice.

Prices are USD per million tokens and live in one table below. Override a model
or add one without touching code via ``PTM_PRICE_<IN>_<OUT>``:

    PTM_PRICE=3.00/15.00    # input/output USD per million tokens
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
    per_prompt = policy_chars + case_chars + 800  # 800 ≈ the fixed instructions
    return estimate(per_prompt * n_cases, n_cases, model)
