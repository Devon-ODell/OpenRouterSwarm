#!/usr/bin/env python3
"""A self-reinforcing coding swarm on free OpenRouter models. Runs until stopped.

Each task: implement → tests → adversarial review → tests → land on the
swarm's trunk branch → judge → reinforce. Later tasks start from trunk, so the
work compounds. Failed tasks are retried with their failure notes, then split
into smaller tasks; strong results earn follow-ups. Models and planner
personas are chosen by Thompson sampling on the rewards they earn, and every
accepted change leaves a lesson for the agents that come after it.

    swarm grind ~/code/project --goal "what it should become"   # nonstop
    swarm grind                                  # resume the configured target
    swarm run --hours 8                          # bounded run
    swarm report                                 # what happened while you were away
    swarm status | add | plan | models | wake

The supervisor never merges into your checkout: accepted work accumulates on
`trunk` (default swarm/trunk). Review with `git log main..swarm/trunk` and
merge what you want. Agents and tests run in a write-restricting macOS
sandbox; it contains accidents, not a determined attacker.
"""
import argparse, datetime as dt, fcntl, hashlib, json, os, random, re, shlex, shutil, signal
import subprocess, sys, threading, time, traceback, uuid
from contextlib import contextmanager
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # LingAI-Trader/
load_dotenv(ROOT / ".env", override=False)
sys.path.insert(0, str(HERE))
from budget import Budget               # noqa: E402
from learn import (Ledger, format_playbook, is_breakthrough, json_array,  # noqa: E402
                   novelty, parse_scores, reward, weight, defect, exhibit,
                   FAULTY, CRIME, BREAKTHROUGH_WEIGHT, PENALTY_WEIGHT)
from workflow import Attempt, STRATEGIES, REVIEW, REPAIR, contract, criteria, parse_review, failure_signature  # noqa: E501
import sandbox                          # noqa: E402

CONFIG = Path(os.environ.get("FLINT_SWARM_CONFIG") or HERE / "config.json").expanduser()
STATE = HERE / "state"                  # per target repo once use_repo() runs
LOGS = HERE / "logs"
SLUG = "default"
STATE.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

DEFAULT_MODEL = "inclusionai/ling-3.0-flash-fin:free"
KINDS = ("feature", "bugfix", "test", "refactor")
META = ("persona", "planner_model", "priority", "depth", "parent", "origin", "acceptance", "depends_on", "root", "strategy")
READ_ONLY_ROLES = {"planner", "architect", "judge", "decomposer", "adversary"}
MAX_ATTEMPTS = 2
SECRET_ENV = re.compile(r"API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|CREDENTIAL", re.I)
GIT_IDENT = {"GIT_AUTHOR_NAME": "flint swarm", "GIT_AUTHOR_EMAIL": "swarm@flint.invalid",
             "GIT_COMMITTER_NAME": "flint swarm", "GIT_COMMITTER_EMAIL": "swarm@flint.invalid"}

_print_lock = threading.Lock()
_journal_lock = threading.Lock()
_process_lock = threading.Lock()
_trunk_lock = threading.Lock()
_view_lock = threading.RLock()
_processes = set()
_stop = threading.Event()
_sync_failed = {}
_study_lock = threading.Lock()
_study_calls = {}
NO_CORPUS = os.path.join(os.devnull, "no-corpus.db")   # cannot exist: flint then hides `study`


# ------------------------------------------------------------------ plumbing

def log(msg, worker="swarm"):
    line = f"{dt.datetime.now():%H:%M:%S} [{worker}] {msg}"
    with _print_lock:
        print(line, flush=True)
    with open(LOGS / f"{dt.date.today()}.log", "a") as f:
        f.write(line + "\n")


def journal(event, **kw):
    rec = {"t": time.time(), "iso": dt.datetime.now().isoformat(timespec="seconds"),
           "event": event, **kw}
    with _journal_lock:
        with open(STATE / "journal.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


EXAMPLE = HERE / "config.example.json"


def load_cfg():
    """The live config, seeded from the committed template the first time. config.json is not
    tracked: the swarm rewrites it on every run, and a tracked file it rewrites cannot be
    updated with `git pull`."""
    if not CONFIG.exists() and EXAMPLE.is_file() and CONFIG.parent == EXAMPLE.parent:
        CONFIG.write_text(EXAMPLE.read_text())
        log(f"created {CONFIG} from {EXAMPLE.name}")
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def save_cfg(c):
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=2) + "\n")
    tmp.replace(CONFIG)


def cfg():
    c = load_cfg()
    if not c:
        sys.exit(f"missing {CONFIG} — run `swarm grind /path/to/repo` first")
    if any(str(c.get(k, "")).startswith("__") or not c.get(k) for k in ("repo", "test_cmd")):
        sys.exit("swarm is not configured — run `swarm grind /path/to/repo` first")
    c["repo"] = str(Path(c["repo"]).expanduser())
    c["python"] = python_for(c)
    if c.get("workers", 1) < 1:
        sys.exit("workers must be at least 1")
    return c


def python_for(c):
    """The interpreter for flint turns: the configured one if it exists, else this checkout's
    .venv, else the one running the swarm. A config copied from another checkout still works."""
    configured = str(c.get("python") or "")
    if configured and not configured.startswith("__") and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser())
    venv = ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def use_repo(c):
    """Queue, lessons, logs and worktrees are kept per target repository."""
    global STATE, LOGS, SLUG
    repo = str(Path(c["repo"]).expanduser().resolve())
    SLUG = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(repo).name) + "-" + \
        hashlib.sha1(repo.encode()).hexdigest()[:6]
    STATE = HERE / "state" / SLUG
    LOGS = HERE / "logs" / SLUG
    STATE.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)


def wt_root():
    return HERE / "wt" / SLUG


def pool(c):
    """Candidate models. Paid models are dropped unless allow_paid is set."""
    ms = c.get("models") or [c.get("model") or DEFAULT_MODEL]
    return [m for m in ms if c.get("allow_paid") or m.endswith(":free")]


def trunk_name(c):
    return c.get("trunk", "swarm/trunk")


def read_goal(c):
    for p in (STATE / "GOAL.md", Path(c["repo"]) / c.get("goal_file", "GOAL.md")):
        if p.is_file() and p.read_text().strip():
            return p.read_text().strip()[:4000]
    return "(no GOAL.md: infer the project's purpose from its README and code, then improve it)"


def clean_env():
    return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}


@contextmanager
def process(cmd, **kwargs):
    with _process_lock:
        if _stop.is_set():
            raise RuntimeError("swarm is stopping")
        p = subprocess.Popen(cmd, start_new_session=True, **kwargs)
        _processes.add(p)
    try:
        yield p
    finally:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        p.wait()
        with _process_lock:
            _processes.discard(p)


def shutdown():
    _stop.set()
    with _process_lock:
        for p in _processes:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def sh(cmd, cwd=None, timeout=900, env=None):
    with process(cmd, cwd=cwd, shell=isinstance(cmd, str),
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, env=env) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.communicate()
            raise
    return p.returncode, (out or "") + (err or "")


def git(args, cwd, check=False):
    # Agents can write inside worktrees; never let that reach hooks the supervisor runs.
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
    rc, out = sh(cmd, cwd=cwd, timeout=180, env={**os.environ, **GIT_IDENT})
    if check and rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out[-500:]}")
    return rc, out.strip()


def sandbox_profile(cwd, c):
    home = os.environ.get("FLINT_HOME", "~/.flint")
    return sandbox.profile([cwd, *sandbox.git_paths(cwd), home], c.get("sandbox_write", []))


def sandboxed(cmd, cwd, c):
    if c.get("sandbox") and sandbox.available():
        return sandbox.wrap(cmd, sandbox_profile(cwd, c))
    return cmd


def notify(c, title, msg):
    if c.get("notify") and sys.platform == "darwin" and shutil.which("osascript"):
        script = (f"display notification {json.dumps(msg[:200], ensure_ascii=False)} "
                  f"with title {json.dumps('flint swarm: ' + title, ensure_ascii=False)}")
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


# ------------------------------------------------------------------ queue

def _read(path):
    if not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def _write(path, rows):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(path)


