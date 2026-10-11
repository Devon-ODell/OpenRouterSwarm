"""The worker thread: claim a task, freeze its config, run the attempt, release the
outcome, back off. Attempt work happens in AttemptRunner; every daemon service arrives
through WorkerDeps, so this module reads no daemon global."""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from budget import Budget
from metrics import ALLOWANCE_BLOCKED, BucketClock, IDLE, PROVIDER_BACKOFF, WALL_BUCKETS
from runtime_context import ConfigSnapshot

try:  # one class identity with the daemon's turn errors
    from .agent_turn import CapReached, NoCredits, ProviderDown
except ImportError:  # pragma: no cover - script mode
    from agent_turn import CapReached, NoCredits, ProviderDown


@dataclass(frozen=True)
class WorkerDeps:
    """The daemon services the worker loop uses, read when they are asked for."""
    log: Callable
    journal: Callable
    watch_config: Callable
    read_goal: Callable
    recheck_allowance: Callable
    allowance_recheck: Callable
    next_wake: Callable
    all_resting: Callable
    describe_error: Callable
    forget_worker: Callable
    config_mtime: Callable
    cfg_lock: Any          # the lock the live config dict is rewritten under
    run: dict              # the run-state dict (requests_started)
    new_ledger: Callable   # () -> the repository's Ledger
    new_tally: Callable    # () -> a fresh Tally
    clocks: dict           # worker name -> BucketClock


# Attempt operations the worker forwards to its AttemptRunner. They stay attributes of
# the Worker because callers and tests reach them there, and patching one on the Worker
# has to intercept the calls the runner makes back through it.
ATTEMPT_METHODS = ("pick", "select_implementer", "no_review", "excluded_models", "call",
                   "ensure_worktree", "adopt_retained", "own_branch", "gate", "snapshot",
                   "review", "reviewed", "_attempt", "integrate", "judge", "_reinforce",
                   "follow_up", "celebrate", "hang", "_cleanup", "strand", "decompose",
                   "retry_split", "scope_violations")


