"""Daemon lifecycle: who holds the daemon lock, deadlines, draining, keeping the machine
awake, and reconciling the free allowance with OpenRouter.

Every dependency is injected; nothing here imports the daemon or reads its globals.
"""
from __future__ import annotations

import datetime as dt, json, os, shutil, signal, subprocess, sys, threading, time, uuid
import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from budget import Budget
from learn import Ledger
from metrics import Reconcile

try:  # one class identity with the daemon's turn errors
    from .agent_turn import CapReached, ModelError, NoCredits, ProviderDown, StepLimit
except ImportError:  # pragma: no cover - script mode
    from agent_turn import CapReached, ModelError, NoCredits, ProviderDown, StepLimit


@dataclass(frozen=True)
class SupervisorDeps:
    """The daemon services the lifecycle uses, read when start() or run_daemon() is entered."""
    log: Callable
    journal: Callable
    account: Callable
    preflight: Callable
    read_goal: Callable
    run_status: Callable
    shutdown: Callable
    holder: Callable
    hours_left: Callable
    keep_awake: Callable
    run_daemon: Callable
    queue_for: Callable
    note_interrupted: Callable
    prune_attempts: Callable
    trunk_name: Callable
    config_kind: Callable
    resync_allowance: Callable
    pool: Callable
    config_mtime: Callable
    source_fingerprint: Callable
    source_changed: Callable
    restart_into_new_code: Callable
    drain: Callable
    dispatch_roadmap: Callable
    quota_exhausted: Callable
    quota_mode: Callable
    sync_usage: Callable
    import_bugs: Callable
    report_progress: Callable
    write_now: Callable
    can_take_a_turn: Callable
    sync_trunk: Callable
    plan: Callable
    rest: Callable
    idle_improvement: Callable
    read: Callable          # = _read
    worker_cls: Callable    # = Worker
    tally_cls: Callable     # = Tally
    state: Path             # = STATE
    config: Path            # = CONFIG
    stop: Any               # = _stop (the Event object)
    run: dict               # = _run  (the dict object)
    run_lock: Any           # = _run_lock
    cfg_seen: dict          # = _cfg_seen
    drain_reason: dict      # = _drain_reason


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


