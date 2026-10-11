"""Status snapshots, health, progress and per-worker bookkeeping.

Every dependency is injected; nothing here imports the daemon or reads its globals.
"""
from __future__ import annotations

import datetime as dt, json, re, shutil, threading, time
from pathlib import Path

from task_rules import failure_class
from learn import Ledger, format_playbook
from metrics import BucketClock
from budget import Budget

ROUND_LINE = re.compile(r"^round (\d+)/(\d+):", re.M)
TRAILER = re.compile(r"^Swarm-([\w-]+):\s*(.*)$", re.M)


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


def write_now(workers, c=None, *, state, ran_on, run, waiting_books, pool, live_workers, turn_round, run_status):
    """What the swarm is doing, for the panel's first line.

    The panel's top line was a list of counters — free requests, a spend meter, a corpus chunk
    count — none of which says whether the swarm is working or what on."""
    rows, live = [], live_workers(workers)
    for w in workers:
        task, since = getattr(w, "task", None), getattr(w, "started", 0)
        if not (task and since and w.is_alive()):
            continue
        rounds = turn_round(w.name)
        # `doing` is "<role> with <model>" during a turn, but plain prose between turns
        # ("running the tests (baseline)"). Splitting on the separator only when it is there
        # keeps the gate from being reported as the name of the model.
        role, _, model = (getattr(w, "doing", "") or "").partition(" with ")
        rows.append({
            "worker": w.name, "task": task.get("id"), "title": str(task.get("title", ""))[:120],
            "role": role or None,
            "model": ran_on.get(w.name) or model or None,
            "round": rounds and rounds[0], "rounds": rounds and rounds[1],
            "since": since, "seconds": round(time.time() - since),
            "doing": getattr(w, "doing", None), "waiting": waiting_books.get(w.name)})
        wvars = vars(w)
        if "state" in wvars:
            rows[-1]["state"] = wvars["state"]
        clock = wvars.get("clock")
        if isinstance(clock, BucketClock):
            rows[-1]["buckets"] = clock.snapshot()
    note = {"at": time.time(), "workers": rows}
    if c is not None and run.get("id"):
        note["run"] = run_status(rows, c)
    if not rows and c is not None:
        ledger = Ledger(state / "learn.json")
        resting = ledger.resting()
        if resting and all(m in {m for m, _, _ in resting} for m in pool(c)):
            first = min(u for _, u, _ in resting)
            note["idle"] = "all models resting"
            note["back"] = first
        else:
            # Only a worker that is still running can still be waiting. A reason left behind
            # by a finished or replaced worker used to sit in the panel's idle line for the
            # rest of the shift, describing an allowance hold that had long since cleared.
            held = sorted(why for name, why in waiting_books.items() if name in live)
            if held:
                note["idle"] = held[0]
    tmp = (state / "now.json").with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(note))
        tmp.replace(state / "now.json")
    except OSError:
        pass                              # a status line is never worth failing a run over
    return note


