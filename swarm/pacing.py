#!/usr/bin/env python3
"""Second-by-second budget sizing for role turns, from measured latency.

Sizes turns from measured latency, not configured hope.

A Dots turn configured for 16 rounds inside a 900 s timeout spent most of its
rounds reading and never got to the edit, because at ~45–60 s a round the
configuration left no time for a final edit, tests or a final answer. This module
computes an affordable round limit from an EWMA of seconds-per-request per model
and role (persisted in learn.json), reserving finalization time for the roles
whose product is a diff.
"""
import time

# A final answer, the edit-only round and the last writes: what a turn needs after
# its last model request. Implement/repair roles write a diff; read-only roles just
# answer, so they can spend almost the whole budget on requests.
IMPL_RESERVE = 180.0
READ_ONLY_RESERVE = 60.0

# The EWMA lives here so swarmd does not have to import learn's ledger for one number.
DEFAULT_SECONDS_PER_REQUEST = 47.0  # measured on the studio's own turns


def reserve_for_role(role, config_seconds=None):
    """The finalization reserve, in seconds, for a role."""
    if role in ("implementer", "repair"):
        return float(config_seconds if config_seconds is not None else IMPL_RESERVE)
    return float(config_seconds if config_seconds is not None else READ_ONLY_RESERVE)


def affordable_steps(timeout, seconds_per_request, reserve, max_steps, min_steps=1):
    """The largest step budget that fits the timeout after finalization reserve.

    Never raises the configured maximum; a fast model may keep its configured
    budget when it safely fits. A zero/negative budget means the timeout cannot
    even afford one request after the reserve; the caller decides what to do.
    """
    max_steps = max(0, int(max_steps or 0))
    if timeout <= 0 or seconds_per_request <= 0:
        return max_steps
    affordable = int((timeout - reserve) // seconds_per_request)
    return max(min_steps, min(max_steps, affordable)) if max_steps else 0


def ewma_update(old, new, alpha=0.2):
    """Exponential moving average of seconds per request. None if there is no prior."""
    if old is None:
        return float(new)
    return alpha * float(new) + (1.0 - alpha) * float(old)


class LatencyLearner:
    """Persist seconds-per-request EWMA per (model, role) in learn.json.

    The ledger's own file is the natural home: it already persists across runs and
    is the place the swarm keeps what it has measured. We write only the `latency`
    key and leave everything else to the Ledger's transaction.
    """

    KEY = "latency"

    def __init__(self, ledger):
        self.ledger = ledger

    def seconds_per_request(self, model, role):
        with self.ledger.txn(write=False) as d:
            row = (d.get(self.KEY) or {}).get(model, {}).get(role)
        if not isinstance(row, dict) or not isinstance(row.get("s"), (int, float)):
            return None
        return float(row["s"])

    def update(self, model, role, secs, requests):
        if not model or not role or requests <= 0:
            return None
        per = max(0.0, float(secs)) / requests
        with self.ledger.txn() as d:
            table = d.setdefault(self.KEY, {}).setdefault(model, {})
            prior = table.get(role, {}).get("s")
            table[role] = {"s": ewma_update(prior, per), "n": table.get(role, {}).get("n", 0) + 1,
                           "at": time.time()}
        return table[role]["s"]


def effective_steps(role, model, timeout, configured_max, ledger=None,
                    seconds_per_request=None):
    """The step budget a turn should actually get.

    Uses the measured EWMA when available, falls back to the configured constant,
    and always clamps to the configured maximum.
    """
    max_steps = max(0, int(configured_max or 0))
    if max_steps == 0:
        return 0
    spr = seconds_per_request
    if spr is None and ledger is not None:
        spr = LatencyLearner(ledger).seconds_per_request(model, role)
    if spr is None:
        spr = DEFAULT_SECONDS_PER_REQUEST
    reserve = reserve_for_role(role)
    return affordable_steps(timeout, spr, reserve, max_steps)