"""Status snapshots, health, progress and per-worker bookkeeping.

Every dependency is injected; nothing here imports the daemon or reads its globals.
"""
from __future__ import annotations

import datetime as dt, json, re, shutil, threading, time
from pathlib import Path

from task_rules import failure_class

ROUND_LINE = re.compile(r"^round (\d+)/(\d+):", re.M)


def turn_round(worker, *, turn_logs):
    """(round, of) for the turn this worker is running, from the lines flint prints, or None."""
    path = turn_logs.get(worker)
    try:
        tail = Path(path).read_text(errors="replace")[-4000:]
    except (OSError, TypeError):
        return None
    hits = ROUND_LINE.findall(tail)
    return (int(hits[-1][0]), int(hits[-1][1])) if hits else None


def mark_progress(kind, *, run, run_lock):
    with run_lock:
        run.update(last_progress_at=time.time(), last_progress=kind)


def read_now(max_age=180, *, state):
    """The daemon's last `now` note, or None when it is missing or stale."""
    try:
        note = json.loads((state / "now.json").read_text())
    except (OSError, ValueError):
        return None
    return note if time.time() - note.get("at", 0) <= max_age else None


def report_progress(workers, said, every=180, *, active_turns, waiting_books, log):
    """Say that a working worker is alive, and on what. A turn can be silent for minutes —
    a slow free model, a first `go build` fetching modules — which reads as a freeze."""
    now = time.time()
    for w in workers:
        task, since = getattr(w, "task", None), getattr(w, "started", 0)
        if not (task and since and w.is_alive()):
            continue
        if now - said.get(w.name, since) > every:
            said[w.name] = now
            log(f"still on '{task['title'][:60]}' ({(now - since) / 60:.0f}m): "
                f"{waiting_books.get(w.name) or active_turns.get(w.name) or getattr(w, 'doing', '?')}", w.name)


def waiting(worker, why, *, books):
    """Record that a worker is blocked rather than working, so progress reports say so."""
    if why is None:
        books.pop(worker, None)
    else:
        books[worker] = why


def worker_publish(worker, state, *, clocks):
    """Publish a structured state code for a worker by name (work order §13).

    `flint()` runs at module level and only knows the worker's name, not the Worker
    object; the BucketClock registry is how a wall-time bucket gets banked there.
    A missing clock (a test harness that never built a Worker) is ignored."""
    clock = clocks.get(worker)
    if clock is not None:
        clock.enter(state)


def forget_worker(worker, *, turns, waiting, held):
    """Drop everything this worker's last task left behind.

    Every one of these maps is keyed by worker name and outlives the task that filled it.
    A worker that finished a turn on a paid stand-in kept `_ran_on`; one whose wait was cut
    short by a stop kept `_waiting`; a killed turn kept `_active_turns` and `_turn_logs`.
    The next task then reported the previous task's model, the previous task's reason for
    not working, and a round counter read out of a log that had stopped growing."""
    for book in (waiting, turns.ran_on, turns.active_turns, turns.turn_logs, held):
        book.pop(worker, None)
    with turns.lock:
        for book in (turns.study_calls, turns.edit_calls, turns.request_calls, turns.charges):
            book.pop(worker, None)


def live_workers(workers):
    """The names of workers that are still running, for reports that must not quote a dead one."""
    return {w.name for w in workers if w.is_alive()}


def attempts_for(tid, *, state):
    """Every recorded attempt at one task, newest first, as (directory, attempt.json)."""
    out = []
    for path in sorted((state / "attempts").glob(f"{tid}-*"), reverse=True):
        try:
            out.append((path, json.loads((path / "attempt.json").read_text())))
        except (OSError, ValueError):
            out.append((path, {}))
    return out


def prune_attempts(keep=300, *, state, log):
    """Keep the newest attempt bundles and drop the rest.

    One directory per attempt is what makes a task's evidence findable; it is also what makes
    the directory grow without limit. A shift lands a few dozen, so a few hundred is several
    weeks of history."""
    try:
        bundles = sorted((state / "attempts").iterdir(), key=lambda p: p.stat().st_mtime,
                         reverse=True)
    except OSError:
        return 0
    dropped = 0
    for path in bundles[keep:]:
        try:
            shutil.rmtree(path)
            dropped += 1
        except OSError:
            pass
    if dropped:
        log(f"pruned {dropped} old attempt bundle(s); keeping the newest {keep}")
    return dropped


