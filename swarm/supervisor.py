"""Daemon lifecycle: who holds the daemon lock, deadlines, draining, keeping the machine
awake, and reconciling the free allowance with OpenRouter.

Every dependency is injected; nothing here imports the daemon or reads its globals.
"""
from __future__ import annotations

import datetime as dt, os, shutil, subprocess, sys, time


def allowance_recheck(c):
    """Seconds between asking OpenRouter whether a spent free allowance is back; 0 or unset is off."""
    try:
        return max(0, int(c.get("allowance_recheck") or 0))
    except (TypeError, ValueError):
        return 0


def recheck_allowance(c, budget, *, recheck_lock, rechecked, account, journal, log,
                      allowance_recheck):
    """While the free allowance looks spent, ask OpenRouter whether it really is.

    The local count only climbs between UTC midnights: flint counts every request it starts,
    including failed ones OpenRouter never charged, and `sync_usage` keeps the larger of the
    two counts. Once it passes the cap the swarm pays for, or idles through, every turn until
    midnight, even while OpenRouter still has free requests for this key. At most every
    `allowance_recheck` seconds this adopts OpenRouter's count instead and lifts a daily-cap
    block OpenRouter no longer backs. True when free turns can run again."""
    every = allowance_recheck(c)
    if not every or not budget.paid_would_help():
        return False            # off, within the allowance, or paused for the owner's window
    with recheck_lock:
        if time.time() - rechecked["at"] < every:
            return False
        rechecked["at"] = time.time()
        q = (account() or {}).get("free_model_daily_requests") or {}
        used, limit = q.get("used"), q.get("limit")
        if not isinstance(used, int) or not isinstance(limit, int):
            return False
        import flint as F

        def adopt(d):
            counted = d.get("count", 0)
            d["count"] = used
            if used < limit:
                d.pop("blocked_until", None)
            return counted
        counted = F.Throttle()._txn(adopt)
        if budget.check()[0]:
            rechecked["said"] = False
            log(f"free allowance is back: OpenRouter reports {used}/{limit} used "
                f"(counted {counted} here); free models again")
            journal("allowance_restored", used=used, limit=limit, counted=counted)
            return True
        if not rechecked["said"]:
            rechecked["said"] = True
            log(f"free allowance spent: OpenRouter reports {used}/{limit} used; "
                f"rechecking every {every / 60:g}m")
        return False


def resync_allowance(c, budget, *, account, journal, log):
    """Full allowance reconciliation at a run boundary, unconditionally, once.

    The local counter only climbs between UTC midnights: flint counts every request it
    starts, including failed ones OpenRouter never charged, and failed requests that were
    charged by the provider can make the local count exceed what OpenRouter reports used.
    A stale local counter is the one thing that can freeze the swarm while free capacity
    exists: `allowance_recheck` only engages once the budget already looks spent and at its
    own throttled interval, so on startup the swarm can stall — or jump to paid models — for
    a number this key's owner never actually used. This is the startup / per-`swarm run`
    sync: the provider's daily free-request counter is authoritative, and it is applied
    once (no rate limiting, no budget gate) so every later check starts from a honest count.

    Returns True when the resync made a change (count corrected or a daily-cap block
    lifted), False when the provider was unreachable or already agreed with the local
    counter."""
    info = account()
    q = (info or {}).get("free_model_daily_requests") or {}
    used, limit = q.get("used"), q.get("limit")
    if not isinstance(used, int):
        log("allowance resync: could not read OpenRouter's free-request counter; "
            "keeping the local count")
        return False
    import flint as F

    def step(d):
        changed = False
        before = d.get("count", 0)
        # The provider is authoritative when the local counter has climbed above it —
        # flint counts failed requests the provider never charged, and that overshoot is
        # the stall this resync exists to undo. Never *raise* the local count to meet a
        # provider number *higher* than ours: requests made elsewhere with this key are
        # already adopted by `sync_usage`'s max, and a wrong read must not shorten the day.
        if used < before:
            d["count"] = used
            changed = True
        block_lifted = bool(d.get("blocked_until")) and isinstance(limit, int) and used < limit
        if block_lifted:
            d.pop("blocked_until", None)
            changed = True
        return changed, before, int(d.get("count", before)), block_lifted

    try:
        changed, before, after, block_lifted = F.Throttle()._txn(step)
    except Exception as e:
        log(f"allowance resync: could not write the local counter ({type(e).__name__}: {e}); "
            "keeping the local count")
        return False
    if changed:
        log(f"allowance resync: OpenRouter reports {used}/{limit} free requests used today "
            f"(local count was {before}); free turns run under the provider's number")
        journal("allowance_resynced", used=used, limit=limit, counted_before=before,
                counted_after=after, block_lifted=block_lifted)
    return changed


def drain(tally, stop=True, *, run, run_lock, journal, log, drain_reason):
    """Finish the tasks in flight, then stop — or, with stop=False, then restart.

    54 attempts died mid-turn in this window because stopping meant SIGINT, and 5.6 hours of
    model work went with them."""
    if stop:
        drain_reason["stop_requested"] = True
    with run_lock:
        run.update(state="Draining", stop_reason="Drain requested" if stop else "Source-change restart")
    if not tally.drain.is_set():
        tally.drain.set()
        log("draining: finishing the task(s) in flight, then "
            + ("stopping" if stop else "restarting"))
        journal("drain_requested", stop=stop)


def keep_awake(*, log):
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("caffeinate: this Mac will not idle-sleep while the swarm runs "
            "(closing the lid on battery still sleeps it)")


def holder(*, daemon_note):
    """How to watch or stop the daemon already running here."""
    d = daemon_note()
    if not d:
        return ""
    return (f"\n  It started at {dt.datetime.fromtimestamp(d['started']):%H:%M:%S} with the goal: "
            f"{str(d.get('goal', ''))[:120]}\n"
            f"  Watch it:  swarm status\n"
            f"  Stop it:   kill -INT {d['pid']}   (or Ctrl-C in its terminal)")


def hours_left(hours):
    """Hours until this shift ends, honouring a deadline carried across a self-restart.

    Without this a daemon that restarts at hour 7 of 8 would run another full 8 hours, and a
    run that restarts often would never end at all."""
    deadline = os.environ.get("FLINT_RUN_DEADLINE")
    if deadline:
        try:
            left = (float(deadline) - time.time()) / 3600
        except ValueError:
            return hours
        if left <= 0:
            return None                      # the shift is over; caller decides what that means
        if hours is None or left < hours:
            return left
    return hours