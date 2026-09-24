#!/usr/bin/env python3
"""JSON bridge between an editor (the Flint Swarm extension for Cursor/VS Code) and the swarm.

Every command prints JSON. `ask` streams one JSON object per line as models finish.

    bridge.py info
    bridge.py status   --repo PATH
    bridge.py ask      --repo PATH --question Q [--file F --start N --end M --selection-file S]
                       [--models 3 | --model a:free,b:free] [--no-synthesis] [--no-study]
    bridge.py task     --repo PATH --title T [--detail D] [--file F --start N --end M --selection-file S]
    bridge.py study    --query Q [-k 6]
    bridge.py report   --repo PATH [--hours 24]
    bridge.py vote     --model M --useful 1|0
    bridge.py grind-cmd --repo PATH [--goal G] [--test-cmd T] [--hours H]
    bridge.py stop

`ask` runs read-only flint agents (read_file, list_files, search, study; no edits, no shell)
in the macOS sandbox, several free models in parallel, then one more model merges their
answers. Answers the user marks useful reinforce that model for later questions (Thompson
sampling, role "consult"). Requests count against the same daily allowance as the swarm but
not against the swarm's reserve, which exists for exactly this kind of use.
"""
import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)   # the sandbox cannot read .env; children get the key via env

import sandbox  # noqa: E402
import swarmd  # noqa: E402
from learn import Ledger  # noqa: E402

CONSULT = HERE / "state" / "consult" / "learn.json"
MAX_SELECTION = 12_000
_children, _children_lock, _out_lock = set(), threading.Lock(), threading.Lock()