def run_status(workers, c, *, state_dir, progress_seen, read, run, run_lock, stop, turn_logs, mark_progress, queue_for, read_roadmap):
    now = time.time()
    for name, path in list(turn_logs.items()):
        try:
            count = len(re.findall(r"^tool: ", Path(path).read_text(errors="replace"), re.M))
        except OSError:
            continue
        if count > progress_seen.get(str(path), 0):
            progress_seen[str(path)] = count
            mark_progress(f"{name}: tool activity")
    pending = queue_for(c).pending()
    roots = list((read_roadmap() or {}).get("roots", [])) if c.get("roadmap_file") else []
    parked = [r for r in read(state_dir / "done.jsonl")
              if r.get("status") == "parked" and not r.get("dismissed_at")
              and r.get("finished", 0) >= run.get("started", now)]
    parked_ids = {r["id"] for r in parked}
    roots += [r["id"] for r in parked if not parked_ids.intersection(r.get("depends_on", []))]
    with run_lock:
        state, reason = "Waiting", "No ready assigned work"
        if stop.is_set():
            state, reason = "Stopped", run.get("stop_reason") or "Stop requested"
        elif run.get("state") == "Draining":
            state, reason = "Draining", run.get("stop_reason")
        elif workers:
            # Work order §13: a worker is Waiting only when its structured state says it
            # is idle/blocked; the prose `waiting` string is no longer the signal. An
            # allowance hold banks real blocked seconds instead of reading as a long task.
            states = [w.get("state") for w in workers]
            state = "Waiting" if all(s in (None, "idle", "allowance_blocked") for s in states) else "Running"
            reason = workers[0].get("doing") or workers[0].get("state") or "No ready assigned work"
        elif roots or (pending and not queue_for(c).ready()):
            state, reason = "Blocked", "Unmet dependencies or retry cooldown"
        # Wall-time buckets across live workers, banked by state (work order §13).
        buckets = {}
        for w in workers:
            clk = vars(w).get("clock") if not isinstance(w, dict) else None
            if not isinstance(clk, BucketClock):
                continue
            for k, v in clk.snapshot().items():
                buckets[k] = buckets.get(k, 0.0) + v
        if buckets:
            run["wall_buckets"] = {k: round(v, 1) for k, v in buckets.items()}
        run["wall_total"] = round(sum(buckets.values()), 1) if buckets else run.get("wall_total")
        # One human line: how the run's wall time actually split (work order §13). The total
        # approximately equals wall time; an allowance hold is its own bucket, not "waiting".
        if buckets:
            def pretty(secs):
                secs = int(max(0, secs))
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                if h:
                    return f"{h}h{m:02d}m"
                if m:
                    return f"{m}m"
                return f"{s}s"
            total = max(1, int(sum(buckets.values())))
            model = buckets.get("active_model", 0)
            hold = buckets.get("allowance_blocked", 0)
            tests = buckets.get("testing", 0) + buckets.get("reviewing", 0)
            idle = buckets.get("idle", 0) + buckets.get("dependency_blocked", 0)
            run["time_summary"] = (
                f"{pretty(total)} wall: {pretty(model)} model work, {pretty(hold)} allowance hold, "
                f"{pretty(tests)} tests/review, {pretty(idle)} idle/blocked")
        if state == "Blocked":
            run.setdefault("blocked_since", now)
        elif run.get("blocked_since"):
            run["blocked_seconds"] = run.get("blocked_seconds", 0) + now - run.pop("blocked_since")
        run.update(state=state, reason=reason, heartbeat_at=now,
                    remaining_seconds=max(0, run["deadline"] - now) if run.get("deadline") else None)
        result = dict(run)
        result["progress_age"] = now - run["last_progress_at"] if run.get("last_progress_at") else None
        result["blocked_duration"] = run.get("blocked_seconds", 0) + (now - run["blocked_since"] if run.get("blocked_since") else 0)
        result["root_blockers"] = roots
        result["blocked_tasks"] = [{"id": r["id"], "title": r["title"], "reason": r.get("note"),
                                    "next_action": "Inspect retained attempt; repair scope, verifier or reviewer before requeueing"}
                                   for r in parked]
        temp = state_dir / "run.tmp"
        temp.write_text(json.dumps(result, indent=2))
        temp.replace(state_dir / "run.json")
        return result


def landed_commits(c, limit=20, *, state, read, git, trunk_name):
    """The trunk commits this swarm landed, newest first, with who wrote them and what for."""
    # Trunk itself, not base..trunk: once trunk is merged, base..trunk is empty and the swarm's
    # own history would read as nothing landed. A Swarm-Task trailer is what makes it the
    # swarm's, so the merge cannot hide it either.
    rc, out = git(["log", f"--format=%H%x1f%h%x1f%s%x1f%at%x1f%b%x1e", "-n", str(limit * 4),
                   trunk_name(c)], cwd=c["repo"])
    if rc != 0 or not out.strip():
        return []
    rows = [r for r in read(state / "journal.jsonl") if r["event"] in ("attempt", "reward")]
    by_task = {}
    for r in rows:
        by_task.setdefault(r.get("id"), {}).update(
            {k: v for k, v in r.items() if k in ("reward", "goal_item", "usd", "implementer")})
    commits = []
    for chunk in out.split(""):
        parts = chunk.strip().split("")
        if len(parts) < 4:
            continue
        sha, short, subject, at = parts[0], parts[1], parts[2], parts[3]
        trailers = dict(TRAILER.findall(parts[4] if len(parts) > 4 else ""))
        if "Task" not in trailers:
            continue                  # someone's own commit that trunk happens to contain
        meta = by_task.get(trailers.get("Task"), {})
        commits.append({
            "sha": sha, "short": short, "at": int(at) if at.isdigit() else 0,
            "subject": re.sub(r"^swarm: ", "", subject),
            "task": trailers.get("Task"),
            "model": trailers.get("Implementer") or meta.get("implementer"),
            "reviewer": trailers.get("Reviewer"),
            "reward": meta.get("reward"), "goal_item": meta.get("goal_item"),
            "usd": meta.get("usd")})
        if len(commits) >= limit:
            break
    return commits