class Worker(threading.Thread):
    def services(self) -> WorkerDeps:
        raise NotImplementedError("bind the worker to a daemon")

    def runner(self):
        raise NotImplementedError("bind the worker to a daemon")

    def __init__(self, idx, c, q, budget, stop, ledger=None, tally=None):
        super().__init__(daemon=True, name=f"w{idx}")
        d = self.services()
        self.idx, self.c, self.q, self.budget, self.stop = idx, c, q, budget, stop
        self.ledger = ledger or d.new_ledger()
        self.tally = tally or d.new_tally()
        self.branch = f"swarm/w{idx}"
        self.wt = None
        self.stage = None
        self.task = None
        self.last_model = None
        self.last_role = None
        self.agent_timed_out = False
        self.worktree_provisioned = False
        self.failure_class = "task"
        self.state = None                 # structured state code (work order §13)
        self.clock = BucketClock()        # wall-time buckets by state
        d.clocks[self.name] = self.clock

    def publish(self, state):
        """Record a structured state transition, for the wall-time buckets and the panel.

        `self.doing` remains human prose; `state` is the machine-readable bucket the
        panel and reports accumulate. Every transition banks the previous bucket, so the
        buckets' total approximates the run's wall time (work order §13)."""
        if state not in WALL_BUCKETS:
            raise ValueError(f"unknown worker state {state!r}")
        self.state = state
        self.clock.enter(state)

    def backing_off(self, state, why):
        """The task is deferred and this worker is asleep until a model or the allowance is back.

        Without this the last turn's "implementer with <model>" stayed as `doing` through the
        wait, so progress lines said the worker was on a model that was not running, and the
        wait was banked as model time."""
        self.doing = why
        self.publish(state)

    # -- helpers -----------------------------------------------------------

    def _attempt_config(self, c=None):
        """Return the supplied attempt config, or freeze one for a direct entry."""
        if c is None:
            # The live dict is rewritten in-place under this same lock. Freeze it while
            # holding the lock so a second worker cannot yield a mixed/partial snapshot.
            d = self.services()
            with d.cfg_lock:
                return ConfigSnapshot(values=self.c, mtime=d.config_mtime()).values
        return c

    def _attempt_inputs(self, c=None, budget=None):
        """Return immutable config and pacing policy for one attempt entry.

        ``run()`` always supplies both values from the snapshot taken immediately after
        claim.  The fallback keeps the long-standing direct-call test/API surface useful:
        callers entering ``do_task`` or an individual helper outside the worker loop get a
        fresh snapshot, never the mutable live dict itself.  Nothing is stored on the
        worker, so concurrent attempts cannot replace each other's view.
        """
        c = self._attempt_config(c)
        if budget is None:
            budget = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
                            owner_window=c.get("owner_window", ["00:00", "00:00"]))
        return c, budget

    def do_task(self, task, goal, c=None, attempt_budget=None):
        # One runner, and so one snapshot of the daemon's services, for the whole attempt. The
        # attempt calls back through the worker dozens of times (snapshot, gate, call, ...);
        # each of those used to rebuild the runner and re-read every daemon global.
        self._attempt_runner = runner = self.runner()
        try:
            outcome = runner.run(task, goal, c, attempt_budget)
        finally:
            self._attempt_runner = None
        return outcome.accepted, outcome.note

    # -- loop --------------------------------------------------------------

    def run(self):
        while not self.stop.is_set() and not self.tally.drain.is_set():
            d = self.services()
            # Between tasks, never inside one: an edited config reaches this run without a
            # restart, and a restart is what killed 54 attempts in this window.
            d.watch_config(self.c, self.budget)
            run_cap = max(0, int(self.c.get("run_request_cap") or 0))
            requests_left = max(0, run_cap - int(d.run.get("requests_started", 0))) if run_cap else None
            task = self.q.claim(requests_left=requests_left)
            self.attempt_requests = 0
            if not task:
                self.doing, self.state = None, IDLE
                self.clock.enter(IDLE)
                self.stop.wait(20)
                continue
            # One claim, one immutable view. A different worker may reload the live dict and
            # retune the daemon budget while this task is running; neither change reaches this
            # attempt. Budget still reads the shared on-disk request counters, so accounting is
            # not forked — only the pacing policy is frozen.
            with d.cfg_lock:
                snapshot = ConfigSnapshot(values=self.c, mtime=d.config_mtime())
            c = snapshot.values
            attempt_budget = Budget(
                cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
                owner_window=c.get("owner_window", ["00:00", "00:00"]))
            # A4 handoff 2026-10-02: an attempt without a persona skips the role
            # shaping that makes prompts accurate, and ~half the audit's effort ran
            # that way. Planner tasks always carry one; human-origin tasks default
            # to "builder" so no attempt burns unshaped, and the journal records it.
            if not task.get("persona"):
                task["persona"] = "builder"
                d.journal("persona_default", id=task["id"],
                          from_origin=task.get("origin", "human"))
            try:
                goal = d.read_goal(c)
                ok, note = self.do_task(task, goal, c, attempt_budget)
                # A timeout only forces a split when there was work to be too big for. With no
                # diff there is nothing to divide, and the timeout is ours, not the task's.
                final = self.q.release(task["id"], ok, note, failure_class=self.failure_class,
                                       split_now=self.agent_timed_out and self.stage != "agent_timeout",
                                       park=self.stage in ("out_of_scope", "request_budget", "model_pool_exhausted"),
                                       requests=getattr(self, "attempt_requests", 0),
                                       failed_model=(self.last_model if not ok and self.failure_class in
                                                     ("harness", "model") else None),
                                       failed_role=self.last_role, ledger=self.ledger,
                                       turn_seconds=round(max(0, time.time() - (getattr(self, "started", 0) or time.time())), 1),
                                       role_turns=getattr(self, "role_calls", 0) or 0, stage=self.stage)
                d.journal("task", id=task["id"], title=task["title"], ok=ok, stage=self.stage,
                          failure_class=None if ok else self.failure_class,
                          status=final.get("status") if final else None,
                          exploration_failures=final.get("exploration_failures") if final else None,
                          worker=self.name, requests=getattr(self, "attempt_requests", 0), note=note[:300])
                if final and final.get("status") == "split":
                    if not self.decompose(final, goal, c=c, attempt_budget=attempt_budget):
                        self.strand(final, "was split into no subtasks")
                elif final and final.get("status") == "parked":
                    self.strand(final, "was parked after repeated failures")
                self.tally.finished()
            except CapReached:
                d.log("quota/window pause — deferring task", self.name)
                self.q.release(task["id"], False, "quota/window pause", defer=60,
                               failure_class="harness", requests=self.attempt_requests)
                # 22 of these in one day, ten minutes each, while the local count read
                # 1025/1000 and OpenRouter reported fewer requests used. Ask before sleeping,
                # and sleep no longer than the interval at which the next ask is due.
                if d.recheck_allowance(c, attempt_budget):
                    continue
                self.backing_off(ALLOWANCE_BLOCKED, "waiting for the free allowance")
                self.stop.wait(min(600, d.allowance_recheck(c) or 600))
            except ProviderDown as e:
                self.q.release(task["id"], False, f"provider unavailable: {str(e)[:200]}", defer=30,
                               failure_class="harness", requests=self.attempt_requests,
                               turn_seconds=round(max(0, time.time() - (getattr(self, "started", 0) or time.time())), 1),
                               role_turns=getattr(self, "role_calls", 0) or 0)
                if self.pick(c=c) is None:
                    # No model can take a turn. Sleep until the first one is back, not blindly.
                    _, until = d.next_wake(self.ledger, c)
                    wait = max(30, min(300, until - time.time()))
                    d.log(f"{d.all_resting(self.ledger, c)}; task deferred, next try in {wait:.0f}s", self.name)
                else:
                    wait = 30
                    d.log(f"no other model could take over from {e.model}; task deferred, next try in 30s", self.name)
                self.backing_off(PROVIDER_BACKOFF, f"waiting for a model (next try in {wait:.0f}s)")
                self.stop.wait(wait)
            except NoCredits as e:
                d.log(f"out of credits: {e}", self.name)
                self.q.release(task["id"], False, "no credits", requests=self.attempt_requests)
                self.stop.set()
            except Exception as e:
                if self.stop.is_set():
                    self.q.release(task["id"], False, "run stopped; changes retained", defer=1,
                                   failure_class="harness")
                    return
                described = d.describe_error(e)
                d.log(f"error on {task['id']}: {described['kind']}: {described['err']}", self.name)
                d.journal("error", id=task["id"], tb=traceback.format_exc()[-1200:], **described)
                # A turn that timed out is still a failed attempt: let it split like any other,
                # or the task is shelved with no smaller pieces to try.
                # An unexpected exception in the supervisor is ours, not the task's.
                final = self.q.release(task["id"], False, str(e)[:400], failure_class="harness")
                if final and final.get("status") == "split":
                    try:
                        self.decompose(final, d.read_goal(c), c=c,
                                       attempt_budget=attempt_budget)
                    except Exception as exc:
                        d.log(f"could not split '{final['title']}': {exc}", self.name)
                self.stop.wait(30)
            finally:
                # Between tasks a worker is not working on anything. `task` and `started` used
                # to keep the last task's values while the worker sat in claim(), so the panel
                # showed a finished task as still running with an elapsed time that kept
                # climbing — the single most misleading line the status ever printed.
                self.task, self.started, self.doing = None, 0, None
                d.forget_worker(self.name)


def _forward(name):
    def method(self, *args, **kwargs):
        runner = getattr(self, "_attempt_runner", None) or self.runner()
        return getattr(runner, name)(*args, **kwargs)
    method.__name__ = method.__qualname__ = name
    return method


for _name in ATTEMPT_METHODS:
    setattr(Worker, _name, _forward(_name))
