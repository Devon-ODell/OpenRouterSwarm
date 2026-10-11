"""The agent-turn contract: typed turn errors and model routing decisions.

Every dependency is injected; nothing in this module imports the daemon or
reads its globals.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


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


def read_charges(path, role, worker, model, *, journal, count_usd):
    """Bank what a finished turn was charged, one `charge` row each, and return the total."""
    total = 0.0
    try:
        rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return 0.0
    for row in rows:
        usd = round(float(row.get("usd") or 0.0), 6)
        total = round(total + usd, 6)
        journal("charge", role=role, worker=worker, model=row.get("model") or model, usd=usd)
    if total:
        count_usd(worker, total)
    Path(path).unlink(missing_ok=True)
    return total


def route_to_paid(c, role, worker, model, why, *, paid_stand_in, spend_left, spend_phrase,
                  spend_used, spend_window, log, journal, ran_on):
    """Swap in a paid stand-in for this turn, or None when none is reachable or affordable."""
    alt = paid_stand_in(c, model)
    if not alt:
        return None
    left = spend_left(c)
    log(f"{role}: {why} — routing this turn to paid {alt}; its cost is booked from "
        "OpenRouter's reported charge"
        + (f" ({spend_phrase(c)})" if left is not None else ""), worker)
    journal("paid_fallback", role=role, worker=worker, model=model, to=alt,
            recorded_spend_usd=round(spend_used(c), 6), spend_window=spend_window(c),
            charge_confirmed=False, reason=why)
    ran_on[worker] = alt
    return alt


def pick_model(ledger, c, exclude=(), role="implementer", *, pool, paid_pool, spend_left):
    """Implementers by Thompson sampling. Reviewers and judges come from the
    same posterior, excluding the implementer so nobody grades their own work.

    `role` is the job this model is being drawn for, so `role_models` can keep a model
    out of the roles it is bad at while leaving it in the ones it is good at. The
    posterior is still the implementer's: one model's record is one record.

    When every free model is resting, a paid stand-in keeps the attempt alive rather
    than abandoning work already done — but only while the day's budget allows."""
    m = ledger.pick("implementer", pool(c, role), exclude=exclude)
    if m is not None:
        return m
    left = spend_left(c)
    if left is not None and left <= 0:
        return None
    return next((p for p in paid_pool(c) if p not in set(exclude)), None)


def excluded_models(task, role=None, *, now):
    """Models the task/model circuit breaker has excluded from `role` on this task.

    One timeout, incomplete response, length termination or no-edit exhaustion on a task
    excludes that model from that task/role for the rest of the run or 24 hours (work
    order §5). A provider-busy handoff before any edit stays cheap and does not poison
    the pairing, so those failures are not recorded as exclusions. A harness timeout is
    still an exclusion: the pairing demonstrably cannot deliver inside the budget, so the
    next implementation attempt must use another model.
    """
    out = set()
    for f in (task or {}).get("failed_models", []):
        if not isinstance(f, dict):
            continue
        cls = f.get("class") or ""
        at = f.get("at") or 0
        if now - at > 24 * 3600:
            continue
        if role and f.get("role") not in (None, role):
            continue
        note = str(f.get("note") or "")
        no_edit_exhaustion = ("timeout" in note.lower() or "timed out" in note.lower()
                              or "70%" in note or "without editing" in note)
        if cls in ("provider_busy", "review_format") or (cls == "harness" and not no_edit_exhaustion):
            # A provider-busy handoff before edits is cheap and the pairing stays usable;
            # a review-format failure is a fact about the reviewer's JSON; an ordinary
            # harness loss (restart, sandbox) says nothing about the model.
            continue
        out.add(f.get("model"))
    return out


def count_study(turns, worker, n=0, reset=False):
    """Study-tool calls made by one worker's turns since its attempt began."""
    with turns.lock:
        if reset:
            return turns.study_calls.pop(worker, 0)
        turns.study_calls[worker] = turns.study_calls.get(worker, 0) + n
        return turns.study_calls[worker]


def count_usd(turns, worker, usd=0.0, reset=False):
    """What one worker's turns have been charged since its attempt began."""
    with turns.lock:
        if reset:
            return round(turns.charges.pop(worker, 0.0), 6)
        turns.charges[worker] = round(turns.charges.get(worker, 0.0) + usd, 6)
        return turns.charges[worker]