class Queue:
    """File queue shared by daemon threads and separate CLI processes.

    Highest priority first, then oldest: 2 = breakthrough follow-up,
    1 = split of a failed task, 0 = planned or added by hand."""

    def __init__(self, max_depth=2, max_queue=20, max_children=3, max_descendants=12):
        self.lock = threading.Lock()
        self.path = STATE / "queue.jsonl"
        self.done = STATE / "done.jsonl"
        self.max_depth = max_depth
        self.max_queue, self.max_children, self.max_descendants = max_queue, max_children, max_descendants

    @contextmanager
    def locked(self):
        with self.lock, open(STATE / "queue.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def seen_titles(self):
        """Everything pending, done or parked — so the planner cannot loop."""
        with self.locked():
            return {r["title"].strip().lower() for r in _read(self.path) + _read(self.done)}

    def recent_titles(self, n=80):
        with self.locked():
            rows = sorted(_read(self.path) + _read(self.done), key=lambda r: r.get("created", 0))
        return list(dict.fromkeys(r["title"] for r in rows))[-n:]

    def add(self, title, detail="", kind="feature", **meta):
        if kind not in KINDS:
            raise ValueError("unsupported task kind; self-editing harness tasks are disabled")
        with self.locked():
            rows = _read(self.path)
            history = rows + _read(self.done)
            if len(rows) >= self.max_queue:
                return None
            if not title.strip() or title.strip().lower() in {r["title"].strip().lower() for r in history}:
                return None
            t = {"id": f"t{uuid.uuid4().hex[:12]}",
                 "title": title.strip(), "detail": detail.strip(), "kind": kind,
                 "attempts": 0, "created": time.time(), "priority": 0, "depth": 0}
            t.update({k: meta[k] for k in META if meta.get(k) is not None})
            criteria(t)
            known = {r["id"]: r for r in history}
            dependencies = t.get("depends_on", [])
            if not isinstance(dependencies, list) or any(not isinstance(d, str) or d not in known for d in dependencies):
                raise ValueError("dependencies must reference existing task IDs")
            parent = t.get("parent")
            if parent:
                if parent not in known:
                    raise ValueError("recursive tasks must reference an existing parent")
                t["depth"] = known[parent].get("depth", 0) + 1
                t["root"] = known[parent].get("root", parent)
                if (t["depth"] > self.max_depth
                        or sum(r.get("parent") == parent for r in history) >= self.max_children
                        or sum(r.get("root") == t["root"] and r["id"] != t["root"] for r in history) >= self.max_descendants):
                    return None
            else:
                t["depth"], t["root"] = 0, t["id"]
            rows.append(t)
            _write(self.path, rows)
            return t

    def claim(self):
        with self.locked():
            rows, now = _read(self.path), time.time()
            complete = {r["id"] for r in _read(self.done) if r.get("status") == "done"}
            ready = [r for r in rows if not r.get("claimed") and r.get("not_before", 0) <= now
                     and set(r.get("depends_on", [])).issubset(complete)]
            if not ready:
                return None
            task = max(ready, key=lambda r: (r.get("priority", 0), -r.get("created", 0)))
            task["claimed"] = now
            _write(self.path, rows)
            return task

    def ready(self):
        now = time.time()
        with self.locked():
            complete = {r["id"] for r in _read(self.done) if r.get("status") == "done"}
            return [r for r in _read(self.path) if not r.get("claimed") and r.get("not_before", 0) <= now
                    and set(r.get("depends_on", [])).issubset(complete)]

    def release(self, tid, ok, note="", defer=0):
        """Returns the task with its new status: done, retry, split or parked."""
        with self.locked():
            rows, task = _read(self.path), None
            for r in rows:
                if r["id"] == tid:
                    task = r
                    break
            if not task:
                return None
            rows = [r for r in rows if r["id"] != tid]
            task.pop("claimed", None)
            task["attempts"] = task.get("attempts", 0) + (0 if defer else 1)
            task["note"] = note[-800:]
            if defer:
                task["status"] = "retry"
                task["not_before"] = time.time() + defer
                rows.append(task)
            elif ok:
                task["status"] = "done"
            else:
                # Failure notes travel with the task so the next attempt can learn from them.
                task["notes"] = (task.get("notes", []) + [note[-800:]])[-2:]
                if task["attempts"] < MAX_ATTEMPTS:
                    task["status"] = "retry"
                    # A failed attempt already cost real requests: back off 5m before retrying.
                    task["not_before"] = time.time() + 300 * task["attempts"] ** 2
                    rows.append(task)
                elif task.get("depth", 0) < self.max_depth:
                    task["status"] = "split"
                else:
                    task["status"] = "parked"
            task["finished"] = time.time()
            _write(self.path, rows)
            if task["status"] != "retry":
                with open(self.done, "a") as f:
                    f.write(json.dumps(task) + "\n")
            return task

    def pending(self):
        with self.locked():
            return _read(self.path)

    def recover(self, accepted=None):
        """Only called after acquiring the exclusive daemon lock."""
        with self.locked():
            rows = _read(self.path)
            accepted = accepted or {}
            done = _read(self.done)
            for task in rows:
                task.pop("claimed", None)
                if task["id"] in accepted:
                    task.update(status="done", note=f"Recovered integrated commit {accepted[task['id']]}", finished=time.time())
                    if not any(r["id"] == task["id"] for r in done):
                        done.append(task)
            _write(self.done, done)
            _write(self.path, [r for r in rows if r["id"] not in accepted])


# ------------------------------------------------------------------ corpus

def study(q, c, k=None):
    """MIT OCW excerpts for a prompt: lecture cards, then source pages, as absolute paths.
    Empty unless a lecture card matches, since tangential pages only distract small models.
    Local BM25 — costs no API quota."""
    if not c.get("corpus_db") or not Path(c["corpus_db"]).expanduser().is_file():
        return ""
    try:
        import mit_corpus
        return mit_corpus.study(q, k or c.get("corpus_k", 4), Path(c["corpus_db"]).expanduser(),
                                c.get("corpus_root"), max_chars=1200, require_card=True)
    except Exception as e:
        log(f"corpus unavailable: {e}")
        return ""


def count_study(worker, n=0, reset=False):
    """Study-tool calls made by one worker's turns since its attempt began."""
    with _study_lock:
        if reset:
            return _study_calls.pop(worker, 0)
        _study_calls[worker] = _study_calls.get(worker, 0) + n
        return _study_calls[worker]


def mit_arm(c, rng=random):
    """Which side of the MIT-corpus experiment an attempt is on: "on" (excerpts in the prompt
    and the study tool), "off" (neither), or "none" when there is no corpus to test."""
    if not c.get("corpus_db") or not Path(c["corpus_db"]).expanduser().is_file():
        return "none"
    exp = c.get("mit_experiment") or {}
    if not exp.get("enabled", True):
        return "on"
    return "on" if rng.random() < float(exp.get("share_on", 0.5)) else "off"


# ------------------------------------------------------------------ flint

class CapReached(Exception):
    pass


class NoCredits(Exception):
    pass


class StepLimit(Exception):
    pass


class ModelError(RuntimeError):
    """The model answered badly: malformed, empty or crashed turn."""


class ProviderDown(Exception):
    """The model's providers are unavailable; not a judgement of its quality."""

    def __init__(self, model, msg):
        super().__init__(msg)
        self.model = model


class ProviderBusy(ProviderDown):
    """The model is rate-limited upstream: it works, but another model should take this turn."""


class ModelGone(ProviderDown):
    """This API key cannot use the model at all (403/404). Waiting will not bring it back."""


def rest(ledger, e):
    """Rest the model behind a ProviderDown; returns a phrase for the log."""
    busy, gone = isinstance(e, ProviderBusy), isinstance(e, ModelGone)
    lines = [l for l in str(e).strip().splitlines() if l.strip()]
    why = lines[-1] if lines else ""
    until = ledger.cool(e.model, why, busy=busy, permanent=gone)
    if gone:
        # `swarm models --write` would put it back: OpenRouter lists it as free with tools,
        # and the refusal is particular to this key.
        return (f"{e.model} is not available to this API key — dropping it for this run. "
                f"Delete it from the \"models\" list in {CONFIG}: {why[:200]}")
    return (f"{e.model} {'is busy (rate-limited upstream)' if busy else 'is unavailable'}; "
            f"resting it until {dt.datetime.fromtimestamp(until):%H:%M:%S}")


def next_wake(ledger, c):
    """(model, until) for the pool model that stops resting first, or (None, 0)."""
    live = set(pool(c))
    return next(((m, until) for m, until, _ in ledger.resting() if m in live), (None, 0))


def all_resting(ledger, c):
    model, until = next_wake(ledger, c)
    if not model:
        return "every model is resting"
    return f"every model is resting; {model} is back at {dt.datetime.fromtimestamp(until):%H:%M:%S} (`swarm wake` ends the rest now)"


def flint(prompt, cwd, c, role, worker, budget, max_steps, model=None):
    """One headless flint turn, paced against the daily allowance."""
    model = model or (pool(c) or [DEFAULT_MODEL])[0]
    while True:
        if _stop.is_set():
            raise RuntimeError("swarm is stopping")
        ok, wait, why = budget.check()
        if ok:
            break
        log(f"{role}: holding {wait/60:.1f}m — {why}", worker)
        _stop.wait(max(1, min(wait, 60)))

    env = dict(os.environ)
    env["FLINT_MAX_STEPS"] = str(max_steps)
    if not env.get("OPENROUTER_API_KEY"):
        env.pop("OPENROUTER_API_KEY", None)
    if c.get("study") is False:
        env["FLINT_CORPUS_DB"] = NO_CORPUS
    elif c.get("corpus_db"):
        env["FLINT_CORPUS_DB"] = str(Path(c["corpus_db"]).expanduser())
    env["FLINT_SWARM_BUDGET"] = json.dumps(dict(
        cap=budget.cap, reserve=budget.reserve, owner_window=c.get("owner_window", ["00:00", "00:00"])))
    cmd = [c.get("python", sys.executable), str(ROOT / "flint.py"),
           "-p", prompt, "-C", str(cwd), "-m", model,
           "--read-only" if role in READ_ONLY_ROLES else "--yolo"]
    cmd = sandboxed(cmd, cwd, c)
    log(f"{role}: asking {model} (up to {max_steps} rounds; free models take 1-7 min)", worker)
    t0 = time.time()
    # Keep progress/errors separate from the role's machine-readable answer.
    logfile = LOGS / f"{worker}-{role}-{time.time_ns()}.log"
    with open(logfile, "w") as err:
        with process(cmd, stdout=subprocess.PIPE, stderr=err,
                     text=True, env=env) as p:
            try:
                out, _ = p.communicate(timeout=c.get("turn_timeout", 1800))
            except BaseException:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.communicate()
                raise
    progress = logfile.read_text(errors="replace")
    studied = len(re.findall(r"^tool: study$", progress, re.M))
    count_study(worker, studied)
    journal("turn", role=role, worker=worker, model=model, rc=p.returncode,
            secs=round(time.time() - t0), chars=len(out), log=str(logfile), study_calls=studied)
    error = progress[-1200:]
    with open(logfile, "a") as f:  # keep the answer beside the progress for later review
        f.write(f"\n--- answer from {model} (exit {p.returncode}) ---\n{out}\n")
    if p.returncode in (3, 6):
        raise CapReached(error)
    if p.returncode == 4:
        raise NoCredits(error)
    if p.returncode == 5:
        raise StepLimit(error)
    if p.returncode == 7:
        raise ProviderDown(model, error)
    if p.returncode == 8:
        raise ProviderBusy(model, error)
    if p.returncode == 9:
        raise ModelGone(model, error)
    if p.returncode != 0:
        raise ModelError(f"{role} turn failed (rc={p.returncode}); log: {logfile}\n{error}")
    return out.strip()


# ------------------------------------------------------------------ roles

PERSONAS = {
    "builder": "Build what a user of this project would reach for next. Choose the features "
               "with the most value toward the goal and make them real.",
    "inventor": "Propose at least one idea nobody asked for that would make the owner say "
                "'oh, that's clever': a new capability, mechanic, tool or insight that fits the "
                "goal. Unusual is welcome; a gimmick that does not serve the goal is not.",
    "scholar": "Use the study corpus (the study tool searches MIT OpenCourseWare lecture cards, "
               "slides, notes, problem sets and transcripts on deep learning and machine learning, probability, matrix calculus, discrete math and combinatorics, algorithms and Python, mathematical finance, fintech, blockchain, risk and decision analysis, venture finance, microeconomics, game theory, public finance, perception and psychology, plus past code "
               "reviews; MIT OCW Courses/agent-skills/FLINT-INDEX.md maps topics to lectures). "
               "Propose tasks that apply a specific technique from it to this project, and name "
               "the course, lecture and source page in the detail.",
    "hardener": "Find where the project is fragile: unhandled inputs, missing tests, wrong "
                "assumptions, past review findings. Propose fixes that make the next features "
                "safer to build.",
    "refiner": "Make what already exists markedly better: faster, simpler, clearer, more "
               "pleasant to use. Remove friction a user would notice.",
}

def persona_text(name, c):
    """A persona's stance, with the MIT topic index as a path the planner can actually open."""
    text = PERSONAS[name]
    if name == "scholar":
        try:
            import mit_corpus
            index = Path(c.get("corpus_root") or mit_corpus.corpus_root()).expanduser() / mit_corpus.SKILLS / "FLINT-INDEX.md"
            if index.is_file():
                text = text.replace("MIT OCW Courses/agent-skills/FLINT-INDEX.md", str(index))
        except Exception:
            pass
    return text


ARCHITECT = """You are the ARCHITECT in an engineering swarm.

PROJECT GOAL
{goal}

TASK
{title}
{detail}

{corpus}

Inspect the repository to understand how it is actually built before proposing
anything. Then produce a SPEC and nothing else:

1. FILES — exact paths you expect to be created or modified.
2. BEHAVIOR — what the code must do, precisely enough that two people would
   build the same thing.
3. ACCEPTANCE — concrete test cases by name, each with the input and the
   expected output. These become real tests, so they must be checkable by
   running `{test_cmd}`.
4. RISK — the one thing most likely to go wrong.

Do NOT write implementation code. Do NOT edit any file. Keep the spec under
400 words. If the task is already satisfied by existing code, say exactly
"ALREADY SATISFIED" and explain in one sentence."""

IMPLEMENTER = """You are the IMPLEMENTER in an engineering swarm.

PROJECT GOAL
{goal}

TASK
{spec}
{previous}
{playbook}

{corpus}

Implement this task in the current repository. It is a checkout of the swarm's
trunk, which already contains the swarm's earlier accepted work: build on it.

Within the task's intent you have creative latitude. If a cleaner design or a
small extra touch makes the result genuinely better for the person using it,
do it and test it. Creativity that serves the goal is rewarded; scope creep
and churn are not.

Rules that are not negotiable:
- The command `{test_cmd}` must exit zero when you are finished. Run it
  yourself and keep working until it does.
- Do not delete, skip, relax or weaken any existing test to get a green run.
  If an existing test is genuinely wrong, leave it failing and say so.
- Add tests that prove the new behavior, including at least one edge case.
- Change as little as possible outside what the task needs.
- Do not commit, switch branches, merge or reset Git; the supervisor manages Git.
- No network calls from code or tests, no credentials, no placing of live orders.
- If a known algorithm or technique applies and a study tool is available, look it up.

When done, print a summary under 150 words: what you changed and the final
result of `{test_cmd}`."""

ADVERSARY = """You are the ADVERSARY in an engineering swarm.
Another agent just wrote this change. Your job is to find what is wrong with it,
not to be agreeable.

PROJECT GOAL
{goal}

SPEC
{spec}

DIFF
{diff}

{corpus}

Look specifically for: off-by-one and boundary errors; unhandled errors and nil
or None paths; concurrency and shared-state bugs; silent precision loss; tests
that assert almost nothing; tests that were weakened or removed to force a pass;
behavior that satisfies the letter of the spec but not its intent.

If you find a real defect, WRITE A TEST THAT FAILS because of it, and leave that
test in the repository. Then print "REJECT: <one line>".

If after genuinely trying you cannot break it, print "APPROVE: <one line>".
Do not approve merely because the suite is green — the suite is what you are
auditing. Do not rewrite the implementation or commit changes.
Your final response must be exactly one line starting APPROVE: or REJECT:."""

JUDGE = """You are the JUDGE for an autonomous engineering swarm. The change below
already passed the test suite and an adversarial reviewer and has landed. Score
it so the swarm learns which agents and ideas to reinforce.

PROJECT GOAL
{goal}

TASK
{title}
{detail}

REVIEWER VERDICT
{verdict}

DIFF
{diff}

Score each from 0 to 10:
- impact: how much closer this moves the project to its goal.
- creativity: originality that serves the goal, such as a new capability, a
  clever mechanism or an elegant simplification. Novelty that does not serve
  the goal scores low.
- quality: correctness, clarity, and how tightly the tests pin the behavior.
Calibrate: solid routine work scores 4 to 6. Reserve 8 and above for work that
unlocks something new or is notably elegant. Set breakthrough to true only if
this changes what the project can do; expect that for fewer than 1 in 10 changes.

Give one lesson: a general, actionable sentence that would help a future agent
working in this repository (advice, not a summary of this change). Suggest up to
two follow-up tasks this change makes possible, with acceptance criteria.

You may read files to check your judgement. Output ONLY this JSON:
{{"impact": 0, "creativity": 0, "quality": 0, "breakthrough": false, "why": "one sentence", "lesson": "one sentence", "follow_ups": [{{"title": "...", "detail": "..."}}]}}"""

PLANNER = """You are the PLANNER for an autonomous engineering swarm that works around the clock.

PROJECT GOAL
{goal}

YOUR STANCE THIS ROUND: {persona_name}
{persona}

ALREADY LANDED ON THE SWARM'S TRUNK (newest first)
{landed}

BREAKTHROUGHS TO BUILD ON
{breakthroughs}

{playbook}

{corpus}

ALREADY PROPOSED OR ATTEMPTED — do not propose any of these again, in any wording:
{seen}

The repository in front of you is the swarm's trunk. Inspect it (at most {steps}
tool calls), compare it with the goal, then propose the next {n} tasks.

Rules:
- Each task must be completable in one focused sitting (well under 25 tool
  calls) and verifiable by running `{test_cmd}`.
- Put concrete acceptance criteria in the detail: inputs, expected behavior and
  the tests that prove it.
- Tasks run one after another on top of accepted work, but each must be
  valuable and testable on its own.
- Nothing that needs credentials, live trading, or network access to real services.

Output ONLY a JSON array, no prose around it:
[{{"title": "...", "detail": "...", "kind": "feature|bugfix|test|refactor"}}]"""

DECOMPOSER = """You are the DECOMPOSER in an engineering swarm. This task failed twice.
Split it into 2 or 3 smaller tasks that together achieve it, each small enough
to succeed on its own.

PROJECT GOAL
{goal}

TASK
{title}
{detail}

WHY THE ATTEMPTS FAILED
{notes}

Inspect the repository briefly first. The first subtask should be the smallest step that
makes real progress. Each subtask must be verifiable by running `{test_cmd}` and
have concrete acceptance criteria. If the task is impossible or misguided,
output [].

Output ONLY a JSON array, no prose around it:
[{{"title": "...", "detail": "...", "kind": "feature|bugfix|test|refactor"}}]"""


# ------------------------------------------------------------------ gate

def run_gate(cwd, c):
    cmd = sandboxed(c["test_cmd"], cwd, c)
    rc, out = sh(cmd, cwd=cwd, timeout=c.get("test_timeout", 900), env=clean_env())
    if c.get("_gate_log"):
        Path(c["_gate_log"]).write_text(f"command: {c['test_cmd']}\nexit: {rc}\n{out}")
    return rc == 0, out[-4000:]


def weakened_tests(diff):
    """Heuristic: did the diff strip assertions out of existing test files?"""
    removed, in_test = 0, False
    for line in diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            low = line.lower()
            in_test = any(m in low for m in ("test", "spec_", "_spec"))
        elif in_test and line.startswith("-") and not line.startswith("---"):
            if any(m in line for m in ("assert", "require", "expect", "t.Error",
                                       "t.Fatal", "should")):
                removed += 1
    return removed


def last_error(output):
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    for l in reversed(lines):
        if re.search(r"error|fail|assert|exception|panic", l, re.I):
            return l[:200]
    return lines[-1][:200] if lines else "no output"


# ------------------------------------------------------------------ trunk

@contextmanager
def trunk_locked():
    with _trunk_lock, open(STATE / "trunk.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def ensure_trunk(c):
    t = trunk_name(c)
    with trunk_locked():
        rc, _ = git(["rev-parse", "--verify", "-q", f"refs/heads/{t}"], cwd=c["repo"])
        if rc != 0:
            git(["branch", t, c.get("base_branch", "main")], cwd=c["repo"], check=True)
            log(f"created {t} from {c.get('base_branch', 'main')}")


def checked_out_at(c, branch):
    _, out = git(["worktree", "list", "--porcelain"], cwd=c["repo"])
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def refresh_view(c):
    """A detached, read-only checkout of trunk for the planner and decomposer."""
    ensure_trunk(c)
    view = wt_root() / "_view"
    if (view / ".git").exists():
        git(["checkout", "-q", "--detach", "-f", trunk_name(c)], cwd=view, check=True)
        git(["clean", "-fdq"], cwd=view)
    else:
        git(["worktree", "prune"], cwd=c["repo"])
        view.parent.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "--detach", str(view), trunk_name(c)], cwd=c["repo"], check=True)
    return view