def start(c, hours=None, max_tasks=None, awake=False, *, d):
    if os.environ.get("FLINT_RUN_DEADLINE") and d.hours_left(hours) is None:
        d.log("the shift this daemon was restarted into is already over; not starting")
        return
    hours = d.hours_left(hours)
    if hours and not os.environ.get("FLINT_RUN_DEADLINE"):
        # Fix the end of the shift now, once. Every later restart inherits this exact time, so
        # a run that restarts ten times still ends when the first start said it would.
        os.environ["FLINT_RUN_DEADLINE"] = str(time.time() + hours * 3600)
    with open(d.state / "daemon.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"another swarm daemon is already running on this repository.{d.holder()}")
        (d.state / "daemon.pid").write_text(json.dumps(
            {"pid": os.getpid(), "started": time.time(), "goal": d.read_goal(c)[:200],
             "repo": str(Path(c["repo"]).expanduser().resolve()), "config": str(d.config)}))
        d.stop.clear()
        run_id = os.environ.get("FLINT_RUN_ID") or uuid.uuid4().hex
        os.environ["FLINT_RUN_ID"] = run_id
        try:
            saved_run = json.loads((d.state / "run.json").read_text())
        except (OSError, ValueError):
            saved_run = {}
        with d.run_lock:
            d.run.clear()
            d.run.update(saved_run if saved_run.get("id") == run_id else
                        {"id": run_id, "started": time.time(), "blocked_seconds": 0,
                         "requests_started": 0})
            # Reconcile provider vs local at run boundaries (work order §12). The local
            # count is the sidecar-backed sum; the provider deltas are OpenRouter's own
            # daily free-request counters, so a restart mid-day still shows the leftover.
            info = d.account()
            q = ((info or {}).get("free_model_daily_requests") or {})
            provider_now = q.get("used") if isinstance(q.get("used"), int) else None
            if d.run.get("provider_before") is None and provider_now is not None:
                d.run["provider_before"] = provider_now
            if d.run.get("provider_last") is not None and provider_now is not None:
                d.run["provider_delta_since_last"] = max(0, provider_now - d.run["provider_last"])
            d.run["provider_last"] = provider_now
            d.run.update(state="Running", stop_reason=None,
                        deadline=float(os.environ["FLINT_RUN_DEADLINE"]) if os.environ.get("FLINT_RUN_DEADLINE") else None)
        try:
            d.preflight(c)
        except BaseException as exc:
            d.stop.set()
            with d.run_lock:
                d.run["stop_reason"] = f"Startup failed: {type(exc).__name__}: {exc}"
            d.run_status([], c)
            (d.state / "daemon.pid").unlink(missing_ok=True)
            raise
        if awake:
            d.keep_awake()
        timer = None
        if hours:
            # Preflight can run a multi-minute baseline suite. It consumes the shift,
            # too; neither startup verification nor self-restarts may extend the deadline.
            seconds = hours * 3600
            try:
                seconds = min(seconds, max(0, float(os.environ["FLINT_RUN_DEADLINE"]) - time.time()))
            except (KeyError, ValueError):
                pass
            timer = threading.Timer(seconds, d.shutdown)
            timer.daemon = True
            timer.start()
        def stopped_by(name):
            def handler(*_):
                # The run record said `stop_reason: null` after `swarm stop`; say who stopped it.
                with d.run_lock:
                    d.run["stop_reason"] = d.run.get("stop_reason") or f"Stopped ({name})"
                d.shutdown()
            return handler

        signal.signal(signal.SIGTERM, stopped_by("SIGTERM"))
        # A shell starting a job in the background hands it SIGINT set to SIG_IGN, and CPython
        # keeps an inherited SIG_IGN rather than installing its own handler. So a daemon
        # launched with `&` or nohup — which is how every shift is started — silently ignored
        # `swarm stop` and the panel's Stop button. Taking the signal back makes them work.
        signal.signal(signal.SIGINT, stopped_by("SIGINT"))
        try:
            d.run_daemon(c, max_tasks, hours=hours)
        finally:
            if timer:
                timer.cancel()
            d.shutdown()
            with d.run_lock:
                if d.run.get("deadline") and time.time() >= d.run["deadline"]:
                    d.run["stop_reason"] = "Shift deadline reached"
                # Bank the run-level reconciliation at the stop boundary (work order §12).
                # Provider deltas come from OpenRouter's own counters; local attempts come
                # from the request sidecars. Differences are shown, never assigned.
                last_provider = d.run.get("provider_last")
                # provider_last is read at (re)start, so by now it is a shift old: ask again.
                try:
                    used = ((d.account() or {}).get("free_model_daily_requests") or {}).get("used")
                    if isinstance(used, int):
                        last_provider = d.run["provider_last"] = used
                except Exception:
                    pass                    # a stop never waits on, or fails over, the account
                if isinstance(last_provider, int):
                    d.run["provider_at_stop"] = last_provider
                local_total = int(d.run.get("requests_started", 0))
                paid_total = sum(int(a.get("paid_requests") or 0) for a in
                                 d.read(d.state / "journal.jsonl")
                                 if a.get("event") == "attempt" and a.get("run_id") == d.run.get("id"))
                d.run["reconcile"] = Reconcile.summary(
                    [],  # local rows are already summed in requests_started
                    provider_before=d.run.get("provider_before"),
                    provider_after=last_provider,
                    paid_requests=paid_total, local_count=local_total)
                d.journal("run_stop", stop_reason=d.run.get("stop_reason"),
                        requests_local=local_total,
                        reconcile=d.run.get("reconcile"))
            d.run_status([], c)
            (d.state / "daemon.pid").unlink(missing_ok=True)
            (d.state / "now.json").unlink(missing_ok=True)
            d.log("stopped. `swarm report` summarises the run.")


class Periodic:
    """A job the supervisor runs at most once per `interval` seconds.

    `due()` answers and, when it says yes, records the run, exactly like the
    `if time.time() - last > interval: last = time.time()` pairs it replaces."""

    def __init__(self, interval, last=0.0):
        self.interval, self.last = interval, last

    def due(self):
        if time.time() - self.last > self.interval:
            self.last = time.time()
            return True
        return False


class PlannerCadence:
    """When the planner may run next: a cooldown that doubles while planning comes back
    empty (capped at 16x), a quick redraw after a botched plan, and back-off after four."""

    def __init__(self):
        self.last_plan, self.dry_runs, self.plan_fails = 0.0, 0, 0

    def tick(self, c, q, budget, ledger, stop, *, d):
        cooldown = c.get("plan_cooldown", 600) * (2 ** min(self.dry_runs, 4))
        if (c.get("auto_plan", True) and not c.get("roadmap_file") and len(q.ready()) < c["workers"] and time.time() - self.last_plan > cooldown
                and d.can_take_a_turn(c, budget)):
            self.last_plan = time.time()
            try:
                d.sync_trunk(c)
                added = d.plan(c, q, budget, c.get("plan_batch", 5), ledger)
                self.plan_fails = 0
                self.dry_runs = 0 if added else self.dry_runs + 1
                if not added:
                    d.log(f"planner added nothing — next attempt in "
                        f"{c.get('plan_cooldown', 600) * 2 ** min(self.dry_runs, 4) / 60:.0f}m")
            except (StepLimit, ModelError) as e:
                # A botched plan says nothing about whether work remains: redraw a
                # (now less likely) model soon, and back off only on a losing streak.
                self.plan_fails += 1
                if self.plan_fails < 4:
                    self.last_plan = time.time() - cooldown + 60
                    d.log(f"planner failed ({type(e).__name__}: {str(e).splitlines()[0][:120]}); "
                        "redrawing in 1m")
                else:
                    self.plan_fails, self.dry_runs = 0, self.dry_runs + 1
                    d.log("planner failed 4 times in a row; backing off")
            except CapReached:
                d.log("cap reached while planning — sleeping")
                stop.wait(60)
            except ProviderDown as e:
                d.log(f"planner: {d.rest(ledger, e)}; redrawing another model")
                self.last_plan = 0.0
            except NoCredits as e:
                d.log(f"out of credits: {e}")
                d.shutdown()
            except Exception as e:
                d.log(f"planner error: {e}")
                self.dry_runs += 1


def run_daemon(c, max_tasks=None, hours=None, *, d):
    repo = Path(c["repo"])
    budget = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
                    owner_window=c.get("owner_window", ["00:00", "00:00"]))
    q, stop, tally = d.queue_for(c), d.stop, d.tally_cls(max_tasks)
    ledger = Ledger(d.state / "learn.json")
    q.recover()
    d.note_interrupted(c, q)
    d.prune_attempts(int(c.get("keep_attempts", 300)))
    d.log(f"swarm up — repo={repo.name} trunk={d.trunk_name(c)} workers={c['workers']}")
    d.log(f"config: {d.config} ({d.config_kind()})")
    d.log(json.dumps(budget.snapshot()))
    # A stale local counter is the one thing that can stall the swarm on startup or push
    # every turn to a paid model: flint counts failed requests the provider never charged,
    # and the count survives restarts. OpenRouter's daily free-request counter is the
    # truth, so reconcile it now, once, before any turn is routed (work order §12).
    d.resync_allowance(c, budget)
    # Rests persist in learn.json across runs; say so, or a fresh start looks stuck.
    live = set(d.pool(c))
    for model, until, why in ledger.resting():
        if model in live:
            d.log(f"{model} is resting until {dt.datetime.fromtimestamp(until):%H:%M:%S} ({why[:120]})")

    d.cfg_seen["mtime"] = d.config_mtime()     # so the first check is a real change, not this one
    source = d.source_fingerprint()
    restart_for, last_restart = [], time.time()
    d.drain_reason["stop_requested"] = False
    try:
        signal.signal(signal.SIGUSR1, lambda *_: d.drain(tally))
        d.log("edit the config to change settings without a restart; "
            f"`swarm stop --drain` or `kill -USR1 {os.getpid()}` stops after this task")
    except ValueError:
        pass                                # not the main thread; a bounded run in a test
    if c.get("roadmap_file"):
        d.dispatch_roadmap(c, q)
    workers = [d.worker_cls(i, c, q, budget, stop, ledger, tally) for i in range(c["workers"])]
    for w in workers:
        w.start()
    usage, bugs, beat = Periodic(900, last=time.time()), Periodic(300), Periodic(3600, last=time.time())
    cadence = PlannerCadence()
    said = {}
    try:
        while not stop.is_set():
            if tally.drain.is_set():
                while any(w.is_alive() for w in workers) and not stop.is_set():
                    d.write_now(workers, c)
                    stop.wait(1)
                if restart_for and d.drain_reason["stop_requested"]:
                    d.log(f"not restarting into {', '.join(restart_for)}: this run was asked to "
                        "stop, and a stop means stop")
                    d.journal("self_restart_skipped", changed=restart_for, reason="stop requested")
                    restart_for = []
                if restart_for:
                    # Everything is finished and nothing is running: the only safe moment.
                    d.restart_into_new_code(restart_for)
                    restart_for = []          # it refused; carry on with the code we have
                    tally.drain.clear()
                    d.run.update(state="Running", stop_reason=None)
                    workers = [d.worker_cls(i, c, q, budget, stop, ledger, tally)
                               for i in range(c["workers"])]
                    for w in workers:
                        w.start()
                    continue
                d.log(f"finished {tally.n} task(s); stopping")
                break
            # The free allowance can run out with the queue empty and nothing in flight — no
            # worker ever calls flint() to notice, so the gate that stops a running turn
            # (above, in flint()) never fires. Catch that here too, once a tick, so a swarm
            # that starts already out of free requests doesn't just idle until midnight.
            if not tally.drain.is_set() and d.quota_exhausted(c, budget):
                d.log("free allowance exhausted with no usable fallback — finishing any "
                    "task(s) in flight, then stopping (set shutdown_on_quota_exhausted to "
                    "\"wait\" or \"paid\" to keep the daemon up instead)")
                d.journal("quota_exhausted_shutdown", reason=budget.check()[2], mode=d.quota_mode(c))
                d.drain(tally)
                continue
            # An editor that autosaves must not be able to thrash the daemon.
            if (not restart_for and c.get("restart_on_change", True)
                    and time.time() - last_restart > c.get("restart_min_interval", 300)):
                changed = d.source_changed(source)
                if changed:
                    source, restart_for, last_restart = d.source_fingerprint(), changed, time.time()
                    d.log(f"the harness changed on disk ({', '.join(changed)}); finishing the "
                        "task(s) in flight, then restarting into it")
                    d.journal("source_changed", changed=changed)
                    d.drain(tally, stop=False)
            if usage.due():
                d.sync_usage(d.account())
            if bugs.due():
                try:
                    new = [] if c.get("roadmap_file") else d.import_bugs(c, q)
                    if new:
                        d.log(f"queued {len(new)} player bug report(s): "
                            + "; ".join(t["title"][:60] for t in new))
                except Exception as e:      # a player's report must never stop the run
                    d.log(f"could not read the bug reports: {type(e).__name__}: {e}")
            d.report_progress(workers, said)
            d.write_now(workers, c)
            if beat.due():
                d.log(f"heartbeat {json.dumps(budget.snapshot())}")
            # Planning costs requests like anything else, so it is rate-limited
            # and backs off when it stops producing new work.
            if c.get("roadmap_file"):
                try:
                    d.dispatch_roadmap(c, q)
                except Exception as exc:
                    d.log(f"roadmap dispatch blocked: {exc}")
            cadence.tick(c, q, budget, ledger, stop, d=d)
            # Last, and only into genuinely empty time: `idle_ready` stands this down for any
            # assigned work, anything in flight, any held roadmap packet, and the last half
            # hour of the shift. It spends the same allowance as every other turn.
            if c.get("idle_improvement", True) and not max_tasks:
                try:
                    d.idle_improvement(c, q, budget, ledger, hours=hours)
                except (CapReached, ProviderDown):
                    pass                    # the allowance or the provider; not our business
                except NoCredits as e:
                    d.log(f"out of credits: {e}")
                    d.shutdown()
                except Exception as e:      # work nobody asked for must never stop a run
                    d.log(f"idle improvement error: {type(e).__name__}: {e}")
            stop.wait(min(10, c.get("tick", 90)) if not max_tasks else 5)
    except KeyboardInterrupt:
        d.log("shutting down")
    finally:
        d.shutdown()
        for w in workers:
            w.join(timeout=10)