def count_edits(turns, worker, n=0, reset=False):
    """Edit calls one worker's turns made since its attempt began.

    An attempt that ends with no diff means two different things. A model that called edit_file
    and still changed nothing got it wrong; a model that never reached an edit ran out of the
    rounds this harness gave it. Only the first is the model's failure."""
    with turns.lock:
        if reset:
            return turns.edit_calls.pop(worker, 0)
        turns.edit_calls[worker] = turns.edit_calls.get(worker, 0) + n
        return turns.edit_calls[worker]


def count_requests(turns, worker, n=0, reset=False, *, run, run_lock):
    """API request starts attributable to this worker's current task."""
    with turns.lock:
        if reset:
            return turns.request_calls.pop(worker, 0)
        n = max(0, int(n or 0))
        turns.request_calls[worker] = turns.request_calls.get(worker, 0) + n
        if n:
            with run_lock:
                run["requests_started"] = run.get("requests_started", 0) + n
        return turns.request_calls[worker]


@dataclass(frozen=True)
class TurnResult:
    """What one finished flint turn produced."""
    text: str          # the role's answer, stripped
    model: str         # the model the turn actually ran on (a paid stand-in may have taken it)
    rc: int
    requests: int      # request starts the turn made
    usd: float         # what OpenRouter charged for it
    log: Path


@dataclass(frozen=True)
class TurnDeps:
    """Every daemon service a turn uses, injected so this module reads no globals."""
    pool: Callable
    default_model: str
    turns: Any                 # TurnBook
    stop: Any                  # threading.Event
    route_to_paid: Callable
    recheck_allowance: Callable
    quota_exhausted: Callable
    quota_mode: Callable
    shutdown: Callable
    log: Callable
    journal: Callable
    waiting: Callable
    worker_publish: Callable
    allowance_blocked: Any     # = ALLOWANCE_BLOCKED
    held: dict                 # = _held
    read_only_roles: Any       # = READ_ONLY_ROLES
    no_corpus: str             # = NO_CORPUS
    spend_file: Callable
    spend_cap: Callable
    spend_reset_day: Callable
    logs: Path                 # = LOGS
    root: Path                 # = ROOT
    sandboxed: Callable
    turn_timeout: Callable
    process: Callable
    kill_group: Callable
    count_requests: Callable
    count_study: Callable
    count_edits: Callable
    read_charges: Callable
    request_log: Callable      # = RequestLog class