def sync_trunk(c):
    """Bring new commits from base_branch into trunk when they merge cleanly and stay green."""
    repo, base, t = c["repo"], c.get("base_branch", "main"), trunk_name(c)
    ensure_trunk(c)
    rc, _ = git(["merge-base", "--is-ancestor", base, t], cwd=repo)
    if rc == 0:
        return "current"
    _, base_sha = git(["rev-parse", base], cwd=repo)
    if _sync_failed.get(base) == base_sha:
        return "skipped"
    with trunk_locked():
        _, old = git(["rev-parse", t], cwd=repo, check=True)
        tmp = wt_root() / f"_sync-{uuid.uuid4().hex[:6]}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "--detach", str(tmp), old], cwd=repo, check=True)
        try:
            rc, out = git(["merge", "--no-edit", "-m", f"swarm: sync {base} into {t}", base], cwd=tmp)
            if rc != 0:
                git(["merge", "--abort"], cwd=tmp)
                log(f"{base} has changes that conflict with {t}; continuing on {t} without them")
                _sync_failed[base] = base_sha
                return "conflict"
            ok, output = run_gate(tmp, c)
            if not ok:
                log(f"merging {base} into {t} fails the tests; continuing without it")
                _sync_failed[base] = base_sha
                return "red"
            if checked_out_at(c, t):
                return "checked-out"
            _, new = git(["rev-parse", "HEAD"], cwd=tmp, check=True)
            git(["update-ref", f"refs/heads/{t}", new, old], cwd=repo, check=True)
            log(f"synced new {base} commits into {t}")
            return "synced"
        finally:
            git(["worktree", "remove", "--force", str(tmp)], cwd=repo)


# ------------------------------------------------------------------ worker

def report_progress(workers, said, every=180):
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
                f"{getattr(w, 'doing', '?')}", w.name)


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