def build_report(c, hours=24, *, state, read, git, trunk_name, promotion_state, reportable):
    L = Ledger(state / "learn.json").snapshot()
    since = time.time() - hours * 3600
    repo, base, t = c["repo"], c.get("base_branch", "main"), trunk_name(c)
    _, ahead = git(["rev-list", "--count", f"{base}..{t}"], cwd=repo)
    _, commits = git(["log", "--format=- %h %s", "-n", "30", f"{base}..{t}"], cwd=repo)
    accepted = [a for a in L["accepted"] if a["t"] >= since]
    rows = read(state / "journal.jsonl")
    tasks = [j for j in rows if j["event"] == "task" and j["t"] >= since]
    attempts = [j for j in rows if j["event"] == "attempt" and j["t"] >= since]
    stages, classes = {}, {}
    for j in tasks:
        stages[j.get("stage", "?")] = stages.get(j.get("stage", "?"), 0) + 1
        if not j.get("ok") and j.get("failure_class"):
            classes[j["failure_class"]] = classes.get(j["failure_class"], 0) + 1
    parked = [d for d in read(state / "done.jsonl")
              if d.get("status") in ("parked", "split") and not d.get("dismissed_at")
              and d.get("finished", 0) >= since]
    # Each task's own last outcome, so the report says what happened rather than guessing.
    last = {j["id"]: j for j in rows if j["event"] == "task"}
    # What the window cost, from OpenRouter's own numbers rather than a local price table.
    spent = round(sum(a.get("usd") or 0.0 for a in attempts), 6)
    requests = sum(int(a.get("requests") or 0) for a in attempts)
    landed_n = sum(1 for a in attempts if a.get("stage") == "accepted")
    by_model = {}
    for a in attempts:
        m = by_model.setdefault(a.get("implementer") or "?", {"attempts": 0, "landed": 0,
                                                                 "requests": 0, "usd": 0.0})
        m["attempts"] += 1
        m["landed"] += a.get("stage") == "accepted"
        m["requests"] += int(a.get("requests") or 0)
        m["usd"] = round(m["usd"] + (a.get("usd") or 0.0), 6)
    b = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
               owner_window=c.get("owner_window", ["00:00", "00:00"])).snapshot()

    out = [f"# Swarm report — {Path(repo).name}",
           f"_{dt.datetime.now():%Y-%m-%d %H:%M}, last {hours:g}h_", "",
           f"**{t}** is {ahead or 0} commit(s) ahead of **{base}**. Nothing has been merged into your branches.",
           "", "## Promotion", "",
           "Landed on `{t}` but **not** merged or deployed — that is the point of the "
           "swarm trunk: review and merge when you like it.".format(t=t),
           "", "```",
           f"accepted/landed {int((promotion_state(c) or {}).get('landed', 0))}; "
           f"merged to {base} {int((promotion_state(c) or {}).get('merged', 0))}; "
           f"verified live {int((promotion_state(c) or {}).get('verified_live', 0))}",
           (promotion_state(c) or {}).get("verified_live_note", ""),
           "```", "",
           "```sh", f"git -C '{repo}' log --oneline {base}..{t}",
           f"git -C '{repo}' diff {base}...{t}",
           f"git -C '{repo}' merge {t}   # from your {base} checkout, when you like it", "```", ""]
    brk = [a for a in accepted if a.get("breakthrough")]
    out += ["## Breakthroughs", ""]
    out += [f"- ★ **{a['title']}** — reward {a['reward']:.2f}, `{a['implementer']}`, "
            f"{a.get('persona') or a.get('origin')}: {(a.get('scores') or {}).get('why', '')}"
            for a in brk] or ["- none this window"]
    out += ["", f"## Landed ({len(accepted)})", "",
            "| reward | impact | creativity | quality | task | implementer | idea from |",
            "|---:|---:|---:|---:|---|---|---|"]
    for a in sorted(accepted, key=lambda a: -a["reward"]):
        s = a.get("scores") or {}
        out.append(f"| {a['reward']:.2f} | {s.get('impact', '–')} | {s.get('creativity', '–')} | "
                   f"{s.get('quality', '–')} | {a['title']} | `{a['implementer']}` | "
                   f"{a.get('persona') or a.get('origin')} |")
    out += ["", "## Outcomes", "", ", ".join(f"{k}: {v}" for k, v in sorted(stages.items())) or "no tasks finished"]
    if classes:
        out += ["", "Whose failures these were: "
                + ", ".join(f"{k} {v}" for k, v in sorted(classes.items(), key=lambda kv: -kv[1]))
                + ". Only `task` failures say the work was wrong."]
    out += ["", "## Efficiency", "",
            f"{landed_n} accepted / {len(tasks)} completed outcomes / {len(attempts)} model attempts / "
            f"{requests} observed request starts.",
            (f"{requests / landed_n:.1f} observed requests per accepted commit."
             if landed_n else "No accepted commit denominator in this window."),
            "", "First-pass accepted: "
            + str(sum(1 for a in attempts if a.get("stage") == "accepted" and not a.get("repairs")))
            + "; accepted after retry: "
            + str(sum(1 for a in attempts if a.get("stage") == "accepted" and a.get("repairs")))
            + ".",
            "", "## Cost", ""]
    if spent:
        out += [f"${spent:.4f} across {len(attempts)} attempt(s); "
                + (f"${spent / landed_n:.4f} per landed commit ({landed_n} landed)."
                   if landed_n else "nothing landed in this window."),
                "", "| model | attempts | landed | $ | $ per landed |", "|---|---:|---:|---:|---:|"]
        for name, m in sorted(by_model.items(), key=lambda kv: -kv[1]["usd"]):
            per = f"{m['usd'] / m['landed']:.4f}" if m["landed"] and m["usd"] else "–"
            out.append(f"| `{name}` | {m['attempts']} | {m['landed']} | {m['usd']:.4f} | {per} |")
    else:
        out += ["No paid turns were charged in this window."]
    # An experiment nobody is running is a section that says nothing, every morning.
    if (c.get("mit_experiment") or {}).get("enabled") and c.get("inject_corpus", True):
        import experiment
        out += ["", "## MIT corpus experiment (every attempt so far)", "",
                experiment.markdown(experiment.summary(state / "journal.jsonl"))]
    if parked:
        out += ["", "## Split or parked (needs a human look)", "",
                "One line each, with whose failure it was. `harness` and `model` mean the task "
                "itself was never shown to be wrong.", ""]
        for d in parked:
            j = last.get(d["id"], {})
            stage = j.get("stage") or ""
            raw = j.get("note") or d.get("note") or ""
            # Classify the note as it was written: cleaning it for display strips the very
            # words — sandbox, the timeout, the path — that say whose failure it was.
            why = j.get("failure_class") or failure_class(stage, raw,
                                                          edited=j.get("edit_calls"))
            note = reportable(raw)
            out.append(f"- **{d['status']}** · {why}"
                       + (f" · {stage}" if stage else "")
                       + f" · {d['title']} — {note[:140] or 'no reason recorded'}")
    out += ["", "## Agent leaderboard", "", "| role | arm | pulls | mean reward | posterior |", "|---|---|---:|---:|---:|"]
    for row in Ledger(state / "learn.json").leaderboard():
        out.append(f"| {row['role']} | `{row['arm']}` | {row['pulls']} | {row['mean']:.2f} | {row['posterior']:.2f} |")
    lessons, pitfalls = Ledger(state / "learn.json").playbook()
    out += ["", "## Playbook", "", format_playbook(lessons, pitfalls) or "(empty so far)", "",
            "## Budget", "", f"{b['spent_today']}/{b['usable']} usable free requests spent today; "
            f"resets {b['resets_local']}.", "", "## Trunk commits", "", commits or "- none yet"]
    text = "\n".join(out) + "\n"
    (state / "REPORT.md").write_text(text)
    return text
