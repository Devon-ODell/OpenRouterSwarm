#!/usr/bin/env python3
"""Route scoring (Phase 2, albatross `/route` parity).

Albatross scores coder candidates by eligibility, cost, selector score and warnings,
then picks the cheapest eligible one. This module is the same evaluation for a
question or a swarm task: given the configured model pool, each candidate's cached
price, and a small policy, it returns a ranked list with a reason per candidate.

`route select <task>` is the headline consumer: the editor types a task, the bridge
scores the pool, and the picker preselects the top candidate instead of the first
item in the list.
"""

from __future__ import annotations

# A candidate is worth roughly its cost in cents, but a free model that can answer
# is worth more than a paid one that can — the swarm runs on free requests.
_COST_WORST = 0.0        # inf cost sorts last
_FREE_BONUS = 2.0        # free models get a head start over paid ones of equal quality


def _price_of(model, catalog):
    """USD cost of one turn of this model, from the catalog cache when it carries one.

    The catalog rows are OpenRouter's /models entries (id + pricing), cached by the
    bridge; a missing pricing block means the price is unknown, not free.
    """
    for m in catalog or []:
        if m.get("id") == model:
            p = m.get("pricing") or {}
            try:
                prompt = float(p.get("prompt") or 0)
                completion = float(p.get("completion") or 0)
            except (TypeError, ValueError):
                return float("inf")
            return prompt * 3 + completion * 2
    return None


def _resolve_local(model):
    """True when the model resolves to a local backend (custom provider checks)."""
    try:
        from swarm import providers
        return bool(providers.resolve(model, default="ollama").get("local"))
    except Exception:
        try:
            import providers
            return bool(providers.resolve(model, default="ollama").get("local"))
        except Exception:
            return False


def score(model, catalog=None, policy=None, task=None):
    """A score in [0, 10] for one candidate, plus the reasons.

    Eligibility first: a policy that says local-only, or a max-turn budget, can
    remove a candidate outright. The remaining score is cost (lower is better)
    with a free-model bonus, flattened so a cheap paid model can still beat an
    expensive free one when both are eligible.
    """
    policy = policy or {}
    reasons = []
    score = 6.0

    if policy.get("localOnly") and not _resolve_local(model):
        return 0.0, ["policy localOnly excludes cloud models"]

    max_turn = policy.get("maxTurnUsd")
    if max_turn:
        price = _price_of(model, catalog)
        if price is not None and price > float(max_turn):
            return 0.0, [f"costs ${price:.4f}/turn, over the ${max_turn} cap"]

    price = _price_of(model, catalog)
    if price is None:
        reasons.append("price unknown")
        if (policy.get("unknownCost") or "warn") == "deny":
            return 0.0, reasons + ["policy denies unknown-cost models"]
        price = _COST_WORST
        score -= 1.0
        reasons.append("treated as worst-case cost")
    else:
        cost_score = 10.0 if price <= 0 else max(0.0, 10.0 - price * 40.0)
        score = 0.35 * cost_score + 0.30 * 10.0 + 0.35 * 6.0
        if price <= 0:
            score += _FREE_BONUS
            reasons.append("free")
        else:
            reasons.append(f"${price:.4f}/turn")
    if task:
        reasons.append("task-aware")
    return round(max(0.0, min(10.0, score)), 3), reasons


def rank(models, catalog=None, policy=None, task=None):
    """The pool ranked by score, best first, with reasons and price each."""
    scored = []
    for m in models or []:
        s, why = score(m, catalog=catalog, policy=policy, task=task)
        if s > 0:
            price = _price_of(m, catalog)
            scored.append({"model": m, "score": s, "reasons": why,
                           "price_usd": price if price is not None else None})
    scored.sort(key=lambda r: (-r["score"], (r["price_usd"] is None, r["price_usd"] or 0)))
    return scored


def select(models, catalog=None, policy=None, task=None):
    """The single best candidate, or None when the pool is empty or all are excluded."""
    ranked = rank(models, catalog=catalog, policy=policy, task=task)
    return ranked[0] if ranked else None