class Worker(threading.Thread):
    def __init__(self, idx, c, q, budget, stop, ledger=None, tally=None):
        super().__init__(daemon=True, name=f"w{idx}")
        self.idx, self.c, self.q, self.budget, self.stop = idx, c, q, budget, stop
        self.ledger = ledger or Ledger(STATE / "learn.json")
        self.tally = tally or Tally()
        self.branch = f"swarm/w{idx}"
        self.wt = None
        self.stage = None
        self.task = None
        self.last_model = None

    # -- helpers -----------------------------------------------------------

    def pick(self, exclude=()):
        """Implementers by Thompson sampling. Reviewers and judges come from the
        same posterior, excluding the implementer so nobody grades their own work."""
        return self.ledger.pick("implementer", pool(self.c), exclude=exclude)

    def call(self, role, prompt, cwd, steps, model, avoid=()):
        """One role turn. If the model is busy or down, the turn goes to another model (never
        one in `avoid`, so nobody grades their own work) instead of abandoning the attempt.
        A role that edits files is handed over only while it has changed nothing, so partial
        work is never finished by a different author. self.last_model names the model that
        took the turn last, whether or not it succeeded."""
        self.last_model = model
        if self.evidence:
            if self.role_calls >= self.c.get("max_role_calls", 10):
                raise ModelError("per-attempt role-call budget exhausted")
            self.role_calls += 1
            self.evidence.record(role, role_calls=self.role_calls, active_model=model)
            self.evidence.write(f"prompt-{self.role_calls:02d}-{role}.txt", prompt)
        c = dict(self.c, study=False) if getattr(self, "mit", None) == "off" else self.c
        self.doing = f"{role} with {model}"
        editing = role not in READ_ONLY_ROLES and self.wt is not None and Path(cwd) == Path(self.wt)
        before = self.snapshot()[0] if editing else None
        tried = set()
        while True:
            try:
                out = flint(prompt, cwd, c, role, self.name, self.budget, steps, model)
                break
            except ProviderDown as e:
                log(rest(self.ledger, e), self.name)
                tried.add(model)
                if editing and self.snapshot()[0] != before:
                    raise
                nxt = self.pick(set(avoid) | tried)
                if nxt is None:
                    raise
                log(f"handing {role} to {nxt}", self.name)
                journal("handoff", role=role, worker=self.name, model=model, to=nxt,
                        busy=isinstance(e, ProviderBusy))
                if self.evidence:
                    self.evidence.record(role, role_calls=self.role_calls, active_model=nxt, handoff_from=model)
                model = self.last_model = nxt
        self.ledger.warm(model)
        if self.evidence:
            self.evidence.write(f"response-{self.role_calls:02d}-{role}.txt", out)
        return out

    def ensure_worktree(self, task):
        # Every attempt gets its own branch from trunk: never reset a user's
        # checkout or discard a previous attempt's changes.
        ensure_trunk(self.c)
        name = f"{task['id']}-{uuid.uuid4().hex[:8]}"
        self.branch = f"swarm/{name}"
        self.wt = wt_root() / name
        self.wt.parent.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "-b", self.branch, str(self.wt), trunk_name(self.c)],
            cwd=self.c["repo"], check=True)

    def own_branch(self):
        rc, ref = git(["symbolic-ref", "-q", "HEAD"], cwd=self.wt)
        return rc == 0 and ref == f"refs/heads/{self.branch}"

    # -- one task ----------------------------------------------------------

    def do_task(self, task, goal):
        self.task, self.stage, self.wt = task, "error", None
        self.doing, self.started = "starting", time.time()
        self.evidence, self.role_calls, self.gate_count = None, 0, 0
        self.mit = None
        count_study(self.name, reset=True)
        note, info = "attempt interrupted before completion", {}
        try:
            self.stage, note, info = self._attempt(task, goal, info)
        except (CapReached, ProviderDown, NoCredits):
            self.stage = "deferred"
            raise
        finally:
            if self.evidence:
                self.evidence.finish(self.stage, note)
            try:
                self._cleanup()
            except Exception as exc:
                # Preserve the outcome and artifact if shutdown/Git prevents cleanup.
                log(f"cleanup deferred for {self.branch}: {exc}", self.name)
        if info.get("implementer"):
            try:
                self._reinforce(task, self.stage, info)
            except Exception as exc:
                journal("learning_error", id=task["id"], err=str(exc), stage=self.stage)
                log(f"learning update failed; task outcome retained: {exc}", self.name)
            # One row per attempt that spent model calls: the MIT experiment's raw data.
            journal("attempt", id=task["id"], title=task["title"], stage=self.stage,
                    mit=info.get("mit"), injected=info.get("mit_injected", False),
                    study_calls=count_study(self.name, reset=True), role_calls=self.role_calls,
                    implementer=info["implementer"], reviewer=info.get("reviewer"),
                    judge=info.get("judge"), reward=info.get("reward"),
                    scores={k: v for k, v in (info.get("scores") or {}).items()
                            if k in ("impact", "creativity", "quality", "breakthrough")},
                    persona=task.get("persona"), origin=task.get("origin", "human"))
        return self.stage == "accepted", note

    def gate(self, label):
        """Run configured commands and retain supervisor-owned evidence."""
        self.gate_count += 1
        self.doing = f"running the tests ({label})"
        commands = [self.c["test_cmd"], *self.c.get("validation_commands", [])]
        evidence, passed = [], True
        for i, command in enumerate(commands):
            name = f"gate-{self.gate_count:02d}-{label}-{i}.log"
            config = dict(self.c, test_cmd=command, _gate_log=str(self.evidence.path / name))
            started = time.monotonic()
            try:
                ok, output = run_gate(self.wt, config)
            except subprocess.TimeoutExpired:
                ok, output = False, "verification command timed out"
                Path(config["_gate_log"]).write_text(f"command: {command}\n{output}\n")
            row = {"command": command, "passed": ok, "log": name,
                   "seconds": round(time.monotonic() - started, 3), "tail": output}
            evidence.append(row)
            passed = passed and ok
        self.evidence.write(f"gate-{self.gate_count:02d}.json", evidence)
        self.evidence.record("verifying", gate=label, gates=evidence)
        return passed, json.dumps(evidence, indent=2)

    def snapshot(self):
        if not self.own_branch():
            raise ModelError(f"worker switched away from {self.branch}")
        git(["add", "-A"], cwd=self.wt, check=True)
        _, tree = git(["write-tree"], cwd=self.wt, check=True)
        _, diff = git(["diff", "--cached", "--unified=3", self.review_base], cwd=self.wt, check=True)
        return tree, diff

    def review(self, tree, diff, tests, model, avoid=()):
        self.evidence.record("reviewing", reviewed_tree=tree, reviewer=model)
        prompt = REVIEW.format(contract=json.dumps(self.contract, indent=2), tree=tree,
                               tests=tests, diff=diff[:self.c.get("max_diff", 24000)])
        try:
            raw = self.call("adversary", prompt, self.wt, self.c["steps"]["adversary"], model, avoid)
            review = parse_review(raw, tree, self.contract["acceptance"])
        except ValueError as exc:
            self.evidence.write(f"review-{self.role_calls:02d}-invalid.txt", raw)
            raise ModelError(f"invalid review: {exc}") from exc
        self.evidence.write(f"review-{self.role_calls:02d}.json", review)
        after, _ = self.snapshot()
        if after != tree:
            raise ModelError("reviewer modified the candidate it was asked to inspect")
        return review

    def _attempt(self, task, goal, info):
        c, w = self.c, self.name
        if task.get("kind") == "harness":
            return "refused", "target a separate harness checkout; live harness editing is disabled", info
        log(f"claim {task['id']} — {task['title']}", w)
        self.ensure_worktree(task)
        wd = self.wt
        self.evidence = Attempt(STATE, self.branch.split("/")[-1], task, wd, self.branch)
        info["artifact"] = str(self.evidence.path)
        info["attempt_id"] = self.evidence.data["id"]
        _, self.review_base = git(["rev-parse", "HEAD"], cwd=wd, check=True)
        strategy = task.get("strategy") or self.ledger.pick("strategy", list(STRATEGIES)) or "regression_first"
        if strategy not in STRATEGIES:
            return "refused", "unknown implementation strategy", info
        self.contract = contract(task, goal, self.review_base, strategy)
        self.evidence.write("contract.json", self.contract)
        self.evidence.record("baseline", strategy=strategy, base_commit=self.review_base)
        info["strategy"] = strategy
        ok, output = self.gate("baseline")
        _, dirty = git(["status", "--porcelain"], cwd=wd, check=True)
        if not ok or dirty:
            return "baseline", "baseline tests failed or changed tracked/unignored files; no model calls spent:\n" + output, info
        self.mit = info["mit"] = mit_arm(c)
        corpus = study(f"{task['title']} {task['detail']}", c) if self.mit == "on" else ""
        info["mit_injected"] = bool(corpus)
        self.evidence.record("study", mit=self.mit, injected=bool(corpus))
        lessons, pitfalls = self.ledger.playbook()
        # Lessons shown to this implementer share the attempt's reward (see Ledger.credit).
        info["lessons"] = [l["id"] for l in lessons + pitfalls if l.get("kind") in ("lesson", "pitfall")]
        impl = self.pick()
        if impl is None:
            raise ProviderDown(None, "every model in the pool is resting after provider failures")
        info["implementer"] = impl
        spec = json.dumps(self.contract, indent=2) + "\nSTRATEGY: " + STRATEGIES[strategy]
        if c["steps"].get("architect", 0):
            spec += "\nARCHITECT NOTES (the contract still controls scope):\n" + self.call(
                "architect", ARCHITECT.format(goal=goal, title=task["title"], detail=task["detail"],
                 corpus=corpus, test_cmd=c["test_cmd"]), wd, c["steps"]["architect"], self.pick({impl}) or impl,
                avoid={impl})
        previous = "\nEARLIER ATTEMPTS:\n" + "\n---\n".join(task.get("notes", []))
        # Parent handoffs are artifacts, not mutable model memory.
        relevant = {task["id"], task.get("parent"), *task.get("depends_on", [])}
        prior = []
        for path in (STATE / "attempts").glob("*/attempt.json"):
            row = json.loads(path.read_text())
            if row["task"]["id"] in relevant and row.get("finished"):
                prior.append((row["finished"], str(path.parent), row.get("note", "")))
        previous += "\n" + json.dumps(sorted(prior)[-3:])[-6000:]
        self.evidence.record("implementing", implementer=impl)
        try:
            handoff = self.call("implementer", IMPLEMENTER.format(
                goal=goal, spec=spec, previous=previous, corpus=corpus, test_cmd=c["test_cmd"],
                playbook=format_playbook(lessons, pitfalls)), wd, c["steps"]["implementer"], impl)
            self.evidence.write("implementation.txt", handoff)
        except StepLimit:
            self.evidence.record("step_limit", note="verify the partial implementation before continuing")
        except ModelError as exc:
            info["implementer"] = self.last_model or impl
            return "model_error", str(exc), info
        # After a handoff the model that actually wrote the code is its author.
        impl = info["implementer"] = self.last_model or impl
        adv = self.pick({impl}) or impl
        info["reviewer"] = adv
        info["same_model_review"] = adv == impl
        seen = set()
        repairs = max(0, min(3, int(c.get("max_repairs", 2))))
        for cycle in range(repairs + 1):
            self.evidence.record("candidate", repairs=cycle)
            _, head = git(["rev-parse", "HEAD"], cwd=wd, check=True)
            if head != self.review_base:
                return "rejected", "agent changed commit history outside supervisor control", info
            tree, diff = self.snapshot()
            if not diff.strip():
                return "no_change", "no implementation changes; inspect the retained handoff", info
            if weakened_tests(diff):
                return "weakened_tests", "existing assertions removed; manual review required", info
            ok, tests = self.gate(f"candidate-{cycle}")
            after, _ = self.snapshot()
            if after != tree:
                return "rejected", "test command modified the candidate; fix test isolation/ignored files", info
            review = None
            if ok:
                try:
                    review = self.review(tree, diff, tests, adv, avoid={impl})
                except (ModelError, StepLimit) as exc:
                    info["reviewer"] = self.last_model or adv   # the model that failed to deliver
                    return "review_error", str(exc), info
                adv = info["reviewer"] = self.last_model or adv
                info["same_model_review"] = adv == impl
                if review["verdict"] == "approve":
                    info["review"] = review
                    break
            failure = json.dumps(review, indent=2) if review else tests
            stage = "rejected" if review else "tests_failed"
            signature = failure_signature(stage, failure)
            self.evidence.record("repair_needed", failure=signature, reason=failure[-6000:])
            info["pitfall"] = failure[-600:]
            if cycle == repairs or signature in seen:
                return stage, f"repair budget/repeated failure; evidence: {self.evidence.path}\n{failure[-2500:]}", info
            seen.add(signature)
            self.evidence.record("repairing")
            try:
                handoff = self.call("repair", REPAIR.format(contract=json.dumps(self.contract, indent=2),
                                    failure=failure, test_cmd=c["test_cmd"]), wd,
                                    c["steps"].get("repair", c["steps"]["implementer"]), impl, avoid={adv})
                self.evidence.write(f"repair-{cycle + 1}.txt", handoff)
            except StepLimit:
                pass
            except ModelError as exc:
                return "model_error", str(exc), info
        git(["commit", "-q", "--no-verify", "-m",
             f"swarm: {task['title']}\n\nSwarm-Task: {task['id']}\n"
             f"Swarm-Implementer: {impl}\nSwarm-Reviewer: {adv}\nSwarm-Strategy: {strategy}"], cwd=wd, check=True)
        self._review_model, self._author = adv, impl
        landed, why = self.integrate()
        if not landed:
            return "conflict", why, info
        _, info["commit"] = git(["rev-parse", "HEAD"], cwd=wd, check=True)
        info["repairs"] = cycle
        info["role_calls"] = self.role_calls
        # Record integration before optional learning/reporting. Recovery consults Git too.
        self.evidence.record("integrated", commit=info["commit"])
        log(f"{task['id']}: landed on {trunk_name(c)}; evidence: {self.evidence.path}", w)
        # A third model scores the landed change; its lesson and follow-ups feed the playbook.
        info["judge"] = self.pick({impl, adv}) or adv
        _, landed_diff = git(["diff", "--unified=3", self.review_base, "HEAD"], cwd=wd)
        info["scores"] = self.judge(task, goal, landed_diff,
                                    "APPROVE: " + str((info.get("review") or {}).get("summary", "")),
                                    info["judge"], avoid={impl, adv})
        info["judge"] = self.last_model or info["judge"]
        if info["scores"]:
            self.evidence.write("judge.json", info["scores"])
        return "accepted", f"integrated {info['commit']} on {trunk_name(c)}; handoff: {self.evidence.path / 'HANDOFF.md'}", info

    def integrate(self):
        """Review/test a rebased candidate before compare-and-swap of swarm trunk."""
        c, t = self.c, trunk_name(self.c)
        for _ in range(2):
            _, old = git(["rev-parse", t], cwd=c["repo"], check=True)
            rc, _ = git(["merge-base", "--is-ancestor", old, "HEAD"], cwd=self.wt)
            if rc != 0:
                rc, _ = git(["rebase", old], cwd=self.wt)
                if rc != 0:
                    git(["rebase", "--abort"], cwd=self.wt)
                    return False, f"conflicts with newer trunk; work retained on {self.branch}"
                self.review_base = old
                tree, diff = self.snapshot()
                ok, output = self.gate("rebased")
                after, _ = self.snapshot()
                if not ok or after != tree or weakened_tests(diff):
                    return False, "rebased candidate failed verification or changed during tests"
                review = self.review(tree, diff, output, self._review_model, avoid={self._author})
                if review["verdict"] != "approve":
                    return False, "rebased candidate needs changes; fresh review saved in artifacts"
            with trunk_locked():
                if checked_out_at(c, t):
                    return False, f"{t} is checked out; not moving it"
                _, current = git(["rev-parse", t], cwd=c["repo"], check=True)
                if current != old:
                    continue
                _, head = git(["rev-parse", "HEAD"], cwd=self.wt, check=True)
                _, committed_tree = git(["rev-parse", "HEAD^{tree}"], cwd=self.wt, check=True)
                if committed_tree != self.evidence.data.get("reviewed_tree"):
                    return False, "candidate commit does not match the reviewed tree"
                rc, output = git(["update-ref", f"refs/heads/{t}", head, old], cwd=c["repo"])
                if rc == 0:
                    return True, ""
        return False, "trunk kept moving; retained candidate for a later attempt"

    def judge(self, task, goal, diff, verdict, model, avoid=()):
        try:
            out = self.call("judge", JUDGE.format(
                goal=goal, title=task["title"], detail=task["detail"], verdict=verdict,
                diff=diff[:self.c.get("max_diff", 24000)]),
                self.wt, self.c["steps"].get("judge", 4), model, avoid)
        except Exception as e:  # the change already landed; never lose it to a judge failure
            log(f"judge unavailable ({type(e).__name__}); using a neutral score", self.name)
            return None
        scores = parse_scores(out)
        if scores is None:
            log(f"judge {model} returned no usable JSON; using a neutral score", self.name)
        return scores

    def _reinforce(self, task, stage, info):
        L, impl = self.ledger, info["implementer"]
        scores = info.get("scores")
        text = f"{task['title']} {task.get('detail', '')}"
        nov = novelty(text, L.accepted_texts()) if stage == "accepted" else 0.0
        if stage == "review_error" and info.get("reviewer"):
            # A reviewer that cannot deliver a verdict wastes the implementer's work.
            L.update("implementer", info["reviewer"], 0.0, weight=PENALTY_WEIGHT["model_error"])
            log(f"reviewer {info['reviewer']} failed to deliver a verdict; penalised", self.name)
        r = reward(stage, scores, nov, repairs=info.get("repairs", 0))
        if r is None:
            return
        # Only the author of faulty code pays the penalty weight; the planner whose
        # idea it was learns at normal weight. Success pays everyone at full weight.
        w = weight(stage)
        credit = w if stage == "accepted" else 1.0
        L.update("implementer", impl, r, weight=w)
        if task.get("persona"):
            L.update("persona", task["persona"], r, weight=credit)
        if task.get("planner_model"):
            L.update("planner", task["planner_model"], r, weight=credit)
        L.credit(info.get("lessons", []), r)
        breakthrough = False
        if stage == "accepted":
            breakthrough = is_breakthrough(r, scores, L.accepted_rewards())
            L.record_accepted({"id": task["id"], "title": task["title"], "detail": task.get("detail", ""),
                               "reward": r, "novelty": nov, "scores": scores, "implementer": impl,
                               "reviewer": info.get("reviewer"), "persona": task.get("persona"),
                               "origin": task.get("origin", "human"), "breakthrough": breakthrough})
            if scores and scores.get("lesson"):
                L.add_lesson(scores["lesson"], "lesson", task["id"], r, pinned=breakthrough)
            if breakthrough:
                # Reinforce again at full reward: the draw that produced this should come up again.
                L.update("implementer", impl, 1.0, weight=BREAKTHROUGH_WEIGHT)
                if task.get("persona"):
                    L.update("persona", task["persona"], 1.0, weight=BREAKTHROUGH_WEIGHT)
                self.celebrate(task, r, scores, impl)
            self.follow_up(task, scores, r, breakthrough)
        elif stage in FAULTY:
            self.hang(task, stage, info, impl)
        elif info.get("pitfall"):
            L.add_lesson(info["pitfall"], "pitfall", task["id"], 0.5)
        info["reward"] = r
        journal("reward", id=task["id"], title=task["title"], stage=stage, reward=r, weight=w,
                novelty=nov, implementer=impl, persona=task.get("persona"),
                scores={k: v for k, v in (scores or {}).items() if k != "follow_ups"},
                breakthrough=breakthrough)
        log(f"{task['id']}: {stage}, reward {r:.2f}{' ★ BREAKTHROUGH' if breakthrough else ''}", self.name)

    def follow_up(self, task, scores, r, breakthrough):
        """Recursion on success: strong work spawns the next step it made possible."""
        if not scores or not (breakthrough or r >= 0.75):
            return
        if len(self.q.pending()) >= self.c.get("max_queue", 20):
            return
        for f in scores.get("follow_ups", [])[:2 if breakthrough else 1]:
            t = self.q.add(f["title"], f["detail"], "feature", persona=task.get("persona"),
                           planner_model=task.get("planner_model"), parent=task["id"],
                           priority=2 if breakthrough else 0, origin="follow_up",
                           depth=task.get("depth", 0))
            if t:
                log(f"follow-up queued: {t['title']}", self.name)

    def celebrate(self, task, r, scores, impl):
        s = scores or {}
        entry = (f"\n## {dt.datetime.now():%Y-%m-%d %H:%M} — {task['title']}\n\n"
                 f"reward {r:.2f} · impact {s.get('impact', '?')} · creativity {s.get('creativity', '?')}"
                 f" · quality {s.get('quality', '?')} · implementer `{impl}`"
                 f" · persona {task.get('persona', 'human')}\n\n{s.get('why', '')}\n\n"
                 f"Lesson: {s.get('lesson', '')}\n")
        with open(STATE / "BREAKTHROUGHS.md", "a") as f:
            f.write(entry)
        log(f"★ BREAKTHROUGH: {task['title']} ({impl})", self.name)
        notify(self.c, "breakthrough", task["title"])

    def hang(self, task, stage, info, impl):
        """Faulty code goes up on the rafters, under its author's name: shown to every
        later implementer and planner, written to RAFTERS.md, and announced."""
        base = getattr(self, "review_base", None) or trunk_name(self.c)
        _, diff = git(["diff", "--unified=0", base, self.branch, "--"], cwd=self.c["repo"])
        what = defect(stage, info.get("artifact"), info.get("pitfall", ""))
        code = exhibit(diff, stage)
        self.ledger.hang(impl, task, stage, what, code, info.get("artifact", ""))
        with open(STATE / "RAFTERS.md", "a") as f:
            f.write(f"\n## {dt.datetime.now():%Y-%m-%d %H:%M} — `{impl}` {CRIME[stage]}\n\n"
                    f"Task: {task['title']}  \nDefect: {what}  \nBranch: `{self.branch}`"
                    + (f"  \nEvidence: {info['artifact']}" if info.get("artifact") else "")
                    + (f"\n\n```diff\n{code}\n```\n" if code else "\n"))
        log(f"HUNG FROM THE RAFTERS: {impl} {CRIME[stage]} — {what[:140]}", self.name)
        notify(self.c, "hung from the rafters", f"{impl}: {what[:120]}")

    def _cleanup(self):
        """Keep every attempt's work on its branch; remove the worktree if configured."""
        if not self.wt or not Path(self.wt).exists():
            return
        repo, t = self.c["repo"], trunk_name(self.c)
        if self.stage != "accepted" and self.own_branch():
            git(["add", "-A"], cwd=self.wt)
            rc, _ = git(["diff", "--cached", "--quiet"], cwd=self.wt)
            if rc == 1:
                git(["commit", "-q", "--no-verify", "-m",
                     f"swarm (not accepted: {self.stage}): {self.task['title']}"], cwd=self.wt)
        if self.c.get("keep_worktrees", True):
            return
        git(["worktree", "remove", "--force", str(self.wt)], cwd=repo)
        _, ahead = git(["rev-list", "--count", f"{t}..{self.branch}"], cwd=repo)
        if ahead == "0":  # landed on trunk, or never produced anything
            git(["branch", "-D", self.branch], cwd=repo)

    def decompose(self, task, goal):
        """Recursion on failure: split a task that failed twice into smaller ones."""
        c = self.c
        # Between attempts: not the last attempt's evidence, role-call budget or MIT arm.
        self.evidence, self.mit = None, None
        model = self.ledger.pick("planner", pool(c)) or self.pick()
        if model is None:
            return 0
        with _view_lock:
            view = refresh_view(c)
            try:
                out = self.call("decomposer", DECOMPOSER.format(
                    goal=goal, title=task["title"], detail=task["detail"], test_cmd=c["test_cmd"],
                    notes="\n---\n".join(task.get("notes", [])) or task.get("note", "")),
                    view, c["steps"].get("decomposer", 6), model)
            except (StepLimit, ModelError) as e:
                self.ledger.update("planner", model, 0.0)
                log(f"decomposer {model} failed ({type(e).__name__}); '{task['title']}' stays parked", self.name)
                return 0
        subtasks = json_array(out)
        if subtasks is None:
            self.ledger.update("planner", model, 0.0)
            subtasks = []
        added = 0
        for t in subtasks[:3]:
            if (isinstance(t, dict) and isinstance(t.get("title"), str)
                    and t.get("kind", "feature") in KINDS
                    and self.q.add(t["title"], str(t.get("detail", "")), t.get("kind", "feature"),
                                   persona=task.get("persona"), planner_model=model,
                                   parent=task["id"], priority=1, origin="split",
                                   depth=task.get("depth", 0) + 1)):
                added += 1
        log(f"split '{task['title']}' into {added} smaller task(s)", self.name)
        journal("split", id=task["id"], title=task["title"], added=added, model=model)
        return added

    # -- loop --------------------------------------------------------------

    def run(self):
        while not self.stop.is_set() and not self.tally.drain.is_set():
            task = self.q.claim()
            if not task:
                self.stop.wait(20)
                continue
            try:
                goal = read_goal(self.c)
                ok, note = self.do_task(task, goal)
                final = self.q.release(task["id"], ok, note)
                journal("task", id=task["id"], title=task["title"], ok=ok, stage=self.stage,
                        worker=self.name, note=note[:300])
                if final and final.get("status") == "split":
                    self.decompose(final, goal)
                self.tally.finished()
            except CapReached:
                log("quota/window pause — deferring task", self.name)
                self.q.release(task["id"], False, "quota/window pause", defer=60)
                self.stop.wait(600)
            except ProviderDown as e:
                self.q.release(task["id"], False, f"provider unavailable: {str(e)[:200]}", defer=30)
                if self.pick() is None:
                    # No model can take a turn. Sleep until the first one is back, not blindly.
                    _, until = next_wake(self.ledger, self.c)
                    wait = max(30, min(300, until - time.time()))
                    log(f"{all_resting(self.ledger, self.c)}; task deferred, next try in {wait:.0f}s", self.name)
                else:
                    wait = 30
                    log(f"no other model could take over from {e.model}; task deferred, next try in 30s", self.name)
                self.stop.wait(wait)
            except NoCredits as e:
                log(f"out of credits: {e}", self.name)
                self.q.release(task["id"], False, "no credits")
                self.stop.set()
            except Exception as e:
                if self.stop.is_set():
                    self.q.release(task["id"], False, "run stopped; changes retained", defer=1)
                    return
                log(f"error on {task['id']}: {e}", self.name)
                journal("error", id=task["id"], err=str(e)[:500],
                        tb=traceback.format_exc()[-1200:])
                self.q.release(task["id"], False, str(e)[:400])
                self.stop.wait(30)