def emit(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _kill_children(*_):
    with _children_lock:
        for p in list(_children):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    os._exit(143)


def config_for(repo=None):
    """The swarm config, pointed at `repo` when given. Only the configured repo is ground by
    a running daemon; other repos still get their own queue and state."""
    c = swarmd.load_cfg()
    c.setdefault("python", sys.executable)
    if str(c.get("python", "")).startswith("__"):
        c["python"] = sys.executable
    if repo:
        repo = str(Path(repo).expanduser().resolve())
        if c.get("repo") != repo:
            branch = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
            c = dict(c, repo=repo, base_branch=branch if branch and branch != "HEAD" else "main")
    return c


def is_target(repo):
    configured = swarmd.load_cfg().get("repo", "")
    try:
        return Path(configured).expanduser().resolve() == Path(repo).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def daemon_running():
    """True when a swarm daemon holds this repo's lock (swarmd.use_repo must have run)."""
    lock = swarmd.STATE / "daemon.lock"
    if not lock.exists():
        return False
    import fcntl
    with open(lock, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def selection_context(repo, file=None, start=None, end=None, selection_file=None):
    """(label, code) for the code the user pointed at: their selection, else the file's lines."""
    if not file:
        return None, None
    path = Path(file).expanduser()
    try:
        rel = str(path.resolve().relative_to(Path(repo).resolve()))
    except ValueError:
        rel = str(path)
    code = ""
    if selection_file:
        code = Path(selection_file).read_text(errors="replace")
    elif path.is_file():
        lines = path.read_text(errors="replace").splitlines()
        a = max(1, int(start or 1))
        b = min(len(lines), int(end or len(lines)))
        code = "\n".join(lines[a - 1:b])
    if len(code) > MAX_SELECTION:
        code = code[:MAX_SELECTION] + f"\n… [{len(code) - MAX_SELECTION} more characters not shown; read the file]"
    where = rel + (f" lines {start}-{end}" if start and end else "")
    return where, code


# ------------------------------------------------------------------ ask

ASK = """You are one of several independent models a developer asked about their code in Cursor.
Answer on your own merits; the answers will be compared.

QUESTION
{question}
{context}
You are in the repository root and can read files (read_file, list_files, search) to check
surrounding code before answering; do it rather than guessing.{study}

Be concrete: cite file:line for claims about the code, show corrected code when you find a bug,
and when a technique comes from the MIT material, name the course and lecture. If you are unsure,
say what would settle it. Keep the answer under 300 words unless code is needed."""

STUDY_HINT = ("\nA `study` tool searches MIT OpenCourseWare lecture cards and source pages "
              "(algorithms, Python, machine learning, probability, finance, economics...). Use it "
              "when a known algorithm, data structure or model applies, then read_file the source "
              "page it cites before relying on it.")

SYNTH = """Several models answered a developer's question about their code. Write the single best
answer for the developer.

QUESTION
{question}
{context}
ANSWERS
{answers}

Check the claims that matter against the code with read_file before keeping them. Keep what is
correct and useful, drop what is wrong (say briefly why), and point out real disagreements.
Credit the model behind each key point in brackets, e.g. [qwen/qwen3.8-27b:free]. Under 300 words
unless code is needed."""


def run_flint(prompt, repo, model, steps, study=True, timeout=420, on_progress=None):
    """One read-only flint turn. on_progress receives its "round …"/"tool: …" lines as they happen."""
    env = dict(os.environ, FLINT_MAX_STEPS=str(steps))
    env.pop("FLINT_SWARM_BUDGET", None)
    if not study:
        env["FLINT_CORPUS_DB"] = swarmd.NO_CORPUS
    cmd = [sys.executable, str(ROOT / "flint.py"), "-p", prompt, "-C", str(repo), "-m", model, "--read-only"]
    if sandbox.available():
        home = os.environ.get("FLINT_HOME", "~/.flint")
        cmd = sandbox.wrap(cmd, sandbox.profile([home]))
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                         start_new_session=True)
    with _children_lock:
        _children.add(p)
    lines, expired = [], threading.Event()

    def pump():
        for line in p.stderr:
            lines.append(line)
            s = line.strip()
            if on_progress and s.startswith(("round ", "tool: ", "step limit", "provider", "per-minute", "pacing")):
                on_progress(s)

    def expire():
        expired.set()
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    reader, timer = threading.Thread(target=pump, daemon=True), threading.Timer(timeout, expire)
    reader.start()
    timer.start()
    try:
        out = p.stdout.read()
        p.wait()
    finally:
        timer.cancel()
        reader.join(timeout=5)
        with _children_lock:
            _children.discard(p)
    err = "".join(lines)
    if expired.is_set():
        rounds = len(re.findall(r"^round \d+/", err, re.M))
        return {"ok": False, "model": model, "error": f"timed out after {timeout}s ({rounds} requests made)",
                "secs": round(time.time() - t0), "requests": rounds, "exit": None}
    studied = len(re.findall(r"^tool: study$", err or "", re.M))
    tools = len(re.findall(r"^tool: ", err or "", re.M))
    rounds = len(re.findall(r"^round \d+/", err or "", re.M))
    base = {"model": model, "secs": round(time.time() - t0), "study_calls": studied,
            "tool_calls": tools, "requests": rounds, "exit": p.returncode}
    if p.returncode == 0 and out.strip():
        return {"ok": True, "text": out.strip(), **base}
    reason = {3: "daily free-request cap reached", 4: "credit or account limit", 5: "ran out of steps",
              6: "budget pause", 7: "provider unavailable",
              8: "model busy (rate-limited upstream)"}.get(p.returncode, "failed")
    tail = "\n".join((err or "").strip().splitlines()[-3:])
    return {"ok": False, "error": f"{reason}: {tail[-400:]}", **base}


def pick_models(c, n, explicit=None):
    pool = swarmd.pool(c)
    if explicit:
        return [m for m in explicit if m in pool or c.get("allow_paid") or m.endswith(":free")][:8]
    ledger, chosen = Ledger(CONSULT), []
    CONSULT.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(min(n, len(pool))):
        m = ledger.pick("consult", pool, exclude=set(chosen))
        if m is None:
            break
        chosen.append(m)
    return chosen


def cmd_ask(a):
    repo = str(Path(a.repo).expanduser().resolve())
    c = config_for(repo)
    if not os.environ.get("OPENROUTER_API_KEY"):
        emit({"event": "error", "error": f"OPENROUTER_API_KEY is not set; add it to {ROOT / '.env'}"})
        return 2
    where, code = selection_context(repo, a.file, a.start, a.end, a.selection_file)
    context = ""
    if where:
        lang = Path(a.file).suffix.lstrip(".")
        context = f"\nCODE THE DEVELOPER POINTED AT: {where}\n```{lang}\n{code}\n```\n"
    corpus_ok = not a.no_study and Path(c.get("corpus_db") or "~/.flint/corpus.db").expanduser().is_file()
    models = pick_models(c, a.models, a.model.split(",") if a.model else None)
    if not models:
        emit({"event": "error", "error": "no usable models: every model in the pool is resting or the pool is empty"})
        return 2
    emit({"event": "context", "repo": repo, "where": where, "models": models, "study": corpus_ok})
    prompt = ASK.format(question=a.question.strip(), context=context, study=STUDY_HINT if corpus_ok else "")
    ledger, results = Ledger(CONSULT), []

    def one(model):
        emit({"event": "start", "model": model})
        try:
            r = run_flint(prompt, repo, model, a.steps, corpus_ok, a.timeout,
                          lambda s: emit({"event": "progress", "model": model, "text": s}))
        except Exception as e:   # a thread must report, not vanish
            r = {"ok": False, "model": model, "error": f"{type(e).__name__}: {e}"}
        if r["ok"]:
            ledger.warm(model)
        elif r.get("exit") in (7, 8):
            ledger.cool(model, r["error"], busy=r.get("exit") == 8)
        results.append(r)
        emit({"event": "answer" if r["ok"] else "error", **r})

    threads = [threading.Thread(target=one, args=(m,)) for m in models]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    good = [r for r in results if r["ok"]]
    if len(good) >= 2 and not a.no_synthesis:
        merger = pick_models(c, 1, None) or [good[0]["model"]]
        answers = "\n\n".join(f"--- {r['model']} ---\n{r['text'][:6000]}" for r in good)
        emit({"event": "start", "model": merger[0], "role": "synthesis"})
        r = run_flint(SYNTH.format(question=a.question.strip(), context=context, answers=answers),
                      repo, merger[0], a.steps, corpus_ok, a.timeout,
                      lambda s: emit({"event": "progress", "model": merger[0], "role": "synthesis", "text": s}))
        emit({"event": "synthesis" if r["ok"] else "error", "role": "synthesis", **r})
    emit({"event": "done", "answered": len(good), "asked": len(models),
          "requests": sum(r.get("requests", 0) for r in results)})
    return 0


# ------------------------------------------------------------------ other commands

def cmd_info(a):
    c = swarmd.load_cfg()
    db = Path(c.get("corpus_db") or "~/.flint/corpus.db").expanduser()
    corpus = None
    if db.is_file():
        import corpus_index
        corpus = corpus_index.stats(db)
    out = {"root": str(ROOT), "python": sys.executable, "config": str(swarmd.CONFIG),
           "configured_repo": None if str(c.get("repo", "__")).startswith("__") else c.get("repo"),
           "models": swarmd.pool(c), "allow_paid": bool(c.get("allow_paid")), "corpus": corpus,
           "sandbox": sandbox.available(), "api_key": bool(os.environ.get("OPENROUTER_API_KEY")),
           "mit_experiment": c.get("mit_experiment")}
    if a.quota:
        info = swarmd.account()
        out["quota"] = (info or {}).get("free_model_daily_requests")
    emit(out)
    return 0


def _journal_line(j):
    what = j.get("title") or j.get("model") or ""
    extra = j.get("stage") or j.get("role") or ""
    return f"{j.get('iso', '')[11:16]} {j.get('event', '')} {extra} {what}".strip()


def cmd_status(a):
    c = config_for(a.repo)
    swarmd.use_repo(c)
    q = swarmd.Queue()
    pending = q.pending()
    done = swarmd._read(q.done)
    ahead = None
    if subprocess.run(["git", "-C", c["repo"], "rev-parse", "--verify", "-q", f"refs/heads/{swarmd.trunk_name(c)}"],
                      capture_output=True).returncode == 0:
        _, ahead = swarmd.git(["rev-list", "--count", f"{c.get('base_branch', 'main')}..{swarmd.trunk_name(c)}"], cwd=c["repo"])
    import experiment
    exp = experiment.summary(swarmd.STATE / "journal.jsonl")
    budget = swarmd.Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
                           owner_window=c.get("owner_window", ["00:00", "00:00"])).snapshot()
    emit({"repo": c["repo"], "is_target": is_target(c["repo"]), "daemon_running": daemon_running(),
          "trunk": swarmd.trunk_name(c), "trunk_ahead": int(ahead) if str(ahead or "").isdigit() else None,
          "queue": [{"id": t["id"], "title": t["title"], "priority": t.get("priority", 0),
                     "claimed": bool(t.get("claimed")), "origin": t.get("origin")} for t in pending],
          "landed": sum(t.get("status") == "done" for t in done),
          "parked": sum(t.get("status") in ("parked", "split") for t in done),
          "budget": budget, "experiment": {"verdict": exp["verdict"], "on": exp["on"]["attempts"],
                                           "off": exp["off"]["attempts"]},
          "recent": [_journal_line(j) for j in swarmd._read(swarmd.STATE / "journal.jsonl")[-12:]]})
    return 0