def retry_recorded(evidence, since):
    """Whether a person recorded a repair or a decision for a packet after its last attempt.

    Making a failed packet visible is only half of it. Re-dispatching it on the next tick
    would run it straight back into the failure that stopped it, once every tick, for the
    rest of the shift. So it stays held until the ledger carries a `retry`, `repair` or
    `decision` with a reason, timestamped after the attempt that failed."""
    note = evidence.get("retry") or evidence.get("repair") or evidence.get("decision")
    if not isinstance(note, dict) or not str(note.get("reason") or "").strip():
        return False
    at = note.get("at")
    if at in (None, ""):
        return False                      # undated authorisation would re-authorise every failure
    try:
        when = float(at)
    except (TypeError, ValueError):
        try:
            when = dt.datetime.fromisoformat(str(at)).timestamp()
        except ValueError:
            return False
    return when > (since or 0)


def promotion_state(c, *, git, trunk_name):
    """The four promotion states of the swarm's work (work order §14).

    1. Accepted — reviewer and gates approved an immutable candidate;
    2. Landed — commit is reachable from swarm/trunk;
    3. Merged — commit is reachable from the configured base branch;
    4. Verified live — a recorded deployment/health command observed that commit in the
       service. There is no deployment recorder yet, so this is always "none (no
       deployment recorder)" unless a future packet adds one.

    Never summarize "landed on swarm/trunk" as shipped, merged or deployed.
    """
    repo, base, t = c["repo"], c.get("base_branch", "main"), trunk_name(c)
    rc, _ = git(["rev-parse", "--verify", "--quiet", t], cwd=repo)
    landed = rc == 0
    merged = False
    if landed:
        rc, _ = git(["merge-base", "--is-ancestor", t, base], cwd=repo)
        merged = rc == 0
    return {"accepted": int(landed),
            "landed": int(landed),
            "merged": int(merged),
            "verified_live": 0,
            "verified_live_note": "no deployment recorder; accepted work sits on swarm/trunk until merged"}


def health(hours=24, *, state, read):
    """The numbers that say whether the swarm is working well, over the last `hours`.

    None of this was anywhere: the panel showed free requests and a corpus chunk count while the
    landed rate sat at 10% for a day and a half."""
    since = time.time() - hours * 3600
    rows = [r for r in read(state / "journal.jsonl") if r.get("t", 0) >= since]
    attempts = [r for r in rows if r["event"] == "attempt"]
    landed = [a for a in attempts if a.get("stage") == "accepted"]
    classes = {}
    for a in attempts:
        if a.get("stage") != "accepted":
            cls = a.get("failure_class") or failure_class(a.get("stage") or "", a.get("note") or "")
            classes[cls] = classes.get(cls, 0) + 1
    usd = round(sum(a.get("usd") or 0.0 for a in attempts), 6)
    return {"hours": hours, "attempts": len(attempts), "landed": len(landed),
            "landed_rate": round(len(landed) / len(attempts), 4) if attempts else None,
            "failures": sorted(classes.items(), key=lambda kv: -kv[1])[:3],
            "usd": usd,
            "usd_per_landed": round(usd / len(landed), 6) if landed and usd else None,
            # Ten attempts is the fewest worth judging a rate on.
            "unhealthy": bool(attempts and len(attempts) >= 10
                              and len(landed) / len(attempts) < 0.15)}


class Tally:
    """Finished tasks across workers; sets drain once an optional limit is reached."""

    def __init__(self, limit=None, drain=None):
        self.n, self.limit = 0, limit
        self.drain = drain or threading.Event()
        self.lock = threading.Lock()

    def finished(self):
        with self.lock:
            self.n += 1
            if self.limit and self.n >= self.limit:
                self.drain.set()