# ------------------------------------------------------------------ planning

def plan(c, q, budget, n=6, ledger=None):
    ledger = ledger or Ledger(STATE / "learn.json")
    ensure_trunk(c)
    goal = read_goal(c)
    persona = ledger.pick("persona", list(PERSONAS)) or "builder"
    model = ledger.pick("planner", pool(c))
    if model is None:
        log(f"planner: {all_resting(ledger, c)}; will retry")
        return 0
    base, t = c.get("base_branch", "main"), trunk_name(c)
    _, landed = git(["log", "--format=- %s", "-n", "25", f"{base}..{t}"], cwd=c["repo"])
    landed = landed.replace("- swarm: ", "- ") or "- (nothing yet)"
    brk = "\n".join(f"- {b['title']}: {(b.get('scores') or {}).get('why', '')}"
                    for b in ledger.breakthroughs()) or "- (none yet)"
    seen = "\n".join(f"- {x}" for x in q.recent_titles(80)) or "- (nothing yet)"
    log(f"planning as {persona} with {model}")
    with _view_lock:
        view = refresh_view(c)
        try:
            out = flint(PLANNER.format(
                goal=goal, persona_name=persona, persona=persona_text(persona, c), landed=landed,
                breakthroughs=brk, playbook=format_playbook(*ledger.playbook()),
                corpus=study(f"{goal[:600]} {persona}", c, k=3), seen=seen, n=n,
                test_cmd=c["test_cmd"], steps=max(2, c["steps"]["planner"] - 3)),
                view, c, "planner", "swarm", budget, c["steps"]["planner"], model)
        except (StepLimit, ModelError):
            ledger.update("planner", model, 0.0)  # ran out of steps or answered badly
            raise
    tasks = json_array(out)
    if tasks is None:
        ledger.update("planner", model, 0.0)
        raise ModelError(f"planner {model} returned no JSON task list")
    added = 0
    for task in tasks[:n]:
        if (isinstance(task, dict) and isinstance(task.get("title"), str)
                and isinstance(task.get("detail", ""), str)
                and task.get("kind", "feature") in KINDS
                and q.add(task["title"], task.get("detail", ""), task.get("kind", "feature"),
                          persona=persona, planner_model=model, origin="plan")):
            added += 1
    log(f"planner queued {added} task(s)")
    journal("plan", added=added, persona=persona, model=model)
    return added