def cmd_task(a):
    c = config_for(a.repo)
    swarmd.use_repo(c)
    where, code = selection_context(c["repo"], a.file, a.start, a.end, a.selection_file)
    want = (a.detail or a.title).strip()
    detail = want
    if where:
        detail += f"\n\nThe developer pointed at {where}:\n```\n{code[:4000]}\n```"
    try:
        t = swarmd.Queue(c.get("max_depth", 1), c.get("max_queue", 20)).add(
            a.title, detail, a.kind, priority=a.priority, origin="cursor", acceptance=[want])
    except ValueError as e:
        emit({"ok": False, "error": str(e)})
        return 2
    emit({"ok": bool(t), "id": t and t["id"], "duplicate_or_full": not t, "is_target": is_target(c["repo"]),
          "daemon_running": daemon_running()})
    return 0


def cmd_study(a):
    import mit_corpus
    emit({"query": a.query, "hits": mit_corpus.search(a.query, a.k, max_chars=a.chars)})
    return 0


def cmd_report(a):
    c = config_for(a.repo)
    swarmd.use_repo(c)
    try:
        text = swarmd.build_report(c, a.hours)
    except Exception as e:
        emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 2
    emit({"ok": True, "markdown": text})
    return 0


def cmd_vote(a):
    CONSULT.parent.mkdir(parents=True, exist_ok=True)
    Ledger(CONSULT).update("consult", a.model, 1.0 if a.useful else 0.0)
    emit({"ok": True, "model": a.model, "useful": bool(a.useful),
          "leaderboard": [r for r in Ledger(CONSULT).leaderboard() if r["role"] == "consult"][:10]})
    return 0


