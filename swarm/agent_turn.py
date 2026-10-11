"""The agent-turn contract: typed turn errors and model routing decisions.

Every dependency is injected; nothing in this module imports the daemon or
reads its globals.
"""
from __future__ import annotations

import datetime as dt
import random
from pathlib import Path


def pool(c, role=None, *, default_model):
    """The free models the swarm works with. Paid ids in `models` are dropped unless
    allow_paid is set, so a stray paid entry can never quietly spend credits.

    ``role_models`` can narrow a role to a proven subset of the general pool.  It
    deliberately narrows rather than extends ``models``: a role override cannot
    quietly introduce an unreviewed or paid provider.
    """
    ms = c.get("models") or [c.get("model") or default_model]
    scoped = (c.get("role_models") or {}).get(role) if role else None
    if scoped is not None:
        narrowed = [m for m in scoped if m in set(ms)]
        # An override that names nothing in the pool would leave the role with no model at
        # all, which reads as "every model is resting" and stalls the run for a typo.
        ms = narrowed or ms
    return [m for m in ms if c.get("allow_paid") or m.endswith(":free")]


def paid_pool(c):
    """Paid models to fall back on, from `paid_models`.

    These are deliberately kept out of `models`: the swarm works on free models and
    reaches for a paid one only when free capacity is gone (see `paid_stand_in`), so the
    bandit's scores stay comparable and the day's bill stays small."""
    if not c.get("allow_paid"):
        return []
    return [m for m in (c.get("paid_models") or []) if not m.endswith(":free")]


def paid_stand_in(c, model, *, paid_pool, spend_left):
    """The paid model to run this turn on instead of `model`, or None.

    A model's own paid twin is preferred — `x:free` and `x` are the same weights, so the
    turn continues with the model the bandit chose rather than a different one."""
    paid = paid_pool(c)
    if not paid:
        return None
    left = spend_left(c)
    if left is not None and left <= 0:
        return None
    twin = str(model)[:-len(":free")] if str(model).endswith(":free") else None
    return next((m for m in paid if m == twin), paid[0])


def quota_mode(c):
    """shutdown_on_quota_exhausted as one of 'shutdown' (default), 'wait', or 'paid'.

    A bare boolean is still accepted: true -> shutdown, false -> wait, matching this
    setting before it had a third option."""
    v = c.get("shutdown_on_quota_exhausted", True)
    if isinstance(v, bool):
        return "shutdown" if v else "wait"
    v = str(v).strip().lower()
    return v if v in ("shutdown", "wait", "paid") else "shutdown"


def quota_exhausted(c, budget, *, quota_mode, paid_pool, paid_stand_in):
    """True when the configured mode says to give up and stop the daemon, right now.

    The owner window is excluded on purpose: it is a scheduled quiet period the owner
    asked for, not the free tier running dry, and it ends on its own on a known clock.

    'wait' never gives up on its own — it waits for the daily reset, as this setting
    behaved before it had a third option.

    'paid' never gives up either, as long as this repo has any paid model configured
    at all: an empty wallet or a busy provider can clear on their own — a live config
    edit topping up the budget, a rate limit lifting — so it keeps retrying rather
    than needing a restart. It only gives up when there is no paid model to ever route
    to, which no amount of waiting can fix.

    'shutdown' (the default) gives up the moment free is out and paid can't help this
    instant."""
    ok, _, why = budget.check()
    if ok or "owner window" in why:
        return False
    mode = quota_mode(c)
    if mode == "wait":
        return False
    if mode == "paid":
        return not paid_pool(c)
    return not (budget.paid_would_help() and paid_stand_in(c, None))


def rest(ledger, e, *, config):
    """Rest the model behind a ProviderDown; returns a phrase for the log."""
    busy, gone = isinstance(e, ProviderBusy), isinstance(e, ModelGone)
    lines = [l for l in str(e).strip().splitlines() if l.strip()]
    why = lines[-1] if lines else ""
    until = ledger.cool(e.model, why, busy=busy, permanent=gone)
    if gone:
        # `swarm models --write` would put it back: OpenRouter lists it as free with tools,
        # and the refusal is particular to this key.
        return (f"{e.model} is not available to this API key — dropping it for this run. "
                f"Delete it from the \"models\" list in {config}: {why[:200]}")
    return (f"{e.model} {'is busy (rate-limited upstream)' if busy else 'is unavailable'}; "
            f"resting it until {dt.datetime.fromtimestamp(until):%H:%M:%S}")


def next_wake(ledger, c, *, pool):
    """(model, until) for the pool model that stops resting first, or (None, 0)."""
    live = set(pool(c))
    return next(((m, until) for m, until, _ in ledger.resting() if m in live), (None, 0))


def all_resting(ledger, c, *, next_wake):
    model, until = next_wake(ledger, c)
    if not model:
        return "every model is resting"
    return f"every model is resting; {model} is back at {dt.datetime.fromtimestamp(until):%H:%M:%S} (`swarm wake` ends the rest now)"


def mit_arm(c, rng=random):
    """Which side of the MIT-corpus experiment an attempt is on: "on" (excerpts in the prompt
    and the study tool), "off" (neither), or "none" when there is no corpus to test."""
    if not c.get("corpus_db") or not Path(c["corpus_db"]).expanduser().is_file():
        return "none"
    # A repository that does not want lecture excerpts in its prompts says so once, here. The
    # experiment's own switch does not mean that: with mit_experiment disabled every attempt is
    # on the corpus side, which is how 187 of 189 studio attempts got excerpts.
    if not c.get("inject_corpus", True):
        return "off"
    exp = c.get("mit_experiment") or {}
    if not exp.get("enabled", True):
        return "on"
    return "on" if rng.random() < float(exp.get("share_on", 0.5)) else "off"


class CapReached(Exception):
    pass


class NoCredits(Exception):
    pass


class StepLimit(Exception):
    pass


class ModelError(RuntimeError):
    """The model answered badly: malformed, empty or crashed turn."""


class ReviewFormatError(ModelError):
    """An invalid review is not an implementation failure."""


class AgentTimeout(Exception):
    """A role exceeded its wall-clock budget; partial work still needs verification."""

    def __init__(self, role, model, timeout, logfile):
        self.role, self.model = role, model
        self.timeout, self.logfile = timeout, logfile
        super().__init__(f"agent_timeout: {role} on {model} exceeded {timeout}s; log: {logfile}")


class ExplorationExhausted(Exception):
    """A turn spent 70% of its rounds without editing and was stopped early (work order §2)."""


class Stopped(RuntimeError):
    """The run was stopped mid-turn. The model did not fail and is not scored for it."""


class ProviderDown(Exception):
    """The model's providers are unavailable; not a judgement of its quality."""

    def __init__(self, model, msg):
        super().__init__(msg)
        self.model = model


class ProviderBusy(ProviderDown):
    """The model is rate-limited upstream: it works, but another model should take this turn."""


class ModelGone(ProviderDown):
    """This API key cannot use the model at all (403/404). Waiting will not bring it back."""


class RequestBudget(RuntimeError):
    """The bounded task or run pilot has spent its allotted requests."""


class ModelPoolExhausted(RuntimeError):
    """Every model in the pool is asleep or has failed enough times this run (work order §2)."""