# ------------------------------------------------------------------ account

def account():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        sys.path.insert(0, str(ROOT))
        import flint as F
        return F.fetch_key_info(key)
    except Exception:
        return None


def sync_usage(info):
    """Count requests made elsewhere with this key, so the local cap stays honest."""
    used = ((info or {}).get("free_model_daily_requests") or {}).get("used")
    if isinstance(used, int):
        import flint as F
        F.Throttle()._txn(lambda d: d.update(count=max(d.get("count", 0), used)))


def free_tool_models():
    key = os.environ.get("OPENROUTER_API_KEY", "")
    sys.path.insert(0, str(ROOT))
    import flint as F
    models = F.openrouter_get("/models", key, timeout=20)["data"]
    return {m["id"]: m for m in models if m["id"].endswith(":free")
            and "tools" in (m.get("supported_parameters") or [])}


# ------------------------------------------------------------------ setup

def detect_test_cmd(repo):
    """How to test this repository, or None. A folder that only holds projects (no manifest of
    its own) is searched one level down: a single project there is used, several are ambiguous
    and left to the owner, since testing the wrong one would reject every task."""
    repo = Path(repo)
    cmd = project_test_cmd(repo)
    if cmd:
        return cmd
    found = sub_projects(repo)
    if len(found) == 1:
        name, cmd = found[0]
        log(f"no project at the top level; testing the only one inside it: {name}")
        return f"cd {shlex.quote(name)} && {cmd}"
    return None


def embedded_repos(repo):
    """Directories one level inside repo that are Git repositories of their own.

    Git never stores an embedded repository's files in its parent, so those directories come
    out EMPTY in every worktree the swarm builds. The swarm cannot read or change that code
    from here: it has to be pointed at the inner repository instead."""
    return [d.name for d in _subdirs(repo) if (d / ".git").exists()]


def _subdirs(repo):
    try:
        return sorted(d for d in Path(repo).iterdir() if d.is_dir() and not d.name.startswith("."))
    except OSError:
        return []


def sub_projects(repo):
    """[(directory name, its test command)] for projects one level inside repo, skipping
    embedded repositories, whose code a worktree of this repository would not contain."""
    gone = set(embedded_repos(repo))
    return [(d.name, cmd) for d in _subdirs(repo) if d.name not in gone
            for cmd in [project_test_cmd(d)] if cmd]


def test_cmd_hint(repo):
    """What to tell someone whose repository the swarm cannot test by itself."""
    repo, lines = Path(repo), []
    for name in embedded_repos(repo):
        lines += [f"  {name}/ is its own Git repository, so its code is not part of {repo.name} "
                  f"and never appears in the swarm's worktrees.",
                  f"  Point the swarm at it directly:",
                  f"    swarm grind {shlex.quote(str(repo / name))} --goal '...'"]
    found = sub_projects(repo)
    if found:
        lines.append("  Projects inside it, and how each would be tested:")
        lines += [f"    --test-cmd 'cd {shlex.quote(n)} && {c}'" for n, c in found]
        lines.append("  Point the swarm at one of those directories instead, or pass one of the above.")
    if not lines:
        lines = ["  Nothing recognisable to test (no go.mod, package.json, pyproject.toml, "
                 "Cargo.toml or Makefile test target).",
                 "  Pass the command you run yourself, e.g. --test-cmd 'go test ./...'"]
    return "\n".join(lines)