def cmd_grind_cmd(a):
    parts = [sys.executable, str(HERE / "swarmd.py"), "grind", str(Path(a.repo).expanduser().resolve())]
    if a.goal:
        parts += ["--goal", a.goal]
    if a.test_cmd:
        parts += ["--test-cmd", a.test_cmd]
    if a.hours:
        parts += ["--hours", str(a.hours)]
    emit({"command": " ".join(shlex.quote(p) for p in parts)})
    return 0


def cmd_stop(a):
    """SIGINT to running swarm daemons (the same as Ctrl-C in their terminal)."""
    out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True).stdout
    me, stopped = os.getpid(), []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != me and re.search(r"swarmd\.py\s+(grind|run)\b", args):
            try:
                os.kill(int(pid), signal.SIGINT)
                stopped.append(int(pid))
            except (ProcessLookupError, PermissionError):
                pass
    emit({"stopped": stopped})
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info")
    s.add_argument("--quota", action="store_true", help="also ask OpenRouter for today's free-request usage")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("status")
    s.add_argument("--repo", required=True)
    s.set_defaults(fn=cmd_status)
    for name, fn in (("ask", cmd_ask), ("task", cmd_task)):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True)
        s.add_argument("--file")
        s.add_argument("--start", type=int)
        s.add_argument("--end", type=int)
        s.add_argument("--selection-file")
        s.set_defaults(fn=fn)
        if name == "ask":
            s.add_argument("--question", required=True)
            s.add_argument("--models", type=int, default=3)
            s.add_argument("--model", help="comma-separated model ids instead of sampling")
            s.add_argument("--steps", type=int, default=8)
            s.add_argument("--timeout", type=int, default=420, help="seconds per model (free models can be slow)")
            s.add_argument("--no-synthesis", action="store_true")
            s.add_argument("--no-study", action="store_true")
        else:
            s.add_argument("--title", required=True)
            s.add_argument("--detail", default="")
            s.add_argument("--kind", default="feature", choices=swarmd.KINDS)
            s.add_argument("--priority", type=int, default=1)
    s = sub.add_parser("study")
    s.add_argument("--query", required=True)
    s.add_argument("-k", type=int, default=6)
    s.add_argument("--chars", type=int, default=1600)
    s.set_defaults(fn=cmd_study)
    s = sub.add_parser("report")
    s.add_argument("--repo", required=True)
    s.add_argument("--hours", type=float, default=24)
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("vote")
    s.add_argument("--model", required=True)
    s.add_argument("--useful", type=int, choices=(0, 1), required=True)
    s.set_defaults(fn=cmd_vote)
    s = sub.add_parser("grind-cmd")
    s.add_argument("--repo", required=True)
    s.add_argument("--goal")
    s.add_argument("--test-cmd")
    s.add_argument("--hours", type=float)
    s.set_defaults(fn=cmd_grind_cmd)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _kill_children)
    try:
        return a.fn(a)
    except SystemExit:
        raise
    except Exception as e:
        emit({"event": "error", "ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