class AgentTurnRunner:
    """One headless flint turn behind the subprocess boundary: pacing, env, prompt file,
    sidecars, timeouts and exit-code translation."""

    def __init__(self, deps: TurnDeps):
        self.deps = deps

    def run(self, prompt, cwd, c, role, worker, budget, max_steps, model=None) -> TurnResult:
        d = self.deps
        model = model or (d.pool(c) or [d.default_model])[0]
        d.turns.ran_on.pop(worker, None)
        whole_turn = max(1, int(max_steps or 1))
        try:
            while True:
                if d.stop.is_set():
                    raise Stopped("swarm is stopping")
                ok, wait, why = budget.check()
                if ok:
                    # Starting is not finishing. 21 turns paused part-way on the budget (exit 6) and
                    # everything they had done to that point was thrown away, so a turn whose rounds do
                    # not all fit goes to a paid model now rather than halfway through. If no paid model
                    # is reachable it still runs: a short turn may finish, and idling until midnight is
                    # certainly worse.
                    if not budget.check(need=whole_turn)[0]:
                        d.route_to_paid(c, role, worker, model,
                                      f"only part of a {whole_turn}-round turn fits the free allowance")
                        model = d.turns.ran_on.get(worker, model)
                    break
                # The local count only climbs until midnight. Before paying for this turn or waiting
                # on it, ask OpenRouter whether the free requests are really gone.
                if d.recheck_allowance(c, budget):
                    continue
                # Free capacity is gone. If this swarm has a dollar budget left, the turn runs on a
                # paid model rather than idling until midnight; `paid_stand_in` returns None once the
                # budget is spent, and during the owner's window no paid model is reached for at all.
                if budget.paid_would_help():
                    alt = d.route_to_paid(c, role, worker, model, why)
                    if alt:
                        model = alt
                        break
                # Free and gone, with no usable fallback for the configured mode: by default
                # the swarm stops here rather than idling for hours until the allowance
                # resets. Set shutdown_on_quota_exhausted to "wait" or "paid" to keep the
                # daemon up instead — "paid" keeps retrying a paid model every cycle below.
                if d.quota_exhausted(c, budget):
                    d.log(f"{role}: free allowance exhausted ({why}) with no usable fallback — "
                        "shutting down (set shutdown_on_quota_exhausted to \"wait\" or \"paid\" "
                        "to keep the daemon up instead)", worker)
                    d.journal("quota_exhausted_shutdown", role=role, worker=worker, reason=why,
                            mode=d.quota_mode(c))
                    d.shutdown()
                    raise Stopped("free allowance exhausted; shutting down")
                # The allowance can be hours from resetting; saying so once a quarter hour is enough,
                # and the worker records that it is waiting rather than working.
                d.waiting(worker, f"waiting for the allowance: {why}")
                d.worker_publish(worker, d.allowance_blocked)
                if time.time() - d.held.get(worker, 0) > 900:
                    d.held[worker] = time.time()
                    d.log(f"{role}: holding {wait/60:.0f}m — {why}", worker)
                d.stop.wait(max(1, min(wait, 60)))
        finally:
            # Whatever ends the wait — a stop, a cap, a provider going away — the worker is
            # no longer holding for the allowance. Leaving the note behind made `now.json`
            # report a reason from a turn that had already finished, and the panel's idle
            # line kept quoting it while the swarm was demonstrably working.
            d.held.pop(worker, None)
            d.waiting(worker, None)

        env = dict(os.environ)
        env.pop("FLINT_ALLOWED_PATHS", None)
        if c.get("_allowed_paths"):
            env["FLINT_ALLOWED_PATHS"] = json.dumps(c["_allowed_paths"])
        env["FLINT_MAX_STEPS"] = str(max_steps)
        # The suite the model is told to leave passing takes 105s in the studio, and flint's bash
        # tool cut off at 120s — 15 seconds from killing the run it was asked to make.
        env["FLINT_BASH_TIMEOUT"] = str(int(c.get("test_timeout", 900)))
        if role not in d.read_only_roles:
            # This role's product is a diff, so flint offers it one edit-only round before the
            # tool-free one and applies edits it writes as reply text. Without that, a turn spent
            # reading is thrown away whole and scored against the model as "no change".
            env["FLINT_FINAL_EDIT"] = "1"
        if not env.get("OPENROUTER_API_KEY"):
            env.pop("OPENROUTER_API_KEY", None)
        if c.get("study") is False:
            env["FLINT_CORPUS_DB"] = d.no_corpus
        elif c.get("corpus_db"):
            env["FLINT_CORPUS_DB"] = str(Path(c["corpus_db"]).expanduser())
        env["FLINT_SWARM_BUDGET"] = json.dumps(dict(
            cap=budget.cap, reserve=budget.reserve, owner_window=c.get("owner_window", ["00:00", "00:00"])))
        # Shared when the budget is monthly, per-repository when it is daily. flint charges real
        # costs here and refuses a paid request the remaining budget can no longer cover.
        env["FLINT_SPEND_FILE"] = str(d.spend_file(c))
        # flint knows what OpenRouter charged; this is how that number gets back here.
        charge_file = d.logs / f"{worker}-{role}-{time.time_ns()}.charges.jsonl"
        env["FLINT_CHARGE_FILE"] = str(charge_file)
        # flint records every HTTP request attempt (including provider retries) here, before it
        # is made, so a SIGKILLed turn still leaves an exact count (work order §1). The daemon
        # reads this after the turn; it never has to parse human log lines for the number.
        request_file = d.logs / f"{worker}-{role}-{time.time_ns()}.requests.jsonl"
        env["FLINT_REQUEST_FILE"] = str(request_file)
        local_count_before = d.count_requests(worker)
        if d.spend_cap(c) is not None:
            env["FLINT_SPEND_CAP"] = str(d.spend_cap(c))
        if d.spend_reset_day(c) is not None:
            env["FLINT_SPEND_RESET_DAY"] = str(d.spend_reset_day(c))
        # The prompt is 10-25 KB. In argv it shows up in `ps`, in every exception's str() and in
        # any crash report, so it goes to a file the attempt is keeping anyway.
        prompt_file = d.logs / f"{worker}-{role}-{time.time_ns()}.prompt.txt"
        prompt_file.write_text(prompt)
        cmd = [c.get("python", sys.executable), str(d.root / "flint.py"),
               "--prompt-file", str(prompt_file), "-C", str(cwd), "-m", model,
               "--read-only" if role in d.read_only_roles else "--yolo"]
        cmd = d.sandboxed(cmd, cwd, c)
        timeout = d.turn_timeout(c, role)
        d.log(f"{role}: asking {model} (up to {max_steps} rounds; timeout {timeout}s)", worker)
        t0 = time.time()
        # Keep progress/errors separate from the role's machine-readable answer.
        logfile = d.logs / f"{worker}-{role}-{time.time_ns()}.log"
        expired = False
        d.turns.active_turns[worker] = f"{role} with {model}"
        d.turns.turn_logs[worker] = logfile
        try:
            with open(logfile, "w") as err:
                with d.process(cmd, stdout=subprocess.PIPE, stderr=err,
                             text=True, env=env) as p:
                    try:
                        out, _ = p.communicate(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        d.kill_group(p)
                        out, _ = p.communicate()
                        expired = True
                    except BaseException:
                        d.kill_group(p)
                        p.communicate()
                        raise
        finally:
            d.turns.active_turns.pop(worker, None)
            d.turns.turn_logs.pop(worker, None)
        progress = logfile.read_text(errors="replace")
        studied = len(re.findall(r"^tool: study$", progress, re.M))
        rounds = len(re.findall(r"^round \d+/\d+: requesting ", progress, re.M))
        provider_retries = len(re.findall(r"provider failed mid-reply — retry \d+/\d+", progress))
        # The sidecar is the source of truth for request starts; the log-line counts are a
        # cross-check that must agree. If the sidecar is missing (an older flint, or a swept
        # log dir) the human-log count is the fallback, and the journal says which was used.
        sidecar_rows, sidecar_total = d.request_log(request_file).read()
        requests_started = sidecar_total if sidecar_total else (rounds + provider_retries)
        d.count_requests(worker, requests_started)
        d.count_study(worker, studied)
        d.count_edits(worker, len(re.findall(r"^tool: (?:edit_file|write_file)$", progress, re.M)))
        usd = d.read_charges(charge_file, role, worker, model)
        d.journal("turn", role=role, worker=worker, model=model, rc=p.returncode,
                secs=round(time.time() - t0), chars=len(out), log=str(logfile), study_calls=studied,
                requests_started=requests_started, tool_rounds=rounds,
                provider_retries=provider_retries,
                local_count_before=local_count_before,
                local_count_after=d.count_requests(worker),
                request_sidecar=request_file.name,
                sidecar_total=sidecar_total, usd=usd)
        d.request_log(request_file).clear()
        error = progress[-1200:]
        with open(logfile, "a") as f:  # keep the answer beside the progress for later review
            f.write(f"\n--- answer from {model} (exit {p.returncode}) ---\n{out}\n")
        # Ctrl-C kills the turn's process, which would otherwise look like a crashed model and
        # cost it twice the usual penalty weight. Stopping the run is not the model's doing.
        if d.stop.is_set() and p.returncode != 0:
            raise Stopped(f"{role} turn on {model} was stopped with the run")
        if expired:
            d.journal("agent_timeout", role=role, worker=worker, model=model,
                    timeout=timeout, secs=round(time.time() - t0), log=str(logfile))
            raise AgentTimeout(role, model, timeout, logfile)
        if p.returncode in (3, 6):
            raise CapReached(error)
        if p.returncode == 4:
            raise NoCredits(error)
        if p.returncode == 5:
            raise StepLimit(error)
        if p.returncode == 11:
            # The turn spent 70% of its rounds without editing (work order §2). It is not a
            # model failure in the scoring sense — the harness let it explore — but it is a
            # strong signal for the task/model circuit breaker: same model, same role, same
            # no-edit exhaustion should not immediately repeat.
            raise ExplorationExhausted(error)
        if p.returncode == 7:
            raise ProviderDown(model, error)
        if p.returncode == 8:
            raise ProviderBusy(model, error)
        if p.returncode == 9:
            raise ModelGone(model, error)
        if p.returncode != 0:
            raise ModelError(f"{role} turn failed (rc={p.returncode}); log: {logfile}\n{error}")
        return TurnResult(text=out.strip(), model=model, rc=p.returncode,
                          requests=requests_started, usd=usd, log=logfile)