def test_cmds(repo):
    """Every way this directory could be tested, best guess first: [(what found it, command)].

    A repository with code in two languages matches more than once — a Go service with a
    Python notebook beside it — so the runner-up is worth showing when the first choice
    turns out to be the wrong one."""
    repo, out = Path(repo), []
    if (repo / "go.mod").exists():
        out.append(("go.mod", "go test ./..."))
    if (repo / "Cargo.toml").exists():
        out.append(("Cargo.toml", "cargo test -q"))
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            test = json.loads(pkg.read_text()).get("scripts", {}).get("test", "")
        except (OSError, json.JSONDecodeError):
            test = ""
        if test and "no test specified" not in test:
            if (repo / "pnpm-lock.yaml").exists():
                out.append(("package.json", "pnpm install --frozen-lockfile --silent && pnpm test"))
            elif (repo / "yarn.lock").exists():
                out.append(("package.json", "yarn install --frozen-lockfile --silent && yarn test"))
            elif (repo / "package-lock.json").exists():
                out.append(("package.json", "npm ci --silent --no-audit --no-fund && npm test --silent"))
            else:
                out.append(("package.json", "npm install --silent --no-audit --no-fund && npm test --silent"))
    found = next((f for f in ("pyproject.toml", "pytest.ini", "setup.cfg", "tests")
                  if (repo / f).exists()), None)
    if found:
        r = subprocess.run(["python3", "-c", "import pytest"], capture_output=True)
        out.append((found, "python3 -m pytest -q" if r.returncode == 0
                    else "python3 -m unittest discover -q"))
    mk = repo / "Makefile"
    if mk.exists() and re.search(r"^test:", mk.read_text(errors="replace"), re.M):
        out.append(("Makefile", "make test"))
    return out


def project_test_cmd(repo):
    cmds = test_cmds(repo)
    return cmds[0][1] if cmds else None


def other_test_cmds(repo, current):
    """Ways to test this repository other than the one that just failed, as printable lines."""
    repo = Path(repo)
    rows = [(what, cmd) for what, cmd in test_cmds(repo) if cmd != current]
    rows += [(f"{name}/", f"cd {shlex.quote(name)} && {cmd}") for name, cmd in sub_projects(repo)
             if f"cd {shlex.quote(name)} && {cmd}" != current]
    if not rows:
        return ""
    lines = ["\n  Other ways this repository could be tested:"]
    lines += [f"    --test-cmd {shlex.quote(cmd)}   (found {what})" for what, cmd in rows]
    return "\n".join(lines)


def scaffold(path, goal):
    """A new Git repository for a problem that has no codebase yet."""
    d = Path(path).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    if (d / ".git").exists():
        return d
    (d / "tests").mkdir(exist_ok=True)
    (d / "README.md").write_text(f"# {d.name}\n\n{goal}\n")
    (d / "GOAL.md").write_text(f"# Goal\n\n{goal}\n")
    (d / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\nnode_modules/\n.pytest_cache/\ndist/\nbuild/\n")
    (d / "tests" / "__init__.py").write_text("")
    (d / "tests" / "test_smoke.py").write_text(
        "import unittest\n\n\nclass Smoke(unittest.TestCase):\n"
        "    def test_suite_runs(self):\n        self.assertTrue(True)\n\n\n"
        "if __name__ == \"__main__\":\n    unittest.main()\n")
    env = {**os.environ, **GIT_IDENT}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "swarm: scaffold"]):
        subprocess.run(["git", *args], cwd=d, check=True, capture_output=True, env=env)
    return d


def configure(repo, test_cmd=None):
    repo = Path(repo).expanduser().resolve()
    if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                      capture_output=True).returncode != 0:
        sys.exit(f"{repo} needs a Git repository with at least one commit.\n"
                 f"  git -C '{repo}' init && git -C '{repo}' add -A && git -C '{repo}' commit -m baseline")
    c = load_cfg()
    if c.get("repo") != str(repo):
        branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        c.update(repo=str(repo), base_branch=branch if branch and branch != "HEAD" else "main")
        c["test_cmd"] = test_cmd or detect_test_cmd(repo) or "__TEST_CMD__"
    elif test_cmd:
        c["test_cmd"] = test_cmd
    c["python"] = python_for(c)
    save_cfg(c)
    if c["test_cmd"].startswith("__"):
        sys.exit(f"could not detect how to test {repo}.\n{test_cmd_hint(repo)}")
    log(f"target {repo} on {c['base_branch']}; tests: {c['test_cmd']}")


def why_unrunnable(cmd):
    """Why a test command could not run at all, as a line to append to an error, or "".

    Judged from the command itself: its output only says "not found", which is equally true of
    a missing interpreter and of prose passed by mistake. Only a single program can be checked,
    so a compound shell command is left alone."""
    if re.search(r"[|&;<>()`]|\$\(", cmd):
        return ""
    try:
        first = shlex.split(cmd)[0]
    except (ValueError, IndexError):
        return ""
    if shutil.which(first):
        return ""
    if not re.fullmatch(r"[\w.+-]+(/[\w.+-]+)*", first):
        return ("\n  --test-cmd takes a shell command that runs the tests, not a description of "
                "the work; the goal goes in --goal.")
    instead = {"python": "python3", "pip": "pip3"}.get(first)
    return (f"\n  `{first}` is not installed or not on PATH"
            + (f" — macOS ships `{instead}`, not `{first}`." if instead else "."))


def preflight(c):
    """Fail fast, before spending requests, on anything that would waste the night."""
    paid = [m for m in (c.get("models") or [c.get("model")]) if m and not m.endswith(":free")]
    if paid and not c.get("allow_paid"):
        sys.exit(f"refusing non-free models {paid}: they spend credits. "
                 "Remove them or set allow_paid: true in swarm/config.json.")
    info = account()
    if info:
        q = info.get("free_model_daily_requests") or {}
        if isinstance(q.get("limit"), int):
            c["daily_cap"] = min(c.get("daily_cap", q["limit"]), q["limit"])
            if c.get("reserve", 0) >= c["daily_cap"]:
                c["reserve"] = c["daily_cap"] // 10
        sync_usage(info)
        log(f"OpenRouter: {q.get('used', '?')}/{q.get('limit', '?')} free requests used today; "
            f"swarm cap {c['daily_cap']} with {c.get('reserve', 0)} reserved for you")
    else:
        log("could not reach OpenRouter for account limits; using config daily_cap")
    try:
        live = free_tool_models()
        missing = [m for m in pool(c) if m not in live and m.endswith(":free")]
        if missing:
            log(f"dropping models OpenRouter no longer serves free with tools: {missing}")
            c["models"] = [m for m in pool(c) if m not in missing]
    except Exception as e:
        log(f"could not list models ({type(e).__name__}); trusting the configured pool")
    if not pool(c):
        sys.exit("no usable models in the pool; run `swarm models --write`")
    log(f"model pool ({len(pool(c))}): {', '.join(pool(c))}")
    if c.get("sandbox"):
        log("sandbox: " + ("on (writes limited to each worktree, caches and temp)"
                           if sandbox.available() else "UNAVAILABLE — agents run unsandboxed"))
    corpus = Path(c.get("corpus_db", "")).expanduser()
    if corpus.is_file():
        from corpus_index import stats
        log(f"study corpus: {stats(corpus)['chunks']} chunks")
    gone = embedded_repos(c["repo"])
    if gone:
        blind = [n for n in gone if re.search(rf"(^|[^\w-]){re.escape(n)}([^\w-]|$)", c["test_cmd"])]
        for name in gone:
            log(f"NOTE: {name}/ is a Git repository of its own, so its code is absent from every "
                f"worktree the swarm builds and cannot be read or changed from here. To work on "
                f"it: swarm grind {shlex.quote(str(Path(c['repo']) / name))}")
        if blind:
            sys.exit(f"the test command works inside {', '.join(blind)}, which is a separate Git "
                     f"repository: the swarm's worktrees do not contain that code, so every task "
                     f"would fail.\n  Point the swarm at it instead:\n"
                     f"    swarm grind {shlex.quote(str(Path(c['repo']) / blind[0]))} --goal '...'")
    ensure_trunk(c)
    sync_trunk(c)
    with _view_lock:
        view = refresh_view(c)
        ok, output = run_gate(view, c)
    if not ok:
        sys.exit(f"`{c['test_cmd']}` fails on {trunk_name(c)} before any work, so every task "
                 f"would be rejected. Fix the tests or pass --test-cmd."
                 f"{why_unrunnable(c['test_cmd'])}{other_test_cmds(c['repo'], c['test_cmd'])}"
                 f"\n{output[-1500:]}")
    log(f"baseline green on {trunk_name(c)}")


def keep_awake():
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("caffeinate: this Mac will not idle-sleep while the swarm runs "
            "(closing the lid on battery still sleeps it)")


# ------------------------------------------------------------------ daemon

def start(c, hours=None, max_tasks=None, awake=False):
    with open(STATE / "daemon.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another swarm daemon is already running on this repository")
        _stop.clear()
        preflight(c)
        if awake:
            keep_awake()
        timer = None
        if hours:
            timer = threading.Timer(hours * 3600, shutdown)
            timer.daemon = True
            timer.start()
        signal.signal(signal.SIGTERM, lambda *_: shutdown())
        try:
            run_daemon(c, max_tasks)
        finally:
            if timer:
                timer.cancel()
            shutdown()
            log("stopped. `swarm report` summarises the run.")


def run_daemon(c, max_tasks=None):
    repo = Path(c["repo"])
    budget = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
                    owner_window=c.get("owner_window", ["00:00", "00:00"]))
    q, stop, tally = Queue(c.get("max_depth", 1)), _stop, Tally(max_tasks)
    ledger = Ledger(STATE / "learn.json")
    q.recover()
    log(f"swarm up — repo={repo.name} trunk={trunk_name(c)} workers={c['workers']}")
    log(json.dumps(budget.snapshot()))
    # Rests persist in learn.json across runs; say so, or a fresh start looks stuck.
    live = set(pool(c))
    for model, until, why in ledger.resting():
        if model in live:
            log(f"{model} is resting until {dt.datetime.fromtimestamp(until):%H:%M:%S} ({why[:120]})")

    workers = [Worker(i, c, q, budget, stop, ledger, tally) for i in range(c["workers"])]
    for w in workers:
        w.start()
    last_plan, last_sync, last_beat, dry_runs, plan_fails = 0.0, time.time(), time.time(), 0, 0
    said = {}
    try:
        while not stop.is_set():
            if tally.drain.is_set():
                for w in workers:
                    w.join()
                log(f"finished {tally.n} task(s); stopping")
                break
            if time.time() - last_sync > 900:
                last_sync = time.time()
                sync_usage(account())
            report_progress(workers, said)
            if time.time() - last_beat > 3600:
                last_beat = time.time()
                log(f"heartbeat {json.dumps(budget.snapshot())}")
            # Planning costs requests like anything else, so it is rate-limited
            # and backs off when it stops producing new work.
            cooldown = c.get("plan_cooldown", 600) * (2 ** min(dry_runs, 4))
            if len(q.ready()) < c["workers"] and time.time() - last_plan > cooldown and budget.check()[0]:
                last_plan = time.time()
                try:
                    sync_trunk(c)
                    added = plan(c, q, budget, c.get("plan_batch", 5), ledger)
                    plan_fails = 0
                    dry_runs = 0 if added else dry_runs + 1
                    if not added:
                        log(f"planner added nothing — next attempt in "
                            f"{c.get('plan_cooldown', 600) * 2 ** min(dry_runs, 4) / 60:.0f}m")
                except (StepLimit, ModelError) as e:
                    # A botched plan says nothing about whether work remains: redraw a
                    # (now less likely) model soon, and back off only on a losing streak.
                    plan_fails += 1
                    if plan_fails < 4:
                        last_plan = time.time() - cooldown + 60
                        log(f"planner failed ({type(e).__name__}: {str(e).splitlines()[0][:120]}); "
                            "redrawing in 1m")
                    else:
                        plan_fails, dry_runs = 0, dry_runs + 1
                        log("planner failed 4 times in a row; backing off")
                except CapReached:
                    log("cap reached while planning — sleeping")
                    stop.wait(60)
                except ProviderDown as e:
                    log(f"planner: {rest(ledger, e)}; redrawing another model")
                    last_plan = 0.0
                except NoCredits as e:
                    log(f"out of credits: {e}")
                    shutdown()
                except Exception as e:
                    log(f"planner error: {e}")
                    dry_runs += 1
            stop.wait(c.get("tick", 90) if not max_tasks else 5)
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        shutdown()
        for w in workers:
            w.join(timeout=10)


