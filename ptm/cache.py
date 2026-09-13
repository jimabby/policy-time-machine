"""Judged this exact prompt before? Then do not pay to ask it again.

The loop this project is built around is *edit a clause, measure again*. As
shipped, the second measurement costs exactly what the first one did: a
threshold moved from 75 to 100 re-judges all six hundred cases, including the
hundreds the edit cannot possibly reach. At USD 4 a replay that is survivable
once and a habit nobody forms.

**The key is the prompt, not the case.** ``sha256(model + prompt)``. Keying on
``(case_id, policy_version)`` would be the obvious choice and would be quietly
wrong: editing a clause does not change the version label, so every stale
verdict would be served as though the policy had not moved. Hashing the prompt
means the policy text, the case payload, the rendering template, the outcome
list and the judge instructions are all in the key. Change any of them and the
entry misses, which is the behaviour you want from a cache standing between you
and a number you are going to act on.

**What must never be cached: stability.** Judging the same prompt repeatedly is
the whole of :mod:`ptm.stability` - a cache would serve the first answer every
time and report a 0% disagreement rate that is an artefact of this module
rather than a property of the judge. The stability DAG therefore does not carry
cache keys at all. Getting that wrong would not make the error bar wrong, it
would make it *reassuring*, which is worse.

Offline the judge is deterministic and free, so the cache saves nothing real.
It is still exercised there - identical code path, same merge, same accounting -
because a cache that is only ever used in the configuration nobody tests is a
cache that corrupts a replay the first time it is used for real.
"""

from __future__ import annotations

import hashlib
import os

from . import cost, store
from .models import Verdict

#: Set ``PTM_CACHE=0`` to make every lookup a miss without touching a DAG. The
#: entries are still written, so turning it back on picks up where it left off.
ENABLED = os.environ.get("PTM_CACHE", "1") == "1"

#: An arbitrary string mixed into every key, to invalidate the cache on purpose.
#:
#: The key is the prompt and the model identifier, which covers everything this
#: project controls: change the policy, the case, the template or the
#: instructions and the entry misses. It cannot cover the one thing this project
#: does *not* control - a vendor changing what sits behind an unchanged model
#: identifier. Nothing here can detect that, so the honest answer is a manual
#: lever, and this is it: bump ``PTM_CACHE_EPOCH`` and every prior answer stops
#: being found.
#:
#: Deliberately not ``PTM_CACHE=0``, which stops reads and keeps writing into a
#: keyspace you have decided you no longer trust, and deliberately not
#: ``cache_clear()``, which destroys the evidence. An epoch leaves the old
#: entries on disk, attributable and countable, and simply stops serving them -
#: so "what did the judge say before the model changed underneath us" is still
#: a question the database can answer.
EPOCH = os.environ.get("PTM_CACHE_EPOCH", "")


def key(prompt: str, model: str, epoch: str | None = None) -> str:
    """The cache key for one prompt under one model, in one epoch.

    All three matter. The same question put to a cheaper model is a different
    question, and serving one model's verdict as another's would make a
    model-comparison run agree with itself perfectly and mean nothing. The
    epoch is the operator saying the same model is no longer the same judge -
    see :data:`EPOCH`.
    """
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    digest.update(b"\x00")
    digest.update((EPOCH if epoch is None else epoch).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(prompt.encode("utf-8"))
    return digest.hexdigest()


def lookup(keys: list[str], count: bool = True) -> dict[str, Verdict]:
    """Cached verdicts by key, or nothing at all when the cache is off.

    ``count=False`` leaves the hit counters alone, for a read that is deciding
    what to judge rather than actually serving a verdict.
    """
    if not ENABLED or not keys:
        return {}
    return {
        k: Verdict(outcome=row["outcome"], rationale=row["rationale"],
                   confidence=row["confidence"], policy_clause=row["policy_clause"] or "")
        for k, row in store.cache_lookup(keys, count=count).items()
    }


def remember(domain: str, policy_version: str, model: str,
             verdicts: dict[str, tuple[str, int, Verdict]]) -> int:
    """Store fresh verdicts. ``verdicts`` maps case id to (key, prompt_chars, verdict).

    Written even when ``ENABLED`` is false: turning the cache off is a statement
    about what you are willing to *read*, usually because you no longer trust an
    entry, and it should not also throw away the run you just paid for.
    """
    return store.cache_put([
        {"cache_key": cache_key, "domain": domain, "policy_version": policy_version,
         "case_id": case_id, "judge_model": model, "outcome": v.outcome,
         "rationale": v.rationale, "confidence": v.confidence,
         "policy_clause": v.policy_clause, "prompt_chars": prompt_chars}
        for case_id, (cache_key, prompt_chars, v) in verdicts.items()
    ])


def split(items: list[dict], key_field: str = "cache_key",
          count: bool = True) -> tuple[list[dict], dict[str, Verdict]]:
    """Divide items into the ones that must be judged and the ones already answered.

    Returns ``(misses, hits_by_case_id)``. Items with no key on them are always
    misses - that is how the stability fan-out opts out, by simply not carrying
    one.
    """
    keys = [i[key_field] for i in items if i.get(key_field)]
    cached = lookup(keys, count=count)
    misses, hits = [], {}
    for item in items:
        k = item.get(key_field)
        if k and k in cached:
            hits[item["case_id"]] = cached[k]
        else:
            misses.append(item)
    return misses, hits


def saving(hit_items: list[dict], model: str, chars_field: str = "prompt_chars") -> dict:
    """What the hits did not cost, priced the same way the ledger prices a run.

    An estimate of an estimate, and labelled as one: it is the cost
    :mod:`ptm.cost` *would* have recorded for these prompts, which is measured
    from prompt size rather than read back from the vendor.
    """
    chars = sum(int(i.get(chars_field) or 0) for i in hit_items)
    priced = cost.estimate(chars, len(hit_items), model)
    return {"cache_hits": len(hit_items),
            "estimated_saved_usd": priced["estimated_cost_usd"]}


def describe(hits: int, misses: int, saved_usd: float = 0.0) -> str:
    total = hits + misses
    if not total:
        return "nothing to judge"
    line = (f"{hits} of {total} verdicts served from cache "
            f"({hits / total:.0%}), {misses} judged fresh")
    line += (f", saving an estimated USD {saved_usd:.2f}" if saved_usd else "")
    # Said out loud, because a run with an epoch set looks identical to a run
    # with a cold cache and the difference is a decision somebody made.
    if EPOCH:
        line += f"  [cache epoch {EPOCH!r}: entries from earlier epochs are not served]"
    return line