# ------------------------------------------------------------------ report

def build_report(c, hours=24):
    L = Ledger(STATE / "learn.json").snapshot()
    since = time.time() - hours * 3600
    repo, base, t = c["repo"], c.get("base_branch", "main"), trunk_name(c)
    _, ahead = git(["rev-list", "--count", f"{base}..{t}"], cwd=repo)
    _, commits = git(["log", "--format=- %h %s", "-n", "30", f"{base}..{t}"], cwd=repo)
    accepted = [a for a in L["accepted"] if a["t"] >= since]
    tasks = [j for j in _read(STATE / "journal.jsonl") if j["event"] == "task" and j["t"] >= since]
    stages = {}
    for j in tasks:
        stages[j.get("stage", "?")] = stages.get(j.get("stage", "?"), 0) + 1
    parked = [d for d in _read(STATE / "done.jsonl")
              if d.get("status") in ("parked", "split") and d.get("finished", 0) >= since]
    b = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
               owner_window=c.get("owner_window", ["00:00", "00:00"])).snapshot()

    out = [f"# Swarm report — {Path(repo).name}",
           f"_{dt.datetime.now():%Y-%m-%d %H:%M}, last {hours:g}h_", "",
           f"**{t}** is {ahead or 0} commit(s) ahead of **{base}**. Nothing has been merged into your branches.",
           "", "```sh", f"git -C '{repo}' log --oneline {base}..{t}",
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
    import experiment
    out += ["", "## MIT corpus experiment (every attempt so far)", "",
            experiment.markdown(experiment.summary(STATE / "journal.jsonl"))]
    if parked:
        out += ["", "## Split or parked (needs a human look)", ""]
        out += [f"- {d['status']}: {d['title']} — {d.get('note', '')[:160].strip()}" for d in parked]
    out += ["", "## Agent leaderboard", "", "| role | arm | pulls | mean reward | posterior |", "|---|---|---:|---:|---:|"]
    for row in Ledger(STATE / "learn.json").leaderboard():
        out.append(f"| {row['role']} | `{row['arm']}` | {row['pulls']} | {row['mean']:.2f} | {row['posterior']:.2f} |")
    lessons, pitfalls = Ledger(STATE / "learn.json").playbook()
    out += ["", "## Playbook", "", format_playbook(lessons, pitfalls) or "(empty so far)", "",
            "## Budget", "", f"{b['spent_today']}/{b['usable']} usable free requests spent today; "
            f"resets {b['resets_local']}.", "", "## Trunk commits", "", commits or "- none yet"]
    text = "\n".join(out) + "\n"
    (STATE / "REPORT.md").write_text(text)
    return text


# ------------------------------------------------------------------ commands

def _setup(a=None):
    c = cfg()
    use_repo(c)
    if a is not None and getattr(a, "workers", None):
        c["workers"] = a.workers
    return c


def cmd_grind(a):
    if a.new:
        if not a.goal:
            sys.exit("--new needs --goal describing the problem")
        repo = scaffold(a.new, a.goal)
        configure(repo, a.test_cmd or "python3 -m unittest discover -s tests -t . -q")
    elif a.repo:
        configure(a.repo, a.test_cmd)
    elif a.test_cmd:
        configure(load_cfg().get("repo", "."), a.test_cmd)
    c = _setup(a)
    if a.goal and re.fullmatch(r"(?:read|use|see|follow|from)?\s*\.?/?GOAL\.md\.?", a.goal.strip(), re.I):
        # They mean the file. Any goal kept from an earlier run would shadow it.
        (STATE / "GOAL.md").unlink(missing_ok=True)
        log(f"--goal names the goal file; reading {Path(c['repo']) / c.get('goal_file', 'GOAL.md')}")
    elif a.goal:
        (STATE / "GOAL.md").write_text(a.goal.strip() + "\n")
    log(f"goal: {read_goal(c)[:200]}")
    start(c, hours=a.hours, max_tasks=a.max_tasks, awake=True)


def cmd_run(a):
    if a.hours <= 0:
        sys.exit("--hours must be positive")
    start(_setup(a), hours=a.hours, max_tasks=a.max_tasks)


def cmd_status(a):
    c = _setup()
    b = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
               owner_window=c.get("owner_window", ["00:00", "00:00"]))
    snap = b.snapshot()
    ok, wait, why = b.check()
    q = Queue()
    pend = q.pending()
    done = _read(q.done)
    _, ahead = git(["rev-list", "--count", f"{c.get('base_branch', 'main')}..{trunk_name(c)}"], cwd=c["repo"])
    print(json.dumps({
        "repo": c["repo"], "trunk": trunk_name(c), "trunk_ahead": ahead,
        "budget": snap,
        "pacing": {"allowed_now": ok, "wait_s": round(wait), "reason": why},
        "queue": {"pending": len(pend),
                  "claimed": sum(1 for t in pend if t.get("claimed"))},
        "landed": sum(1 for t in done if t.get("status") == "done"),
        "split": sum(1 for t in done if t.get("status") == "split"),
        "parked": sum(1 for t in done if t.get("status") == "parked"),
        "top_arms": Ledger(STATE / "learn.json").leaderboard()[:8],
        "resting": [{"model": m, "until": dt.datetime.fromtimestamp(u).isoformat(timespec="seconds"),
                     "reason": why} for m, u, why in Ledger(STATE / "learn.json").resting()],
    }, indent=2))
    for t in _read(STATE / "journal.jsonl")[-8:]:
        print(f"  {t['iso']}  {t['event']:<8} {t.get('stage') or t.get('role') or ''} "
              f"{t.get('title') or t.get('model') or ''}")


def cmd_report(a):
    print(build_report(_setup(), a.hours))


def cmd_add(a):
    c = _setup()
    try:
        t = Queue(c.get("max_depth", 1), c.get("max_queue", 20)).add(
            a.title, a.detail or "", a.kind, priority=a.priority, origin="human",
            acceptance=a.acceptance or None)
    except ValueError as e:
        sys.exit(f"not queued: {e}")
    if not t:
        sys.exit("not queued: that title was already tried, or the queue is full")
    print(f"queued {t['id']}: {t['title']}")
    for row in criteria(t):
        print(f"  {row['id']} {row['text'][:150]}")


def cmd_plan(a):
    c = _setup()
    b = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
               owner_window=c.get("owner_window", ["00:00", "00:00"]))
    plan(c, Queue(c.get("max_depth", 1)), b, a.n)


def cmd_wake(a):
    """End every model's rest now (e.g. after a provider outage is over)."""
    _setup()
    L = Ledger(STATE / "learn.json")
    rows = L.resting()
    for model, _, _ in rows:
        L.warm(model)
    print(f"woke {len(rows)} model(s): {', '.join(m for m, _, _ in rows)}" if rows else "no model is resting")


def cmd_models(a):
    live = free_tool_models()
    c = load_cfg()
    current = set(c.get("models") or [])
    for mid, m in sorted(live.items(), key=lambda kv: -(kv[1].get("context_length") or 0)):
        print(f"{'*' if mid in current else ' '} {mid:58} ctx={m.get('context_length')}")
    gone = sorted(current - set(live))
    if gone:
        print(f"\nno longer free with tools: {', '.join(gone)}")
    if a.write:
        c["models"] = [m for m in live if (live[m].get("context_length") or 0) >= 128_000]
        save_cfg(c)
        print(f"\nwrote {len(c['models'])} models to {CONFIG}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grind", help="run nonstop until Ctrl-C (keeps the Mac awake)")
    g.add_argument("repo", nargs="?", help="target Git repository (default: configured one)")
    g.add_argument("--goal", help="what the project should become (stored with the swarm's state)")
    g.add_argument("--new", metavar="DIR", help="start a new project in DIR for a problem with no codebase")
    g.add_argument("--test-cmd", help="command that must pass for work to land")
    g.add_argument("--hours", type=float, help="optional limit; default is no limit")
    g.add_argument("--workers", type=int)
    g.add_argument("--max-tasks", type=int, help="stop after finishing this many tasks")
    g.set_defaults(fn=cmd_grind)
    run = sub.add_parser("run", help="bounded run")
    run.add_argument("--hours", type=float, default=24, help="stop after this many hours (default 24)")
    run.add_argument("--workers", type=int)
    run.add_argument("--max-tasks", type=int)
    run.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    r = sub.add_parser("report", help="what landed, breakthroughs, leaderboard, playbook")
    r.add_argument("--hours", type=float, default=24)
    r.set_defaults(fn=cmd_report)
    p = sub.add_parser("add")
    p.add_argument("title")
    p.add_argument("--detail", default="")
    p.add_argument("--kind", default="feature")
    p.add_argument("--priority", type=int, default=1, help="hand-added tasks jump the queue (default 1)")
    p.add_argument("--acceptance", action="append", metavar="TEXT",
                   help="one thing the reviewer must verify; repeat for each (default: the detail)")
    p.set_defaults(fn=cmd_add)
    p2 = sub.add_parser("plan")
    p2.add_argument("-n", type=int, default=5)
    p2.set_defaults(fn=cmd_plan)
    sub.add_parser("wake", help="end every model's rest now").set_defaults(fn=cmd_wake)
    m = sub.add_parser("models", help="list free tool-capable models; --write sets the pool")
    m.add_argument("--write", action="store_true")
    m.set_defaults(fn=cmd_models)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
