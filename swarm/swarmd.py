#!/usr/bin/env python3
"""A self-reinforcing coding swarm on free OpenRouter models. Runs until stopped.

Each task: implement → tests → adversarial review → tests → land on the
swarm's trunk branch → judge → reinforce. Later tasks start from trunk, so the
work compounds. Failed tasks are retried with their failure notes, then split
into smaller tasks; strong results earn follow-ups. Models and planner
personas are chosen by Thompson sampling on the rewards they earn, and every
accepted change leaves a lesson for the agents that come after it.

    swarm grind ~/code/project --goal "what it should become"   # nonstop
    swarm stop [--drain]                         # now, or after the task in flight
    swarm grind                                  # resume the configured target
    swarm run --hours 8                          # bounded run
    swarm report                                 # what happened while you were away
    swarm status | add | plan | models | wake

The supervisor never merges into your checkout: accepted work accumulates on
`trunk` (default swarm/trunk). Review with `git log main..swarm/trunk` and
merge what you want. Agents and tests run in a write-restricting macOS
sandbox; it contains accidents, not a determined attacker.
"""
import argparse, datetime as dt, fcntl, hashlib, json, os, plistlib, random, re, shlex, shutil, signal
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
import improvement                      # noqa: E402

CONFIG = Path(os.environ.get("FLINT_SWARM_CONFIG") or HERE / "config.json").expanduser()
STATE = HERE / "state"                  # per target repo once use_repo() runs
LOGS = HERE / "logs"
SLUG = "default"
STATE.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

DEFAULT_MODEL = "inclusionai/ling-3.0-flash-fin:free"
KINDS = ("feature", "bugfix", "test", "refactor")
META = ("persona", "planner_model", "priority", "depth", "parent", "origin", "acceptance",
        "depends_on", "root", "strategy", "execution_class", "allow_test_changes", "serves",
        "packet_id", "allowed_paths", "verification_commands", "cycle_id", "proposal_id",
        "evidence", "benefit", "baseline_commit", "effort_minutes", "recovery_action")
# Only a person may authorise a task to change existing tests. A planner or decomposer that
# could set this on its own subtasks would have found the way to make any red suite green.
TEST_CHANGE_ORIGINS = frozenset({"human", "cursor"})
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
_edit_calls = {}      # worker -> edit_file/write_file calls its turns made this attempt
_charges = {}         # worker -> dollars its turns were charged this attempt
_held = {}            # worker -> when it last said it was waiting for the allowance
_waiting = {}         # worker -> why it is not working, or absent while it is
_ran_on = {}          # worker -> the model its last turn actually ran on, once a stand-in took it
_active_turns = {}   # worker -> live role and actual routed model
_turn_logs = {}      # worker -> the log the live turn is writing, for its round counter
_run = {}
_run_lock = threading.RLock()
_scheduler_drain = threading.Event()
_recheck_lock = threading.Lock()
_rechecked = {"at": 0.0, "said": False}   # last OpenRouter allowance check, and whether "still spent" was logged
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
           "event": event, "run_id": _run.get("id"), **kw}
    with _journal_lock:
        with open(STATE / "journal.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


EXAMPLE = HERE / "config.example.json"


def repo_slug(repo):
    """The short, stable name a repository's state, logs, worktrees and config are filed under."""
    repo = str(Path(repo).expanduser().resolve())
    return re.sub(r"[^A-Za-z0-9._-]+", "-", Path(repo).name) + "-" + \
        hashlib.sha1(repo.encode()).hexdigest()[:6]


def per_repo_config(repo):
    """Where a repository's tuned config belongs, whether or not anyone has written one."""
    return HERE / "configs" / f"{repo_slug(repo)}.json"


def config_path_for(repo=None):
    """Which config file governs `repo`: the environment's choice, then the repository's own
    tuned file, then the default.

    Tuned files in swarm/configs/ are the whole point of that folder, but only
    start-studio-swarm.command used to find one, through FLINT_SWARM_CONFIG. Every other way in
    — `swarm grind <repo>`, the bridge, the Cursor panel's Start button — read config.json and
    ground a carefully tuned repository on default settings without saying so."""
    env = os.environ.get("FLINT_SWARM_CONFIG")
    if env:
        return Path(env).expanduser()
    if repo:
        tuned = per_repo_config(repo)
        if tuned.is_file():
            return tuned
    return HERE / "config.json"


def config_kind(path=None):
    """How to name the loaded config in a log line or the panel's badge."""
    path = Path(path or CONFIG)
    if path.parent == HERE / "configs":
        return "per-repo"
    return "environment" if os.environ.get("FLINT_SWARM_CONFIG") else "default"


# What an edit to the config file can change without a restart, checked between tasks.
# "These settings take effect when the daemon next starts" cost 25 daemon starts in 26 hours
# and killed 54 attempts in flight — 5.6 hours of work.
RELOADABLE = frozenset({
    "models", "role_models", "paid_models", "steps", "role_timeouts", "turn_timeout", "test_timeout",
    "max_repairs", "reviewer_exclude", "plan_cooldown", "plan_batch", "allowance_recheck",
    "monthly_usd", "daily_usd", "spend_reset_day", "daily_cap", "reserve", "owner_window",
    "max_queue", "max_depth", "inject_corpus", "allow_paid", "study", "tick", "max_diff",
    "validation_commands", "test_cmd", "keep_worktrees", "improve_when_idle",
    "improvement_paths", "improvement_timeout", "review_format_retries"})
# Changing these under a running daemon would strand worktrees, state or threads.
RESTART_ONLY = frozenset({"repo", "trunk", "workers", "sandbox_write", "base_branch", "python",
                          "goal_file", "roadmap_file"})
_cfg_lock = threading.Lock()
# 0 until a daemon takes its baseline in run_daemon. Reloading is that daemon's business: any
# other process holds a config it assembled itself, and pouring the file over it would replace
# settings its caller chose — a bridge command's repo-specific test_cmd, say.
_cfg_seen = {"mtime": 0.0}


def config_mtime():
    try:
        return CONFIG.stat().st_mtime
    except OSError:
        return 0.0


def watch_config(c, budget=None):
    """Apply an edited config to a running daemon. Returns the keys that changed.

    Called between tasks, so nothing in flight is interrupted — which is the whole point: a
    restart to pick up a setting used to kill the attempt that was running."""
    with _cfg_lock:
        mtime = config_mtime()
        if not _cfg_seen["mtime"] or not mtime or mtime == _cfg_seen["mtime"]:
            return []
        _cfg_seen["mtime"] = mtime
        try:
            fresh = json.loads(CONFIG.read_text())
        except (OSError, ValueError) as e:
            log(f"config changed but could not be read; keeping the running settings: {e}")
            return []
        changed, blocked = [], []
        for key, value in fresh.items():
            if key in RESTART_ONLY:
                if c.get(key) != value:
                    blocked.append(key)
            elif (key in RELOADABLE or key.startswith("corpus_")) and c.get(key) != value:
                c[key] = value
                changed.append(key)
        for key in [k for k in c if k in RELOADABLE and k not in fresh]:
            del c[key]                 # removed from the file: back to its default
            changed.append(key)
        if changed and {"daily_cap", "reserve", "owner_window"} & set(changed) and budget:
            try:
                budget.retune(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
                              owner_window=c.get("owner_window", ["00:00", "00:00"]))
            except ValueError as e:
                log(f"config: pacing left as it was ({e})")
        if changed:
            log(f"config reloaded: {', '.join(sorted(changed))}")
            journal("config_reload", keys=sorted(changed), path=str(CONFIG))
        if blocked:
            for key in sorted(blocked):
                log(f"restart needed for {key}: it cannot change while the daemon is running")
            journal("config_restart_needed", keys=sorted(blocked), path=str(CONFIG))
        return changed


def use_config(repo):
    """Point this process at the config that governs `repo`.

    The path is a module global and never a key inside the file: the file holds values, and a
    config copied from another checkout must not drag the old path along with it."""
    global CONFIG
    CONFIG = config_path_for(repo)
    return CONFIG


def warn_config_mismatch(repo, record=False):
    """The tuned file that exists for `repo` but is not the one loaded, or None.

    Silence here is what let the 11:32 run on 2026-09-26 grind the studio for 25 paid attempts
    on the default pytest gate and the default paid fallback."""
    if not repo:
        return None
    tuned = per_repo_config(repo)
    if not tuned.is_file() or tuned.resolve() == Path(CONFIG).resolve():
        return None
    log(f"WARNING using {CONFIG} while {tuned.name} exists for this repo — its tuned settings "
        f"are not in effect (FLINT_SWARM_CONFIG={tuned})")
    if record:
        journal("config_mismatch", loaded=str(CONFIG), tuned=str(tuned), repo=str(repo))
    return tuned


def load_cfg(repo=None):
    """The live config, seeded from the committed template the first time. config.json is not
    tracked: the swarm rewrites it on every run, and a tracked file it rewrites cannot be
    updated with `git pull`.

    With `repo`, the path is resolved for that repository first, so a caller that knows which
    repository it means gets that repository's tuned settings rather than the default ones."""
    if repo:
        use_config(repo)
    if not CONFIG.exists() and EXAMPLE.is_file() and CONFIG.parent == EXAMPLE.parent:
        CONFIG.write_text(EXAMPLE.read_text())
        log(f"created {CONFIG} from {EXAMPLE.name}")
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def save_cfg(c):
    """Write back to whichever file this process loaded, per-repo file included."""
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
    SLUG = repo_slug(c["repo"])
    STATE = HERE / "state" / SLUG
    LOGS = HERE / "logs" / SLUG
    STATE.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)


def wt_root():
    return HERE / "wt" / SLUG


def pool(c, role=None):
    """The free models the swarm works with. Paid ids in `models` are dropped unless
    allow_paid is set, so a stray paid entry can never quietly spend credits.

    ``role_models`` can narrow a role to a proven subset of the general pool.  It
    deliberately narrows rather than extends ``models``: a role override cannot
    quietly introduce an unreviewed or paid provider.
    """
    ms = c.get("models") or [c.get("model") or DEFAULT_MODEL]
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


def spend_file(c=None):
    """The ledger this swarm charges paid replies to.

    A shared budget (`monthly_usd`) is one pot for every swarm on this key, so they all
    charge the same file and none of them can spend the ceiling on its own. A
    per-repository budget (`daily_usd`) keeps its own file, which is what it always meant."""
    return (HERE / "state" / "spend.json") if monthly(c) else STATE / "spend.json"


def monthly(c=None):
    """True when this config holds a shared budget over a month rather than a day."""
    return bool(c) and c.get("monthly_usd") not in (None, "")


def spend_reset_day(c=None):
    """The day of the month a shared budget restarts, or None for a daily window."""
    if not monthly(c):
        return None
    day = c.get("spend_reset_day")
    return int(day) if str(day if day is not None else "").strip().isdigit() else 1


def spend_cap(c=None):
    """The dollar ceiling for one window, or None when no dollar budget is set."""
    for key in ("monthly_usd", "daily_usd"):
        if c and c.get(key) not in (None, ""):
            try:
                return float(c[key])
            except (TypeError, ValueError):
                return None
    return None


def spend_window(c=None):
    """The name of the current budget window.

    flint owns this arithmetic and writes the ledger, so swarmd asks it rather than
    keeping a second copy that could drift and disagree about when money came back."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import flint
    return flint.spend_period(spend_reset_day(c))


def spend_resets(c=None):
    """The date the current window ends, so a spent budget can say when it returns."""
    import calendar
    day = spend_reset_day(c)
    if day is None:
        return None
    start = dt.date.fromisoformat(spend_window(c))
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return dt.date(year, month, min(day, calendar.monthrange(year, month)[1])).isoformat()


def spend_used(c=None):
    """Dollars charged to paid models in the current budget window, from flint's ledger."""
    try:
        d = json.loads(spend_file(c).read_text())
    except (OSError, json.JSONDecodeError):
        return 0.0
    if d.get("period") != spend_window(c):
        return 0.0            # a window that has rolled over has nothing spent in it yet
    try:
        return float(d.get("usd", 0.0))
    except (TypeError, ValueError):
        return 0.0


def spend_left(c):
    """What is left of this window's dollar budget, or None when no budget is set."""
    cap = spend_cap(c)
    if cap is None:
        return None
    return max(0.0, cap - spend_used(c))


def spend_phrase(c):
    """How much money is left and when it comes back, for a log line."""
    left, cap = spend_left(c), spend_cap(c)
    if left is None or cap is None:
        return ""
    if not monthly(c):
        return f"${left:.2f} of today's ${cap:.2f} left"
    return f"${left:.2f} of this month's ${cap:.2f} left, back {spend_resets(c)}"


def paid_stand_in(c, model):
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


def can_take_a_turn(c, budget):
    """Whether any turn could start right now — on a free model or a paid stand-in.

    `flint()` already falls back to a paid model when free capacity is gone, so anything
    that gates work on `budget.check()` alone is stricter than the turn it is guarding.
    That matters most for the planner: a swarm whose queue has drained and whose free
    allowance is spent has nothing to work on and no way to think of anything, so it sits
    idle until midnight with its dollar budget untouched."""
    if budget.check()[0]:
        return True
    return bool(budget.paid_would_help() and paid_stand_in(c, None))


def trunk_name(c):
    return c.get("trunk", "swarm/trunk")


# A goal file's ordered priorities, when it has any. The heading has to say so: a "Legacy
# backlog (reference, not the next dispatch order)" is exactly what this must not read as a
# priority list.
PRIORITY_HEADING = re.compile(r"^#{1,6}\s*.*(?:work on now|priorities).*$", re.I | re.M)
NUMBERED = re.compile(r"^\s{0,3}(\d+)[.)]\s+(.*)$")
# How much of a landed change's reward its place in that list is worth. The judge gave 0.94 to
# a thumbnail validator and 0.97 to a configurable word list while the four deep games GOAL.md
# puts first landed nothing, and those rewards are what trained the planner toward tooling.
GOAL_ITEM_WEIGHT = {1: 1.0, 2: 0.85, 3: 0.6, 4: 0.5, 0: 0.35}


def goal_items(goal_text):
    """The numbered priorities under the goal's own "work on now" heading, in order.

    Empty when the goal does not rank anything, and then nothing downstream changes: no task is
    dropped for serving no item and no reward is scaled. A goal that does not say what comes
    first cannot be used to say a change came second."""
    m = PRIORITY_HEADING.search(goal_text or "")
    if not m:
        return []
    items, seen = [], set()
    for line in (goal_text[m.end():]).splitlines():
        if line.startswith("#"):
            break                       # the next heading ends the list
        row = NUMBERED.match(line)
        if row:
            n = int(row.group(1))
            if n in seen:
                break                   # a second list under the same heading
            seen.add(n)
            items.append(re.sub(r"\*\*|`", "", row.group(2)).strip()[:200])
    return items


def goal_item_weight(n, items):
    """What a landed change's place in the priority list multiplies its reward by."""
    if not items:
        return 1.0
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if not 1 <= n <= len(items):
        n = 0
    return GOAL_ITEM_WEIGHT.get(n, 0.5 if n else GOAL_ITEM_WEIGHT[0])


def read_goal(c):
    for p in (STATE / "GOAL.md", Path(c["repo"]) / c.get("goal_file", "GOAL.md")):
        if p.is_file() and p.read_text().strip():
            return p.read_text().strip()[:4000]
    return "(no GOAL.md: infer the project's purpose from its README and code, then improve it)"


def clean_env():
    return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}


def kill_group(p):
    """Stop a child and everything it started. Never raises: a process that is already gone,
    or that the OS will not let us signal, must not abort a shutdown that is killing others."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:              # gone (ProcessLookupError) or refused (PermissionError)
        pass


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
            kill_group(p)
        p.wait()
        with _process_lock:
            _processes.discard(p)


def shutdown():
    _stop.set()
    with _process_lock:
        for p in _processes:
            kill_group(p)


def reportable(note):
    """A failure note with the machinery taken out: paths, log references, round counters.

    "Split or parked" used to print raw log fragments — `_file tool: read_file round 3/26…` —
    which say nothing to the person reading the report in the morning."""
    note = re.sub(r"\s+", " ", str(note or "")).strip()
    # A subprocess error's str() begins with the whole argv, sandbox profile and all.
    # ...and release() truncates the note at 800 characters, so the argv is often unterminated.
    note = re.sub(r"Command '\[.*?(?:\]'|$)", "the sandboxed command", note, flags=re.S)
    note = re.sub(r"(?:;\s*)?(?:evidence|log|artifact):\s*\S+", "", note)
    note = re.sub(r"\bround \d+(?:/\d+)?\b", "", note)
    note = re.sub(r"(?:/[\w.@%+-]+){2,}", "", note)          # any leftover absolute path
    return re.sub(r"\s{2,}", " ", note).strip(" ;,-")


def describe_error(e):
    """What to journal about an exception, without its command line.

    All 12 "sandbox-exec" errors in this window were really "timed out after 420 seconds",
    hidden past character 500 of a str() that began with the whole sandbox profile. argv is
    never recorded: it has held a 25 KB prompt, and prompts do not belong in the journal."""
    row = {"kind": type(e).__name__}
    if isinstance(e, subprocess.TimeoutExpired):
        row["timeout"] = e.timeout
        row["err"] = f"timed out after {e.timeout} seconds"
    elif isinstance(e, subprocess.CalledProcessError):
        row["returncode"] = e.returncode
        row["err"] = f"exited {e.returncode}"
    else:
        row["err"] = str(e)[:500]
    for name in ("stderr", "output"):
        out = getattr(e, name, None)
        if out:
            row["stderr"] = (out.decode("utf-8", "replace") if isinstance(out, bytes)
                             else str(out))[-1000:]
            break
    return row


# ------------------------------------------------------------------ running the current code

# The daemon imports swarmd once and keeps it, but spawns flint.py afresh for every turn. Edit
# the harness while it runs and it becomes half one version and half another: new flint against
# old supervisor, with no sign of it in any log.
def source_files():
    # Reporting/bridge additions must not restart a shift. Include the daemon's
    # transitive local dependencies, including modules loaded by child turns.
    return sorted({ROOT / "flint.py", ROOT / "nonstop.py", *(
        HERE / name for name in ("swarmd.py", "budget.py", "learn.py", "workflow.py",
                                "sandbox.py", "improvement.py", "mit_corpus.py", "wallet.py"))})


def source_fingerprint():
    out = {}
    for f in source_files():
        try:
            st = f.stat()
        except OSError:
            continue
        out[str(f)] = (round(st.st_mtime, 3), st.st_size)
    return out


def source_changed(before):
    """The files whose contents changed under a running daemon."""
    now = source_fingerprint()
    return sorted(Path(k).name for k in set(before) | set(now) if before.get(k) != now.get(k))


def source_compiles():
    """(ok, why). A half-saved edit must not be restarted into."""
    for f in source_files():
        try:
            compile(f.read_text(), str(f), "exec")
        except SyntaxError as e:
            return False, f"{f.name}:{e.lineno}: {e.msg}"
        except (OSError, ValueError) as e:
            return False, f"{f.name}: {e}"
    return True, ""


def restart_into_new_code(changed):
    """Replace this process with the same command on the new code. Never returns if it works.

    execv keeps the pid, so the daemon.pid note and anything watching it stay correct, and
    Python marks its own descriptors close-on-exec, so the lock this daemon holds is released
    as it goes — verified rather than assumed. Nothing is running at this point: the workers
    have been drained and their children killed."""
    ok, why = source_compiles()
    if not ok:
        log(f"the harness changed but will not compile, so this run keeps the code it started "
            f"with: {why}")
        journal("self_restart_refused", changed=changed, reason=why)
        return False
    log(f"restarting into the edited harness ({', '.join(changed)})")
    journal("self_restart", changed=changed)
    shutdown()                                  # kill anything still running before we go
    for path in (STATE / "now.json",):
        path.unlink(missing_ok=True)
    # FLINT_RUN_DEADLINE is already in this process's environment, fixed when the shift began,
    # and is inherited unchanged. Recomputing it here from the hours remaining is what made a
    # restart walk the end of the shift forward by however long the run had lasted.
    try:
        os.execve(sys.executable, [sys.executable, *sys.argv], dict(os.environ))
    except OSError as e:                        # nothing was lost; carry on as we are
        log(f"could not restart into the new code: {e}")
        journal("self_restart_failed", changed=changed, error=str(e)[:200])
        return False


# Two different things drain the workers: somebody asking the run to stop, and the harness
# having been edited. They must not be confused — a source change that arrives while a stop is
# pending used to turn that stop into a restart, and the run carried on after being told to end.
_drain_reason = {"stop_requested": False}


def drain(tally, stop=True):
    """Finish the tasks in flight, then stop — or, with stop=False, then restart.

    54 attempts died mid-turn in this window because stopping meant SIGINT, and 5.6 hours of
    model work went with them."""
    if stop:
        _drain_reason["stop_requested"] = True
        _run["stop_reason"] = "Drain requested"
    _scheduler_drain.set()
    if not tally.drain.is_set():
        tally.drain.set()
        log("draining: finishing the task(s) in flight, then "
            + ("stopping" if stop else "restarting"))
        journal("drain_requested", stop=stop)


def sh(cmd, cwd=None, timeout=900, env=None):
    with process(cmd, cwd=cwd, shell=isinstance(cmd, str),
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, env=env) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except BaseException:
            kill_group(p)
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


def spend_ledger_paths(c=None):
    """The spend ledger's three files: the ledger, its lock and the tmp file it is replaced
    through. A sandboxed turn on a paid model has to write these to record what it spent.

    They are named one by one rather than opening up STATE, which also holds the queue and the
    journal — an agent that could rewrite its own queue would be a different thing entirely.
    This matters most when the swarm is working on its own checkout: STATE then lives inside
    the repo under test but outside the worktree, so without this the very first paid turn dies
    with a PermissionError on spend.lock and the model is blamed for it."""
    return [spend_file(c).with_suffix(ext) for ext in (".json", ".lock", ".tmp")]


def sandbox_profile(cwd, c):
    home = os.environ.get("FLINT_HOME", "~/.flint")
    return sandbox.profile([cwd, *sandbox.git_paths(cwd), home],
                           [*spend_ledger_paths(c), *c.get("sandbox_write", [])])


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

    def resolve(self, refs):
        """Task ids for a list of ids or exact titles. Titles are unique in a queue, so they
        are a usable key, and a person writing a dependency knows the title, not the id."""
        with self.locked():
            rows = _read(self.path) + _read(self.done)
        ids = {r["id"] for r in rows}
        titles = {r["title"].strip().lower(): r["id"] for r in rows}
        out, missing = [], []
        for ref in refs:
            key = str(ref).strip()
            if key in ids:
                out.append(key)
            elif key.lower() in titles:
                out.append(titles[key.lower()])
            else:
                missing.append(ref)
        if missing:
            raise ValueError(f"no queued or finished task matches {missing}; "
                             f"depend on a task's exact title or its id")
        return out

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
            if t.get("execution_class", "standard") not in ("standard", "fast"):
                raise ValueError("execution_class must be standard or fast")
            if t.get("allow_test_changes"):
                if t.get("origin") not in TEST_CHANGE_ORIGINS:
                    raise ValueError("only a person can allow a task to change existing tests")
                t["allow_test_changes"] = True
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

    def repoint(self, old, new):
        """Move every pending dependency on `old` onto `new`, and say how many moved.

        A split parent never reaches `done`, so a task still waiting on it would wait for
        ever. Its children carry its work, so the last of them stands in for it.
        """
        if not new or old == new:
            return 0
        with self.locked():
            rows, moved = _read(self.path), 0
            for r in rows:
                deps = r.get("depends_on") or []
                if old in deps:
                    r["depends_on"] = [d for d in deps if d != old] + [new]
                    moved += 1
            if moved:
                _write(self.path, rows)
            return moved

    def strand(self, tid, why):
        """Park every queued task that depends on `tid`, directly or through another.

        A parked task, or one split into nothing, never reaches `done`, so anything
        waiting on it would wait for ever: invisible in the logs, and holding a queue
        slot the planner can never reclaim. Parking the dependents with the reason makes
        the chain visible in `swarm report`, where a person can fix and re-add it.
        Returns the titles parked."""
        with self.locked():
            rows = _read(self.path)
            dead, parked = {tid}, []
            changed = True
            while changed:
                changed = False
                for r in rows:
                    if r["id"] in dead or r.get("claimed"):
                        continue
                    if dead & set(r.get("depends_on") or []):
                        dead.add(r["id"])
                        changed = True
            now = time.time()
            keep = []
            for r in rows:
                if r["id"] in dead and r["id"] != tid and not r.get("claimed"):
                    r["status"], r["finished"] = "parked", now
                    r["note"] = f"blocked: {why}"[-800:]
                    parked.append(r)
                else:
                    keep.append(r)
            if parked:
                _write(self.path, keep)
                with open(self.done, "a") as f:
                    for r in parked:
                        f.write(json.dumps(r) + "\n")
            return [r["title"] for r in parked]

    def claim(self, eligible=None, improvements=True):
        with self.locked():
            rows, now = _read(self.path), time.time()
            complete = {r["id"] for r in _read(self.done) if r.get("status") == "done"}
            ready = [r for r in rows if not r.get("claimed") and r.get("not_before", 0) <= now
                     and not r.get("blocked_reason")
                     and set(r.get("depends_on", [])).issubset(complete)
                     and (improvements or r.get("origin") != "improvement")
                     and (eligible is None or eligible(r))]
            if any(r.get("claimed") and r.get("origin") == "improvement" for r in rows):
                ready = [r for r in ready if r.get("origin") != "improvement"]
            if any(r.get("origin") != "improvement" for r in rows):
                ready = [r for r in ready if r.get("origin") != "improvement"]
            if not ready:
                return None
            task = max(ready, key=lambda r: (r.get("origin") != "improvement",
                                           r.get("priority", 0), -r.get("created", 0)))
            task["claimed"] = now
            _write(self.path, rows)
            return task

    def ready(self):
        now = time.time()
        with self.locked():
            complete = {r["id"] for r in _read(self.done) if r.get("status") == "done"}
            return [r for r in _read(self.path) if not r.get("claimed") and r.get("not_before", 0) <= now
                    and not r.get("blocked_reason")
                    and set(r.get("depends_on", [])).issubset(complete)]

    MAX_HARNESS_FAILURES = 3

    def release(self, tid, ok, note="", defer=0, split_now=False, failure_class="task"):
        """Returns the task with its new status: done, retry, split, parked or held.

        `failure_class` decides whether this failure counts against the task at all. A harness
        failure — rounds, wall clock, a restart, a sandbox, a provider — is not evidence that the
        task is too big, and it used to cost the task one of its two lives all the same: 90 of
        121 finished tasks were split or parked, every deep-game milestone among them."""
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
            harness = not ok and not defer and failure_class in ("harness", "review_format")
            task["attempts"] = task.get("attempts", 0) + (0 if defer or harness else 1)
            if not harness:
                task["note"] = note[-800:]
            if defer:
                task["status"] = "retry"
                task["not_before"] = time.time() + defer
                rows.append(task)
            elif ok:
                task["status"] = "done"
            elif harness:
                # This says nothing about the task, so it costs the task nothing and teaches the
                # next attempt nothing. It is still counted: a task that only ever fails this way
                # needs a person, not another turn.
                task["harness_failures"] = task.get("harness_failures", 0) + 1
                task["harness_note"] = note[-400:]
                if task["harness_failures"] < self.MAX_HARNESS_FAILURES:
                    task["status"] = "retry"
                    task["not_before"] = time.time() + 120
                    rows.append(task)
                else:
                    task["status"] = "parked"
                    task["note"] = f"needs harness look: {note[-500:]}"
            else:
                # Failure notes travel with the task so the next attempt can learn from them.
                task["notes"] = (task.get("notes", []) + [note[-800:]])[-2:]
                if task["attempts"] < MAX_ATTEMPTS and not split_now:
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

    def get(self, tid):
        with self.locked():
            return next((r for r in _read(self.path) if r["id"] == tid), None)

    @staticmethod
    def blocked_by(tid, rows):
        """Pending tasks that could never be claimed if `tid` went away: the ones that depend on
        it, directly or through another task. Children are not included — only depends_on gates
        claim(), so a child of a removed task still runs."""
        doomed, grew = {tid}, True
        while grew:
            grew = False
            for r in rows:
                if r["id"] not in doomed and doomed.intersection(r.get("depends_on") or []):
                    doomed.add(r["id"])
                    grew = True
        return [r for r in rows if r["id"] in doomed and r["id"] != tid]

    def remove(self, tid, cascade=False):
        """Drop a pending task, e.g. because the developer deleted it in the editor. Tasks that
        depend on it can never be claimed once it is gone, so they go too — but only when the
        caller asks, so a click cannot quietly empty half the queue. Returns the removed rows."""
        with self.locked():
            rows = _read(self.path)
            if not any(r["id"] == tid for r in rows):
                raise KeyError(tid)
            doomed = self.blocked_by(tid, rows)
            if doomed and not cascade:
                raise ValueError(f"{len(doomed)} queued task(s) depend on this one: "
                                 + ", ".join(r["title"] for r in doomed))
            gone = {tid} | {r["id"] for r in doomed}
            _write(self.path, [r for r in rows if r["id"] not in gone])
            return [r for r in rows if r["id"] in gone]

    def clear(self, include_claimed=False):
        """Empty the queue. A claimed task is the one a worker is in the middle of, so it stays
        unless the caller insists. Returns (removed, kept)."""
        with self.locked():
            rows = _read(self.path)
            kept = [] if include_claimed else [r for r in rows if r.get("claimed")]
            keep = {r["id"] for r in kept}
            _write(self.path, kept)
            return [r for r in rows if r["id"] not in keep], kept

    def edit(self, tid, title=None, detail=None, kind=None, priority=None, acceptance=None,
             allow_test_changes=None):
        """Rewrite a pending task in place, keeping its id, its dependencies and its history.
        Only the fields given change. A task being worked on right now is refused: the worker
        already has the old wording. Editing clears a retry backoff, since the point of fixing
        a task is to have it tried again."""
        with self.locked():
            rows = _read(self.path)
            task = next((r for r in rows if r["id"] == tid), None)
            if task is None:
                raise KeyError(tid)
            if task.get("claimed"):
                raise ValueError("a worker is running this task right now; stop the swarm, "
                                 "or wait for the attempt to finish")
            new = dict(task)
            if title is not None:
                if not title.strip():
                    raise ValueError("a task needs a title")
                taken = {r["title"].strip().lower() for r in rows + _read(self.done) if r["id"] != tid}
                if title.strip().lower() in taken:
                    raise ValueError("another task already has that title")
                new["title"] = title.strip()
            if detail is not None:
                new["detail"] = detail.strip()
            if kind is not None:
                if kind not in KINDS:
                    raise ValueError(f"kind must be one of {', '.join(KINDS)}")
                new["kind"] = kind
            if priority is not None:
                if not 0 <= int(priority) <= 2:
                    raise ValueError("priority must be 0, 1 or 2")
                new["priority"] = int(priority)
            if acceptance is not None:
                new["acceptance"] = [a.strip() for a in acceptance if a and a.strip()] or None
                if new["acceptance"] is None:
                    del new["acceptance"]
            if allow_test_changes is not None:
                # Editing the queue is something only a person does, so this is theirs to set.
                new["allow_test_changes"] = bool(allow_test_changes)
                if not new["allow_test_changes"]:
                    del new["allow_test_changes"]
            criteria(new)                      # refuses an unusable acceptance list before it lands
            new.pop("not_before", None)
            _write(self.path, [new if r["id"] == tid else r for r in rows])
            return new

    def retry_now(self, tid):
        """Clear a task's retry backoff so the next free worker takes it. Returns the task."""
        with self.locked():
            rows = _read(self.path)
            task = next((r for r in rows if r["id"] == tid), None)
            if task is None:
                raise KeyError(tid)
            task.pop("not_before", None)
            _write(self.path, rows)
            return task

    def requeue(self, tid, recovery_action=None):
        """Put a finished task — parked, split or done — back in the queue, fresh.

        A task parked for three harness failures was never shown to be wrong, so it comes back
        with its attempt count cleared rather than one strike from being parked again."""
        with self.locked():
            done = _read(self.done)
            task = next((r for r in reversed(done) if r["id"] == tid), None)
            if task is None:
                raise KeyError(tid)
            rows = _read(self.path)
            if any(r["id"] == tid for r in rows):
                raise ValueError("that task is already in the queue")
            if len(rows) >= self.max_queue:
                raise ValueError("the queue is full")
            if not recovery_action or not recovery_action.strip():
                raise ValueError("Record the scoped repair or decision that makes this retry useful.")
            task = dict(task)
            task["previous_outcomes"] = (task.get("previous_outcomes", []) + [
                {k: task.get(k) for k in ("status", "finished", "note", "attempts", "harness_note")}])[-20:]
            task["recovery_action"] = recovery_action.strip()
            task["notes"] = (task.get("notes", []) + ["Recovery decision: " + recovery_action.strip()])[-3:]
            task = dict(task, attempts=0, harness_failures=0)
            for key in ("status", "finished", "not_before", "claimed", "note", "harness_note", "blocked_reason"):
                task.pop(key, None)
            rows.append(task)
            _write(self.path, rows)
            _write(self.done, [r for r in done if r["id"] != tid])
            return task

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

# ------------------------------------------------------------------ resuming interrupted work

def resume_file():
    return STATE / "resume.json"


def load_resume():
    try:
        rows = json.loads(resume_file().read_text())
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def save_resume(rows):
    tmp = resume_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    tmp.replace(resume_file())


def take_resume(task_id):
    """The retained work for this task, removed as it is handed over.

    One shot: a patch that will not apply must not be retried on every attempt for ever."""
    rows = load_resume()
    row = rows.pop(task_id, None)
    if row is not None:
        save_resume(rows)
    return row


def retained_diff(c, attempt):
    """The work an interrupted attempt left behind, as a patch, or "".

    Two places it can be. _cleanup commits unaccepted work to the attempt's branch, so usually
    it is a commit; when the daemon was killed before cleanup ran it is still sitting in the
    worktree. Both are read the same way, against the commit the attempt started from."""
    base, branch = attempt.get("base_commit"), attempt.get("branch")
    if not base or not branch:
        return ""
    # git() strips its output, and `git apply` rejects a patch with no trailing newline.
    if git(["rev-parse", "--verify", "-q", f"{branch}^{{commit}}"], cwd=c["repo"])[0] == 0:
        rc, diff = git(["diff", "--unified=3", base, branch], cwd=c["repo"])
        if rc == 0 and diff.strip():
            return diff + "\n"
    wt = Path(attempt.get("worktree") or "")
    if wt.is_dir() and (wt / ".git").exists():
        git(["add", "-A"], cwd=wt)
        rc, diff = git(["diff", "--cached", "--unified=3", base], cwd=wt)
        if rc == 0 and diff.strip():
            return diff + "\n"
    return ""


def find_interrupted(c, q):
    """Attempts a restart cut short that still have work on disk, keyed by task id.

    54 attempts died this way in one day — 5.6 hours of model turns — and every one of them was
    implemented again from nothing. The work is on a branch; this is how it gets picked up."""
    pending = {t["id"]: t for t in q.pending()}
    found = {}
    for path in sorted((STATE / "attempts").glob("*/attempt.json")):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        task = row.get("task") or {}
        if row.get("finished") or task.get("id") not in pending:
            continue
        diff = retained_diff(c, row)
        if not diff:
            continue
        patch = path.parent / "retained.patch"
        patch.write_text(diff)
        # The newest interrupted attempt for a task wins; attempts sort by name, not by time.
        if found.get(task["id"], {}).get("started", 0) <= row.get("started", 0):
            found[task["id"]] = {
                "attempt": row.get("id"), "branch": row.get("branch"),
                "base_commit": row.get("base_commit"), "patch": str(patch),
                "implementer": row.get("implementer") or row.get("active_model"),
                "started": row.get("started", 0), "lines": diff.count("\n") + 1}
    return found


def note_interrupted(c, q):
    """Record what can be resumed, once, at daemon startup."""
    try:
        found = find_interrupted(c, q)
    except Exception as e:                      # never let this stop a run from starting
        log(f"could not look for interrupted work: {type(e).__name__}: {e}")
        return {}
    if found:
        save_resume(found)
        for tid, row in found.items():
            log(f"{tid}: {row['lines']} lines of work survived an interrupted attempt "
                f"({row['attempt']}); it will be verified rather than written again")
        journal("resume_available", tasks=sorted(found), attempts=[r["attempt"] for r in found.values()])
    return found


# ------------------------------------------------------------------ failure classes

# Whose problem a failure is. Queue.release treated them all alike — a note, an attempt spent,
# and after two attempts a split or a park — so a step limit, a sandbox timeout or a restart
# cost a task one of its two lives and scored the model down for it. 90 of 121 finished tasks
# were split or parked, every deep-game milestone among them.
TASK_STAGES = frozenset({"tests_failed", "rejected", "weakened_tests"})
# Not the model answering badly: the baseline was already broken, trunk moved under the attempt,
# the supervisor declined it, or the run was stopped.
HARNESS_STAGES = frozenset({"baseline", "conflict", "refused", "interrupted", "deferred",
                            "agent_timeout"})
HARNESS_NOTE = re.compile(
    r"step limit|stopped after \d+ rounds|timed out|timeout|exceeded \d+s|sandbox|"
    r"interrupted before completion|deferred|killed|sigkill|provider (?:unavailable|kept failing)|"
    r"not available|empty response|connection|\b50\d\b|\b429\b", re.I)
MODEL_NOTE = re.compile(r"no usable json|malformed|unparseable|could not parse|invalid json|"
                        r"wrote .*as (?:reply )?text|did not follow", re.I)


def failure_class(stage, note="", edited=None):
    """`task`, `model` or `harness` — the only distinction worth acting on differently.

    task     the change was wrong: the tests failed, or the reviewer proved a defect.
    model    the model did not produce usable output, or edited and still changed nothing.
    harness  this swarm lost the turn: rounds, wall clock, a restart, a sandbox, a provider.
    """
    note = str(note or "")
    if stage == "review_format_error":
        return "review_format"
    if stage in TASK_STAGES:
        return "task"
    if stage in HARNESS_STAGES:
        return "harness"
    if stage == "model_error":
        # A provider that returned 5xx or nothing at all is not a model answering badly.
        return "harness" if HARNESS_NOTE.search(note) else "model"
    if stage == "review_error":
        return "harness" if HARNESS_NOTE.search(note) else "model"
    if stage == "no_change":
        # Having tried an edit and still changed nothing is the model's doing. Never reaching an
        # edit is this harness running the rounds out on it.
        return "model" if edited else "harness"
    if MODEL_NOTE.search(note):
        return "model"
    if HARNESS_NOTE.search(note):
        return "harness"
    return "task"


# ------------------------------------------------------------------ earlier attempts

# The one line of a failure that says what to do differently.
FAILING = re.compile(r"^(?:FAIL|ERROR):.*|^E\s{2,}.*|^\s*(?:Assertion|Type|Value|Name|Attribute|"
                     r"Index|Key|Import|Module|Syntax|Runtime|OS|IO)Error\b.*|^\s*assert\s.*",
                     re.M)


def first_failing_line(text):
    """The first line of a test failure worth repeating, or ""."""
    m = FAILING.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip()[:200] if m else ""


def attempt_lines(task, limit=3, max_chars=1_200):
    """What earlier attempts at this task actually hit, one line each.

    This was `json.dumps(sorted(prior)[-3:])[-6000:]`: a JSON dump cut off mid-string, made
    mostly of absolute paths the model cannot open, at the point in the prompt where the model
    is most likely to be reading."""
    relevant = {task["id"], task.get("parent"), *(task.get("depends_on") or [])}
    rows = []
    for path in (STATE / "attempts").glob("*/attempt.json"):
        try:
            row = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("task", {}).get("id") in relevant and row.get("finished"):
            rows.append(row)
    out = []
    for row in sorted(rows, key=lambda r: r["finished"])[-limit:]:
        when = dt.datetime.fromtimestamp(row["finished"]).strftime("%m-%d %H:%M")
        stage = row.get("phase") or "unknown"
        model = row.get("implementer") or row.get("active_model") or "?"
        note = re.sub(r"\s+", " ", str(row.get("note") or "")).strip()
        # Paths and evidence directories are noise here; the failing line is the signal.
        note = re.sub(r"(?:; )?evidence: \S+", "", note).strip(" ;")
        failing = first_failing_line(row.get("note") or "")
        line = f"- {when} {stage} on {model}: {failure_class(stage, note)}"
        if note and note != stage:
            line += f" — {note[:160]}"
        if failing:
            line += f" ({failing})"
        out.append(line)
    text = "\n".join(out)
    return text[:max_chars]


# ------------------------------------------------------------------ context pack

# Paths a task names in its own words. Anything outside this set is a guess about what matters,
# and a guess is what filled these prompts with a lecture on parking-garage real options.
CODE_PATH = re.compile(r"[\w][\w./-]*\.(?:py|js|json|md|html|css)\b")
GAME_DIR = re.compile(r"\b(games/([\w.-]+))/")
# Definitions worth an outline when a file is too long to show: Python and JavaScript
# functions, classes, and the consts a game's module keeps its tables in.
DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function)\s+\w+"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*="
    r"|^\s*\w+\s*[:=]\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)")


def numbered(text, first=1):
    """Lines in read_file's exact format, so an edit_file old_str can be copied out of them."""
    return "\n".join(f"{i:>5}\t{line}" for i, line in enumerate(text.splitlines(), first))


def outline(text):
    """Definitions with their line numbers, for a file too long to show whole.

    A signature is what locates the code; the rest of a 200-character line is body, and an
    outline that repeats it is as big as the file it stands in for."""
    return "\n".join(f"{i:>5}\t{line.rstrip()[:120]}"
                     for i, line in enumerate(text.splitlines(), 1) if DEFINITION.match(line))


def task_text(task):
    rows = task.get("acceptance") or []
    return " ".join([str(task.get("title") or ""), str(task.get("detail") or "")]
                    + [r if isinstance(r, str) else str(r.get("text", "")) for r in rows])


def named_paths(task, also=""):
    """The file paths the task names, in the order it names them, deduplicated."""
    text = task_text(task) + " " + (also or "")
    return list(dict.fromkeys(m.group(0).rstrip(".") for m in CODE_PATH.finditer(text)))


def kin_text(task, q):
    """The words a task inherits from the task it was split out of.

    44 of 121 finished tasks were splits, and a decomposer writes subtasks in terms of behaviour:
    "Add parked vehicle data structure and rendering" names no file, while the task it came out
    of names games/crosstown/game.js. Without this the splits — the ones already failing — are
    exactly the ones that get no code."""
    out = []
    for tid in dict.fromkeys(x for x in (task.get("parent"), task.get("root")) if x and x != task["id"]):
        row = q.get(tid) or next((r for r in _read(q.done) if r.get("id") == tid), None)
        if row:
            out.append(task_text(row))
    return " ".join(out)


def covering_tests(wd, terms, limit=2):
    """Test files that mention what the task is about, first `limit` of them."""
    found = []
    for term in terms:
        if len(found) >= limit or not term:
            break
        rc, out = git(["grep", "-l", "-F", "-i", "--", term, "tests"], cwd=wd)
        if rc == 0:
            for path in out.splitlines():
                if path not in found:
                    found.append(path)
    return found[:limit]


def context_pack(task, wd, limit=20_000, also=""):
    """The code this task is about, in the prompt, in read_file's own format.

    The implementer prompt carried no code at all: 16.6 KB of goal, rules and unrelated
    excerpts, and not one line of the files the task names. A model with 12 tool rounds spent 6
    to 11 of them reading before it could edit anything, and 60 of 189 attempts ran out of
    rounds before the first edit. Everything here comes from paths the task itself names, or
    the task it was split out of, so there is nothing to guess and nothing to pad.
    """
    wd, parts, used = Path(wd), [], 0
    text = task_text(task) + " " + (also or "")
    # The last two sections are small and carry the most per character — which test module to
    # run, and what else is in the directory — so a long file cannot crowd them out.
    reserve = min(2_500, limit // 4)

    def add(header, body, ceiling=None):
        nonlocal used
        # Only blank lines come off: a line's leading spaces are its line number's padding, and
        # read_file's format is the whole point — an old_str is copied out of it byte for byte.
        body = (body or "").strip("\n")
        ceiling = limit if ceiling is None else ceiling
        if not body.strip() or used >= ceiling:
            return False
        room = ceiling - used
        if len(body) > room:
            body = body[:room] + f"\n… [{len(body) - room} chars not shown; read_file for the rest]"
        parts.append(f"{header}\n{body}")
        used += len(body) + len(header) + 2
        return True

    def show(rel, max_lines=400, head=None):
        f = wd / rel
        if not f.is_file():
            return
        try:
            body = f.read_text(errors="replace")
        except OSError:
            return
        lines, ceiling = body.splitlines(), limit - reserve
        if head:
            add(f"--- {rel} (first {min(head, len(lines))} of {len(lines)} lines) ---",
                numbered("\n".join(lines[:head])), ceiling)
            return
        whole = numbered(body)
        # A game's logic runs to 300 lines of 200 characters, so the line count alone is not a
        # size. A file that will not fit gets its definitions, which is still navigable — a
        # file cut off halfway is not.
        if len(lines) <= max_lines and len(whole) <= max(0, ceiling - used):
            add(f"--- {rel} ({len(lines)} lines) ---", whole, ceiling)
        else:
            add(f"--- {rel} ({len(lines)} lines; definitions only, read_file for the rest) ---",
                outline(body), ceiling)

    paths = named_paths(task, also)
    seen = set()
    for rel in paths:
        if (wd / rel).is_file() and rel not in seen:
            seen.add(rel)
            show(rel)
    # A game directory is a unit: what it declares, what its notes say, and its logic.
    for whole, slug in dict.fromkeys(GAME_DIR.findall(text)):
        for rel, kw in ((f"{whole}/manifest.json", {}), (f"{whole}/NOTES.md", {"head": 60}),
                        (f"{whole}/game.js", {})):
            if rel not in seen:
                seen.add(rel)
                show(rel, **kw)
    terms = [slug for _, slug in GAME_DIR.findall(text)] + [Path(rel).stem for rel in paths]
    for rel in covering_tests(wd, list(dict.fromkeys(terms))):
        if rel in seen:
            continue
        seen.add(rel)
        body = (wd / rel).read_text(errors="replace") if (wd / rel).is_file() else ""
        tests = "\n".join(f"{i:>5}\t{line.rstrip()}" for i, line in enumerate(body.splitlines(), 1)
                          if re.match(r"\s*(?:def test_|it\(|test\()", line))
        add(f"--- {rel} (the tests in it; run this module, not the whole suite) ---", tests)
    for d in dict.fromkeys(str(Path(rel).parent) for rel in paths if "/" in rel):
        rc, out = git(["ls-files", "--", d], cwd=wd)
        if rc == 0 and out:
            add(f"--- files in {d} ---", "\n".join(out.splitlines()[:60]))
    if not parts:
        return ""
    return ("CODE THIS TASK TOUCHES (already read for you; line numbers match read_file, so an\n"
            "old_str can be copied straight out of it)\n\n" + "\n\n".join(parts))


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


def waiting(worker, why):
    """Record that a worker is blocked rather than working, so progress reports say so."""
    if why is None:
        _waiting.pop(worker, None)
    else:
        _waiting[worker] = why


def count_study(worker, n=0, reset=False):
    """Study-tool calls made by one worker's turns since its attempt began."""
    with _study_lock:
        if reset:
            return _study_calls.pop(worker, 0)
        _study_calls[worker] = _study_calls.get(worker, 0) + n
        return _study_calls[worker]


def count_usd(worker, usd=0.0, reset=False):
    """What one worker's turns have been charged since its attempt began."""
    with _study_lock:
        if reset:
            return round(_charges.pop(worker, 0.0), 6)
        _charges[worker] = round(_charges.get(worker, 0.0) + usd, 6)
        return _charges[worker]


def count_edits(worker, n=0, reset=False):
    """Edit calls one worker's turns made since its attempt began.

    An attempt that ends with no diff means two different things. A model that called edit_file
    and still changed nothing got it wrong; a model that never reached an edit ran out of the
    rounds this harness gave it. Only the first is the model's failure."""
    with _study_lock:
        if reset:
            return _edit_calls.pop(worker, 0)
        _edit_calls[worker] = _edit_calls.get(worker, 0) + n
        return _edit_calls[worker]


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


# ------------------------------------------------------------------ flint

class CapReached(Exception):
    pass


class NoCredits(Exception):
    pass


class StepLimit(Exception):
    pass


class ModelError(RuntimeError):
    """The model answered badly: malformed, empty or crashed turn."""


class ReviewFormatError(ModelError):
    """A bounded set of reviews failed schema validation on the same tree."""


FAST_IMPLEMENTER = """Perform this small, fully specified edit.
YOUR TASK: {title}
PROJECT GOAL AND RULES
{goal}

{context}

TASK CONTRACT
{spec}
Follow repository instructions and the contract. Change only what the task requires.
Do not commit, switch branches, merge or reset Git. Do not weaken existing tests.
No network calls from code or tests, credentials, or live orders.
Run `{test_cmd}` and report the changes and result in under 150 words.
The supervisor will independently test and review the result.
"""


class AgentTimeout(Exception):
    """A role exceeded its wall-clock budget; partial work still needs verification."""

    def __init__(self, role, model, timeout, logfile):
        self.role, self.model = role, model
        self.timeout, self.logfile = timeout, logfile
        super().__init__(f"agent_timeout: {role} on {model} exceeded {timeout}s; log: {logfile}")


def turn_timeout(c, role):
    return c.get("role_timeouts", {}).get(role, c.get("turn_timeout", 1800))


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


def read_charges(path, role, worker, model):
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


def route_to_paid(c, role, worker, model, why):
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
    _ran_on[worker] = alt
    return alt


def flint(prompt, cwd, c, role, worker, budget, max_steps, model=None):
    """One headless flint turn, paced against the daily allowance.

    The turn may end up on a paid stand-in rather than the model asked for, so it records
    what actually ran in `_ran_on` for the caller to credit. Stale entries are cleared
    first: a turn that runs on the model it was given must not inherit the last one's.
    """
    model = model or (pool(c) or [DEFAULT_MODEL])[0]
    _ran_on.pop(worker, None)
    whole_turn = max(1, int(max_steps or 1))
    while True:
        if _stop.is_set():
            raise Stopped("swarm is stopping")
        if c.get("_discovery") and (_scheduler_drain.is_set() or not improve_enabled(c)):
            raise Stopped("discovery stopped")
        if c.get("_attempt_deadline") and time.time() >= c["_attempt_deadline"]:
            raise AgentTimeout(role, model, 0, "attempt deadline")
        ok, wait, why = budget.check()
        if ok:
            # Starting is not finishing. 21 turns paused part-way on the budget (exit 6) and
            # everything they had done to that point was thrown away, so a turn whose rounds do
            # not all fit goes to a paid model now rather than halfway through. If no paid model
            # is reachable it still runs: a short turn may finish, and idling until midnight is
            # certainly worse.
            if not budget.check(need=whole_turn)[0]:
                route_to_paid(c, role, worker, model,
                              f"only part of a {whole_turn}-round turn fits the free allowance")
                model = _ran_on.get(worker, model)
            break
        # The local count only climbs until midnight. Before paying for this turn or waiting
        # on it, ask OpenRouter whether the free requests are really gone.
        if recheck_allowance(c, budget):
            continue
        # Free capacity is gone. If this swarm has a dollar budget left, the turn runs on a
        # paid model rather than idling until midnight; `paid_stand_in` returns None once the
        # budget is spent, and during the owner's window no paid model is reached for at all.
        if budget.paid_would_help():
            alt = route_to_paid(c, role, worker, model, why)
            if alt:
                model = alt
                break
        # The allowance can be hours from resetting; saying so once a quarter hour is enough,
        # and the worker records that it is waiting rather than working.
        waiting(worker, f"waiting for the allowance: {why}")
        if time.time() - _held.get(worker, 0) > 900:
            _held[worker] = time.time()
            log(f"{role}: holding {wait/60:.0f}m — {why}", worker)
        _stop.wait(max(1, min(wait, 60)))
    _held.pop(worker, None)
    waiting(worker, None)

    env = dict(os.environ)
    env["FLINT_MAX_STEPS"] = str(max_steps)
    # The suite the model is told to leave passing takes 105s in the studio, and flint's bash
    # tool cut off at 120s — 15 seconds from killing the run it was asked to make.
    env["FLINT_BASH_TIMEOUT"] = str(int(c.get("test_timeout", 900)))
    if role not in READ_ONLY_ROLES:
        # This role's product is a diff, so flint offers it one edit-only round before the
        # tool-free one and applies edits it writes as reply text. Without that, a turn spent
        # reading is thrown away whole and scored against the model as "no change".
        env["FLINT_FINAL_EDIT"] = "1"
    if not env.get("OPENROUTER_API_KEY"):
        env.pop("OPENROUTER_API_KEY", None)
    if c.get("study") is False:
        env["FLINT_CORPUS_DB"] = NO_CORPUS
    elif c.get("corpus_db"):
        env["FLINT_CORPUS_DB"] = str(Path(c["corpus_db"]).expanduser())
    env["FLINT_SWARM_BUDGET"] = json.dumps(dict(
        cap=budget.cap, reserve=budget.reserve, owner_window=c.get("owner_window", ["00:00", "00:00"])))
    # Shared when the budget is monthly, per-repository when it is daily. flint charges real
    # costs here and refuses a paid request the remaining budget can no longer cover.
    env["FLINT_SPEND_FILE"] = str(spend_file(c))
    # flint knows what OpenRouter charged; this is how that number gets back here.
    charge_file = LOGS / f"{worker}-{role}-{time.time_ns()}.charges.jsonl"
    env["FLINT_CHARGE_FILE"] = str(charge_file)
    if spend_cap(c) is not None:
        env["FLINT_SPEND_CAP"] = str(spend_cap(c))
    if spend_reset_day(c) is not None:
        env["FLINT_SPEND_RESET_DAY"] = str(spend_reset_day(c))
    # The prompt is 10-25 KB. In argv it shows up in `ps`, in every exception's str() and in
    # any crash report, so it goes to a file the attempt is keeping anyway.
    prompt_file = LOGS / f"{worker}-{role}-{time.time_ns()}.prompt.txt"
    prompt_file.write_text(prompt)
    cmd = [c.get("python", sys.executable), str(ROOT / "flint.py"),
           "--prompt-file", str(prompt_file), "-C", str(cwd), "-m", model,
           "--read-only" if role in READ_ONLY_ROLES else "--yolo"]
    cmd = sandboxed(cmd, cwd, c)
    timeout = turn_timeout(c, role)
    log(f"{role}: asking {model} (up to {max_steps} rounds; timeout {timeout}s)", worker)
    t0 = time.time()
    # Keep progress/errors separate from the role's machine-readable answer.
    logfile = LOGS / f"{worker}-{role}-{time.time_ns()}.log"
    expired = False
    _active_turns[worker] = f"{role} with {model}"
    _turn_logs[worker] = logfile
    try:
        with open(logfile, "w") as err:
            with process(cmd, stdout=subprocess.PIPE, stderr=err,
                         text=True, env=env) as p:
                try:
                    while True:
                        if c.get("_discovery") and (_scheduler_drain.is_set() or not improve_enabled(c)):
                            raise Stopped("discovery stopped")
                        remaining = timeout - (time.time() - t0)
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(cmd, timeout)
                        try:
                            out, _ = p.communicate(timeout=min(2, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            if time.time() - t0 >= timeout:
                                raise
                except subprocess.TimeoutExpired:
                    kill_group(p)
                    out, _ = p.communicate()
                    expired = True
                except BaseException:
                    kill_group(p)
                    p.communicate()
                    raise
    finally:
        _active_turns.pop(worker, None)
        _turn_logs.pop(worker, None)
    progress = logfile.read_text(errors="replace")
    if re.search(r"^tool: ", progress, re.M):
        mark_progress(f"{role}: tool activity completed")
    studied = len(re.findall(r"^tool: study$", progress, re.M))
    count_study(worker, studied)
    count_edits(worker, len(re.findall(r"^tool: (?:edit_file|write_file)$", progress, re.M)))
    usd = read_charges(charge_file, role, worker, model)
    journal("turn", role=role, worker=worker, model=model, rc=p.returncode,
            secs=round(time.time() - t0), chars=len(out), log=str(logfile), study_calls=studied,
            usd=usd)
    error = progress[-1200:]
    with open(logfile, "a") as f:  # keep the answer beside the progress for later review
        f.write(f"\n--- answer from {model} (exit {p.returncode}) ---\n{out}\n")
    # Ctrl-C kills the turn's process, which would otherwise look like a crashed model and
    # cost it twice the usual penalty weight. Stopping the run is not the model's doing.
    if _stop.is_set() and p.returncode != 0:
        raise Stopped(f"{role} turn on {model} was stopped with the run")
    if expired:
        journal("agent_timeout", role=role, worker=worker, model=model,
                timeout=timeout, secs=round(time.time() - t0), log=str(logfile))
        raise AgentTimeout(role, model, timeout, logfile)
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
               "slides, notes, problem sets and transcripts on deep learning and machine learning, probability, matrix calculus, discrete math and combinatorics, algorithms and Python, mathematical finance, fintech, blockchain, risk and decision analysis, venture finance, microeconomics, game theory, public finance, perception and psychology, math for computer science, cryptography (interactive proofs, SNARGs), poker strategy, semiconductor microfabrication, game design, plus past code "
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

# The order is deliberate. A model attends most to the end of a long prompt, so the task's own
# contract, rules and acceptance criteria go last; the goal and the reference material, which
# were previously the freshest text in a 16.6 KB prompt, come first.
IMPLEMENTER = """You are the IMPLEMENTER in an engineering swarm.
YOUR TASK: {title}

PROJECT GOAL
{goal}

{context}

{playbook}

{corpus}
{previous}

Implement this task in the current repository. It is a checkout of the swarm's
trunk, which already contains the swarm's earlier accepted work: build on it.

Within the task's intent you have creative latitude. If a cleaner design or a
small extra touch makes the result genuinely better for the person using it,
do it and test it. Creativity that serves the goal is rewarded; scope creep
and churn are not.

Rules that are not negotiable:
- Your change must leave `{test_cmd}` passing. While you work, run the narrowest
  test that covers what you changed — the context pack above names the module,
  e.g. `python3 -m unittest tests.test_table_games -q`. The supervisor runs the
  whole command afterwards, so you do not have to.
- Do not delete, skip, relax or weaken any existing test to get a green run.
  If an existing test is genuinely wrong, leave it failing and say so.
- Add tests that prove the new behavior, including at least one edge case.
- Change as little as possible outside what the task needs.
- Do not commit, switch branches, merge or reset Git; the supervisor manages Git.
- No network calls from code or tests, no credentials, no placing of live orders.
- If a known algorithm or technique applies and a study tool is available, look it up.

THE TASK, IN FULL — this contract controls the scope, and its acceptance
criteria are what the reviewer will check:
{spec}

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
{priorities}
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
{{"impact": 0, "creativity": 0, "quality": 0, "goal_item": 0, "breakthrough": false, "why": "one sentence", "lesson": "one sentence", "follow_ups": [{{"title": "...", "detail": "..."}}]}}"""

PRIORITIES = """
THE GOAL'S ORDERED PRIORITIES — 1 is what matters most:
{items}
Set "goal_item" to the number of the priority this change serves, or 0 if it
serves none of them. Judge honestly: useful work that serves nothing on this
list is still 0.
"""

PLANNER = """You are the PLANNER for an autonomous engineering swarm that works around the clock.

PROJECT GOAL
{goal}
{priorities}
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
{serves_rule}
Output ONLY a JSON array, no prose around it:
[{{"title": "...", "detail": "...", "kind": "feature|bugfix|test|refactor"{serves_field}}}]"""

PLANNER_SERVES = """- Every task must serve one of the goal's numbered priorities, and say which in
  "serves". A task that serves none of them will be dropped. At least half of
  this batch must serve priority 1: that is what the goal says matters most, and
  it is where nothing has landed.
"""

DECOMPOSER = """You are the DECOMPOSER in an engineering swarm. This task timed out or failed repeatedly.
Split it into 2 or 3 smaller tasks that together achieve it, each small enough
to succeed as a verified increment. Subtasks execute in array order: each depends
on the preceding subtask landing successfully. Do not assume unlanded work exists.

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

def baseline_key(c, commit):
    """What a cached baseline pass is a pass *of*: this commit and these exact commands."""
    parts = [str(commit), c.get("test_cmd", ""), *(c.get("validation_commands") or [])]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def baseline_cache(key, passed=None, ttl=86_400):
    """Read or record that trunk at this commit passed its own gate.

    Every attempt ran the full suite before the model started, and the studio's suite takes 105
    seconds. Nothing about trunk changed between one attempt and the next, so most of those runs
    proved the same thing over again."""
    path = STATE / "baseline.json"
    if passed is None:
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if row.get("key") != key or not row.get("passed"):
            return None
        return row if time.time() - row.get("at", 0) < ttl else None
    row = {"key": key, "passed": bool(passed), "at": time.time()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(row))
    tmp.replace(path)
    return row


def run_gate(cwd, c):
    cmd = sandboxed(c["test_cmd"], cwd, c)
    rc, out = sh(cmd, cwd=cwd, timeout=c.get("test_timeout", 900), env=clean_env())
    if c.get("_gate_log"):
        Path(c["_gate_log"]).write_text(f"command: {c['test_cmd']}\nexit: {rc}\n{out}")
    return rc == 0, out[-4000:]


ASSERT_CALL = re.compile(
    r"\b(assert\w*(?:\.\w+)*|expect|require|should\w*|t\.(?:Error|Fatal)f?)\b")
# Directory names too common to identify anything: "games" is not what "games/reversi" means.
GENERIC_DIR = frozenset({"", ".", "/", "src", "lib", "app", "test", "tests", "spec", "specs",
                         "games", "site", "docs", "scripts", "static", "public", "assets"})


def assert_counts(lines):
    """How many times each assertion function is called across these lines."""
    counts = {}
    for line in lines:
        for m in ASSERT_CALL.finditer(line):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def identifiers(path):
    """The names that stand for this path in code: the path, its directory, and their names."""
    p = Path(path)
    out = {str(path), p.name, p.stem}
    parent = p.parent
    if str(parent) not in GENERIC_DIR:
        out |= {str(parent), parent.name}
    return {x for x in out if x and x not in GENERIC_DIR}


def deleted_paths(diff):
    """Every name that identifies something this diff deletes."""
    out, pending = set(), None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            pending = line.split(" b/", 1)[-1].strip()
        elif line.startswith("deleted file mode") and pending:
            out |= identifiers(pending)
            pending = None
    return out


def test_hunks(diff):
    """[(path, deleted, [hunk lines])] for the test files this diff touches."""
    files, path, deleted, is_test, hunks = [], None, False, False, None

    def close():
        if is_test and path:
            files.append((path, deleted, hunks))
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            close()
            path = line.split(" b/", 1)[-1].strip()
            low = path.lower()
            is_test = any(m in low for m in ("test", "spec_", "_spec"))
            deleted, hunks = False, []
        elif line.startswith("deleted file mode"):
            deleted = True
        elif line.startswith("@@") and is_test:
            hunks.append([])
        elif is_test and hunks and line[:1] in ("+", "-", " "):
            hunks[-1].append(line)
    close()
    return files


def names_deleted(line, gone):
    """True when this line refers to something the diff deletes.

    A test that loads games/star-catcher/game.js writes it as
    path.join(__dirname, '..', 'games', 'star-catcher', 'game.js'), so the slug is what can be
    matched, not the joined path."""
    return any(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", line) for name in gone)


def names_its_source(test_path, gone):
    """True when a deleted test file is named after something else the diff deletes.

    tests/test_reversi.py deleted alongside games/reversi/ is one removal, not a weakening;
    tests/test_core.py deleted while the code stays is the clearest weakening there is."""
    stem = Path(test_path).stem.lower()
    return any(len(name) >= 3 and name.lower() in stem for name in gone)


def weakened_tests(diff, allow_test_changes=False):
    """Assertions this diff took out of test files that are still meant to exist.

    The old check counted every removed line containing "assert" under any path containing
    "test". All four hits across 189 studio attempts were legitimate, and this check carries the
    heaviest penalty in the ledger (weight 6) and hangs the model on the rafters, so a false
    positive punishes a model for doing what the owner asked:

      - two attempts deleted games/reversi/ and games/star-catcher/ and the assertions that
        loaded them, which is what "remove star catcher and reversi" means;
      - one was the owner's own removal request, again;
      - one changed `assert.equal(b[112], 3)` to `assert.equal(b[80], 3)` — an assertion edited
        to match an 8x15 board, not an assertion removed.

    So: count per assertion function, treat a removal paired with an addition of the same
    function in the same hunk as an edit, skip hunks that remove code for files the diff deletes,
    and skip a test file deleted alongside the source it tested.
    """
    if allow_test_changes:
        return 0                       # the owner authorised this; the adversary is told so
    total, all_gone = 0, deleted_paths(diff)
    for path, deleted, hunks in test_hunks(diff):
        # A file cannot be its own deleted source, or every deleted test file would excuse itself.
        gone = all_gone - identifiers(path)
        if deleted:
            if names_its_source(path, gone):
                continue               # the test went with the code it tested
            # A test file deleted while the code it tests stays is the plainest weakening there
            # is, so every assertion it took with it counts.
            for lines in hunks:
                total += sum(assert_counts([l[1:] for l in lines if l.startswith("-")]).values())
            continue
        for lines in hunks:
            minus = [l[1:] for l in lines if l.startswith("-")]
            plus = [l[1:] for l in lines if l.startswith("+")]
            if gone and any(names_deleted(l, gone) for l in minus):
                continue               # this hunk removes the use of something that is gone
            back = assert_counts(plus)
            for name, n in assert_counts(minus).items():
                total += max(0, n - back.get(name, 0))
    return total


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

ROUND_LINE = re.compile(r"^round (\d+)/(\d+):", re.M)


def turn_round(worker):
    """(round, of) for the turn this worker is running, from the lines flint prints, or None."""
    path = _turn_logs.get(worker)
    try:
        tail = Path(path).read_text(errors="replace")[-4000:]
    except (OSError, TypeError):
        return None
    hits = ROUND_LINE.findall(tail)
    return (int(hits[-1][0]), int(hits[-1][1])) if hits else None


def improve_settings(c):
    saved = improvement.read(STATE / "improvement-settings.json")
    return {"enabled": saved.get("enabled", c.get("improve_when_idle", False)) is True,
            "paths": saved.get("paths", c.get("improvement_paths", []))}


def improve_enabled(c):
    return improve_settings(c)["enabled"]


def mark_progress(reason):
    with _run_lock:
        _run["last_progress_at"] = time.time()
        _run["last_progress"] = reason


def begin_run(c):
    run_id = os.environ.get("FLINT_RUN_ID") or uuid.uuid4().hex
    os.environ["FLINT_RUN_ID"] = run_id
    saved = improvement.read(STATE / "run.json")
    with _run_lock:
        _run.clear()
        _run.update(saved if saved.get("id") == run_id else
                    {"id": run_id, "started": time.time(), "blocked_seconds": 0})
        _run.update(state="Running", stop_reason=None,
                    deadline=float(os.environ["FLINT_RUN_DEADLINE"]) if os.environ.get("FLINT_RUN_DEADLINE") else None)
        improvement.write(STATE / "run.json", _run)


def assigned_blockers(c, q):
    if c.get("roadmap_file"):
        report = improvement.read(STATE / "roadmap-execution.json")
        blockers = report.get("blockers", {})
        if blockers:
            return [dict(blockers[pid], id=pid) for pid in report.get("root_blockers", []) or blockers]
    with q.locked():
        pending, done = _read(q.path), _read(q.done)
    pending_ids = {t["id"] for t in pending}
    return [{"id": t["id"], "reason": t.get("blocked_reason") or t.get("note") or "Assigned task unfinished",
             "owner": "repository owner", "recovery_action": "Resolve or explicitly cancel this task before discovery."}
            for t in pending + done if t.get("origin") != "improvement"
            and (t in pending or (t["id"] not in pending_ids and t.get("status") in ("parked", "split")
                 and not (c.get("roadmap_file") and (t.get("packet_id") or t.get("title") in {
                     p.get("title") for p in report.get("blockers", {}).values()}))))]


_tool_observed = {}


def run_snapshot(workers, c):
    """Activity clocks are independent: a heartbeat is never evidence of progress."""
    now = time.time()
    for name, logfile in list(_turn_logs.items()):
        try:
            count = len(re.findall(r"^tool: ", Path(logfile).read_text(errors="replace"), re.M))
        except OSError:
            continue
        key = str(logfile)
        if count > _tool_observed.get(key, 0):
            _tool_observed[key] = count
            mark_progress(f"{name}: tool activity")
    q = Queue()
    pending = q.pending()
    blocked = assigned_blockers(c, q)
    cycles = improvement.read(STATE / "improvements.json")
    if _stop.is_set():
        state, reason = "Stopped", _run.get("stop_reason") or "Stop requested"
    elif _scheduler_drain.is_set():
        state, reason = "Draining", "Finishing current work before stopping or restarting"
    elif workers:
        state = "Waiting" if all(w.get("waiting") for w in workers) else "Running"
        reason = workers[0].get("waiting") or workers[0].get("doing") or "Working"
    elif cycles.get("phase") == "discovering":
        state, reason = "Running", "Finding an improvement"
    elif any(t.get("claimed") for t in pending):
        state, reason = "Waiting", "Reconciling active attempts"
    elif blocked and (not pending or not q.ready() or all(t.get("blocked_reason") for t in pending)):
        state, reason = "Blocked", blocked[0]["reason"]
    else:
        state, reason = "Waiting", cycles.get("reason") or "Waiting for ready work"
    with _run_lock:
        was_blocked = _run.get("blocked_since")
        if state == "Blocked":
            _run.setdefault("blocked_since", now)
        elif was_blocked:
            _run["blocked_seconds"] = _run.get("blocked_seconds", 0) + now - was_blocked
            _run.pop("blocked_since", None)
        _run.update(state=state, reason=reason, heartbeat_at=now)
        improvement.write(STATE / "run.json", _run)
        result = dict(_run)
    result.update(heartbeat_age=0, progress_age=now - result["last_progress_at"] if result.get("last_progress_at") else None,
                  blocked_duration=result.get("blocked_seconds", 0) + (now - result["blocked_since"] if result.get("blocked_since") else 0),
                  remaining_seconds=max(0, result["deadline"] - now) if result.get("deadline") else None,
                  blockers=blocked, improvement={**improve_settings(c), "phase": cycles.get("phase"),
                      "selected": cycles.get("selected"), "next_scan": cycles.get("next_scan"),
                      "reason": cycles.get("reason"), "cycle_id": cycles.get("active_cycle")})
    return result


def discovery_allowed(c, q, budget):
    if _stop.is_set() or _scheduler_drain.is_set() or not improve_enabled(c):
        return False
    if os.environ.get("FLINT_RUN_DEADLINE") and hours_left(None) is None:
        return False
    return not q.pending() and not assigned_blockers(c, q) and can_take_a_turn(c, budget)


def discover_improvement(c, q, budget, ledger):
    """One bounded cycle; called by one scheduler thread under a cross-process lock."""
    with open(STATE / "improvement.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        data = improvement.read(STATE / "improvements.json")
        # Reconcile the preceding cycle from durable queue results after a restart.
        current = data.get("selected") or {}
        if current.get("task_id"):
            tid = current["task_id"]
            if q.get(tid):
                return
            outcome = next((t for t in reversed(_read(q.done)) if t["id"] == tid), {})
            accepted = outcome.get("status") == "done"
            data["no_progress"] = 0 if accepted else data.get("no_progress", 0) + 1
            disposition = "accepted" if accepted else "rejected"
            data.setdefault("seen", {})[current["id"]] = {
                "disposition": disposition, "evidence_key": current["evidence_key"],
                "baseline_commit": current["baseline_commit"]}
            data.setdefault("cycles", []).append({"id": data.get("active_cycle"), "task_id": tid,
                "proposal_id": current["id"], "outcome": disposition, "finished": time.time()})
            data["cycles"] = data["cycles"][-100:]
            data["selected"] = None
            data["phase"] = "waiting"
            if data["no_progress"] >= 3:
                data["next_scan"] = time.time() + 1800
                data["reason"] = "Three cycles produced no accepted change; discovery cooldown"
                data["no_progress"] = 0
            improvement.write(STATE / "improvements.json", data)
        if not discovery_allowed(c, q, budget):
            return
        _, baseline = git(["rev-parse", trunk_name(c)], cwd=c["repo"], check=True)
        if data.get("next_scan", 0) > time.time() and data.get("baseline") == baseline:
            return
        try:
            scope = improvement.paths(improve_settings(c)["paths"])
        except ValueError as exc:
            data.update(phase="waiting", reason=str(exc), next_scan=None)
            improvement.write(STATE / "improvements.json", data)
            return
        model = ledger.pick("planner", pool(c, "planner"))
        if model is None:
            return
        cycle = uuid.uuid4().hex
        data.update(phase="discovering", active_cycle=cycle, baseline=baseline,
                    reason="Finding an improvement", run_id=_run.get("id"), next_scan=None)
        improvement.write(STATE / "improvements.json", data)
        journal("improvement_discovery", cycle_id=cycle, baseline=baseline)
        try:
            with _view_lock:
                view = refresh_view(c)
                history = {"tasks": q.recent_titles(40), "proposals": data.get("backlog", []),
                           "decisions": data.get("seen", {})}
                prompt = improvement.PROMPT.format(goal=read_goal(dict(c, repo=str(view))),
                    scope=json.dumps(scope), history=json.dumps(history)[-24000:])
                config = dict(c, _discovery=True, allow_paid=False, study=False)
                config["role_timeouts"] = dict(c.get("role_timeouts", {}), planner=300)
                raw = flint(prompt, view, config, "planner", "discovery", budget,
                            min(8, c["steps"].get("planner", 8)), model)
            proposals = improvement.candidates(json_array(raw), scope, baseline)
            seen = data.get("seen", {})
            proposals = [p for p in proposals if p["id"] not in seen or (
                seen[p["id"]].get("disposition") == "rejected"
                and seen[p["id"]].get("evidence_key") != p["evidence_key"]
                and seen[p["id"]].get("baseline_commit") != baseline)]
            data["backlog"] = (proposals + data.get("backlog", []))[:20]
            _, current_base = git(["rev-parse", trunk_name(c)], cwd=c["repo"], check=True)
            if not discovery_allowed(c, q, budget) or current_base != baseline:
                data.update(phase="waiting", reason="Assigned work, stop, or a changed baseline postponed discovery")
                return
            if proposals:
                selected = proposals[0]
                detail = (selected["problem"] + "\nEVIDENCE: " + selected["evidence"]
                          + "\nBENEFIT: " + selected["benefit"] + "\nVERIFY: " + selected["verification"])
                task = q.add(selected["title"], detail, "feature", origin="improvement", priority=-1,
                    acceptance=selected["acceptance"], allowed_paths=selected["allowed_paths"],
                    cycle_id=cycle, proposal_id=selected["id"], evidence=selected["evidence"],
                    benefit=selected["benefit"], baseline_commit=baseline, effort_minutes=selected["effort_minutes"])
                if task:
                    data.update(selected=dict(selected, task_id=task["id"]), phase="queued",
                                reason=selected["benefit"])
                    journal("improvement_selected", cycle_id=cycle, id=task["id"], proposal_id=selected["id"])
                    return
            data["no_progress"] = data.get("no_progress", 0) + 1
            data.update(phase="waiting", reason="Waiting — no supported improvement found")
        except (Stopped, CapReached):
            data.update(phase="waiting", reason="Discovery paused by stop, drain, or request allowance")
        except Exception as exc:
            data["no_progress"] = data.get("no_progress", 0) + 1
            data.update(phase="waiting", reason=f"Discovery failed: {str(exc)[:200]}")
            journal("improvement_discovery_error", cycle_id=cycle, error=str(exc)[:400])
        finally:
            if data.get("phase") != "queued":
                cooldown = 1800 if data.get("no_progress", 0) >= 3 else 300
                data["next_scan"] = time.time() + cooldown
                if cooldown == 1800:
                    data["no_progress"] = 0
                data.setdefault("cycles", []).append({"id": cycle, "baseline": baseline,
                    "outcome": data.get("reason"), "finished": time.time()})
                data["cycles"] = data["cycles"][-100:]
            improvement.write(STATE / "improvements.json", data)


def write_now(workers, c=None):
    """What the swarm is doing, for the panel's first line.

    The panel's top line was a list of counters — free requests, a spend meter, a corpus chunk
    count — none of which says whether the swarm is working or what on."""
    rows = []
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
            "model": _ran_on.get(w.name) or model or None,
            "round": rounds and rounds[0], "rounds": rounds and rounds[1],
            "since": since, "seconds": round(time.time() - since),
            "waiting": _waiting.get(w.name), "doing": getattr(w, "doing", None)})
    note = {"at": time.time(), "workers": rows}
    if c is not None:
        note["run"] = run_snapshot(rows, c)
    if not rows and c is not None:
        ledger = Ledger(STATE / "learn.json")
        resting = ledger.resting()
        if resting and all(m in {m for m, _, _ in resting} for m in pool(c)):
            first = min(u for _, u, _ in resting)
            note["idle"] = "all models resting"
            note["back"] = first
        elif _waiting:
            note["idle"] = sorted(_waiting.values())[0]
    tmp = (STATE / "now.json").with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(note))
        tmp.replace(STATE / "now.json")
    except OSError:
        pass                              # a status line is never worth failing a run over
    return note


TRAILER = re.compile(r"^Swarm-([\w-]+):\s*(.*)$", re.M)


def landed_commits(c, limit=20):
    """The trunk commits this swarm landed, newest first, with who wrote them and what for."""
    # Trunk itself, not base..trunk: once trunk is merged, base..trunk is empty and the swarm's
    # own history would read as nothing landed. A Swarm-Task trailer is what makes it the
    # swarm's, so the merge cannot hide it either.
    rc, out = git(["log", f"--format=%H%x1f%h%x1f%s%x1f%at%x1f%b%x1e", "-n", str(limit * 4),
                   trunk_name(c)], cwd=c["repo"])
    if rc != 0 or not out.strip():
        return []
    rows = [r for r in _read(STATE / "journal.jsonl") if r["event"] in ("attempt", "reward")]
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


def health(hours=24):
    """The numbers that say whether the swarm is working well, over the last `hours`.

    None of this was anywhere: the panel showed free requests and a corpus chunk count while the
    landed rate sat at 10% for a day and a half."""
    since = time.time() - hours * 3600
    rows = [r for r in _read(STATE / "journal.jsonl") if r.get("t", 0) >= since]
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


def read_now(max_age=180):
    """The daemon's last `now` note, or None when it is missing or stale."""
    try:
        note = json.loads((STATE / "now.json").read_text())
    except (OSError, ValueError):
        return None
    return note if time.time() - note.get("at", 0) <= max_age else None


def report_progress(workers, said, every=180):
    """Say that a working worker is alive, and on what. A turn can be silent for minutes —
    a slow free model, a first `go build` fetching modules — which reads as a freeze."""
    now = time.time()
    for w in workers:
        task, since = getattr(w, "task", None), getattr(w, "started", 0)
        if not (task and since and w.is_alive()):
            said.pop(w.name, None)
            continue
        if now - said.get(w.name, since) > every:
            said[w.name] = now
            log(f"still on '{task['title'][:60]}' ({(now - since) / 60:.0f}m): "
                f"{_waiting.get(w.name) or _active_turns.get(w.name) or getattr(w, 'doing', '?')}", w.name)


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
        self.agent_timed_out = False
        self.failure_class = "task"

    # -- helpers -----------------------------------------------------------

    def pick(self, exclude=(), role="implementer"):
        """Implementers by Thompson sampling. Reviewers and judges come from the
        same posterior, excluding the implementer so nobody grades their own work.

        `role` is the job this model is being drawn for, so `role_models` can keep a model
        out of the roles it is bad at while leaving it in the ones it is good at. The
        posterior is still the implementer's: one model's record is one record.

        When every free model is resting, a paid stand-in keeps the attempt alive rather
        than abandoning work already done — but only while the day's budget allows."""
        m = self.ledger.pick("implementer", pool(self.c, role), exclude=exclude)
        if m is not None:
            return m
        left = spend_left(self.c)
        if left is not None and left <= 0:
            return None
        return next((p for p in paid_pool(self.c) if p not in set(exclude)), None)

    def no_review(self):
        """Models the owner has barred from reviewing (config `reviewer_exclude`)."""
        return set(self.c.get("reviewer_exclude") or ())

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
        deadline = getattr(self, "attempt_deadline", None)
        if deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AgentTimeout(role, model, 0, "attempt deadline")
            c = dict(c, _attempt_deadline=deadline)
            c["role_timeouts"] = dict(c.get("role_timeouts", {}),
                                       **{role: min(turn_timeout(c, role), max(1, int(remaining)))})
        if getattr(self, "execution_class", "standard") == "fast":
            c = dict(c, study=False)
            if role in ("implementer", "repair"):
                steps = min(steps, 4)
                c["role_timeouts"] = dict(c.get("role_timeouts", {}), **{role: 300})
        self.doing = f"{role} with {model}"
        editing = role not in READ_ONLY_ROLES and self.wt is not None and Path(cwd) == Path(self.wt)
        before = self.snapshot()[0] if editing else None
        tried = set()
        while True:
            self.doing = f"{role} with {model}"
            try:
                out = flint(prompt, cwd, c, role, self.name, self.budget, steps, model)
                # A paid stand-in may have taken the turn. Credit the model that actually
                # answered, or the bandit learns from work another model did: a model with
                # no paid twin would be scored for every turn `paid_stand_in` handed away.
                ran = _ran_on.pop(self.name, None)
                if ran and ran != model:
                    asked = model
                    model = self.last_model = ran
                    self.doing = f"{role} with {ran}"
                    if self.evidence:
                        self.evidence.record(role, role_calls=self.role_calls, active_model=ran,
                                             stood_in_for=asked)
                break
            except AgentTimeout as exc:
                self.last_model = exc.model
                self.agent_timed_out = True
                if self.evidence:
                    self.evidence.record("agent_timeout", role=role, active_model=exc.model,
                                         timeout=exc.timeout, log=str(exc.logfile))
                log(str(exc), self.name)
                raise
            except ProviderDown as e:
                log(rest(self.ledger, e), self.name)
                tried.add(model)
                if editing and self.snapshot()[0] != before:
                    raise
                nxt = self.pick(set(avoid) | tried | (self.no_review() if role == "adversary" else set()),
                                role=role)
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

    def adopt_retained(self, task, wd):
        """Re-apply work an interrupted attempt left behind, or None.

        The patch is applied to a worktree freshly branched from trunk rather than the old
        worktree being reused: trunk has moved on, and a stale checkout is a worse starting
        point than none. If it no longer applies, the attempt simply proceeds as normal — the
        model writes it again, which is exactly what used to happen every time."""
        row = take_resume(task["id"])
        if not row:
            return None
        patch = Path(row.get("patch") or "")
        if not patch.is_file():
            return None
        rc, out = git(["apply", "--index", "--whitespace=nowarn", str(patch)], cwd=wd)
        if rc != 0:
            log(f"{task['id']}: the retained work no longer applies to trunk; "
                f"implementing it again ({out.strip()[:160]})", self.name)
            journal("resume_failed", id=task["id"], attempt=row.get("attempt"),
                    reason=out.strip()[:300])
            git(["reset", "--hard"], cwd=wd)     # leave the worktree exactly as it was
            return None
        log(f"{task['id']}: re-applied {row.get('lines', '?')} lines from the interrupted "
            f"attempt {row.get('attempt')}; verifying rather than rewriting", self.name)
        journal("resumed", id=task["id"], attempt=row.get("attempt"),
                implementer=row.get("implementer"), lines=row.get("lines"))
        return row

    def own_branch(self):
        rc, ref = git(["symbolic-ref", "-q", "HEAD"], cwd=self.wt)
        return rc == 0 and ref == f"refs/heads/{self.branch}"

    # -- one task ----------------------------------------------------------

    def do_task(self, task, goal):
        self.task, self.stage, self.wt = task, "error", None
        self.doing, self.started = "starting", time.time()
        self.attempt_deadline = (self.started + min(1200, self.c.get("improvement_timeout", 1200))
                                 if task.get("origin") == "improvement" else None)
        self.evidence, self.role_calls, self.gate_count = None, 0, 0
        self.mit = None
        self.agent_timed_out = False
        self.failure_class = "task"
        self.execution_class = task.get("execution_class", "standard")
        count_study(self.name, reset=True)
        count_edits(self.name, reset=True)
        count_usd(self.name, reset=True)
        note, info = "attempt interrupted before completion", {}
        try:
            self.stage, note, info = self._attempt(task, goal, info)
        except AgentTimeout as exc:
            self.stage, note = "agent_timeout", str(exc)
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
        self.failure_class = ("task" if self.stage == "accepted" else
                              failure_class(self.stage, note, edited=info.get("edit_calls")))
        if info.get("implementer"):
            try:
                self._reinforce(task, self.stage, info)
            except Exception as exc:
                journal("learning_error", id=task["id"], err=str(exc), stage=self.stage)
                log(f"learning update failed; task outcome retained: {exc}", self.name)
            # One row per attempt that spent model calls: the MIT experiment's raw data.
            journal("attempt", id=task["id"], title=task["title"], stage=self.stage,
                    goal_item=info.get("goal_item"), usd=count_usd(self.name, reset=True),
                    failure_class=None if self.stage == "accepted" else self.failure_class,
                    edit_calls=info.get("edit_calls"),
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
        commands = list(dict.fromkeys([self.c["test_cmd"], *self.c.get("validation_commands", []),
                                       *self.task.get("verification_commands", [])]))
        evidence, passed = [], True
        for i, command in enumerate(commands):
            name = f"gate-{self.gate_count:02d}-{label}-{i}.log"
            config = dict(self.c, test_cmd=command, _gate_log=str(self.evidence.path / name))
            if self.attempt_deadline:
                remaining = self.attempt_deadline - time.time()
                if remaining <= 0:
                    raise AgentTimeout("verification", None, 0, "attempt deadline")
                config["test_timeout"] = min(config.get("test_timeout", 900), max(1, remaining))
            started = time.monotonic()
            try:
                ok, output = run_gate(self.wt, config)
            except subprocess.TimeoutExpired:
                ok, output = False, "verification command timed out"
                Path(config["_gate_log"]).write_text(f"command: {command}\n{output}\n")
            row = {"command": command, "passed": ok, "log": name,
                   "seconds": round(time.monotonic() - started, 3), "tail": output}
            evidence.append(row)
            mark_progress("verification completed")
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

    def review(self, tree, diff, tests, model, avoid=(), allow_test_changes=False):
        self.evidence.record("reviewing", reviewed_tree=tree, reviewer=model)
        note = ("\nThe owner authorised test changes for this task; check that the changes to "
                "existing tests match what was asked, and reject them if they do not.\n"
                if allow_test_changes else "")
        prompt = REVIEW.format(contract=json.dumps(self.contract, indent=2), tree=tree,
                               tests=tests, diff=diff[:self.c.get("max_diff", 24000)]) + note
        tried, correction = set(), ""
        retries = max(0, min(2, int(self.c.get("review_format_retries", 2))))
        for retry in range(retries + 1):
            raw = self.call("adversary", prompt + correction, self.wt,
                            self.c["steps"]["adversary"], model, avoid)
            after, _ = self.snapshot()
            if after != tree:
                raise ModelError("reviewer modified the candidate it was asked to inspect")
            try:
                review = parse_review(raw, tree, self.contract["acceptance"])
            except ValueError as exc:
                self.evidence.write(f"review-{self.role_calls:02d}-invalid.txt", raw)
                self.evidence.record("review_format_error", error=str(exc), format_retry=retry,
                                     reviewed_tree=tree, reviewer=self.last_model or model)
                journal("review_format_error", id=self.task["id"], tree=tree,
                        reviewer=self.last_model or model, retry=retry, error=str(exc))
                if retry == retries or self.role_calls >= self.c.get("max_role_calls", 10):
                    raise ReviewFormatError(f"invalid review after {retry + 1} calls: {exc}") from exc
                tried.add(self.last_model or model)
                model = self.pick(set(avoid) | self.no_review() | tried, role="adversary") or model
                correction = (f"\nThe previous response failed validation: {exc}. Reinspect the SAME "
                              "tree; return a complete JSON review. Include each criterion exactly "
                              "once, with evidence, and verification for every finding.\n")
                continue
            self.evidence.write(f"review-{self.role_calls:02d}.json", review)
            return review

    def _attempt(self, task, goal, info):
        c, w = self.c, self.name
        if task.get("kind") == "harness":
            return "refused", "target a separate harness checkout; live harness editing is disabled", info
        reason = task_blocker(c, task)
        if reason:
            return "refused", reason, info
        log(f"claim {task['id']} — {task['title']}", w)
        self.ensure_worktree(task)
        wd = self.wt
        self.evidence = Attempt(STATE, self.branch.split("/")[-1], task, wd, self.branch)
        self.evidence.record("claimed", run_id=_run.get("id"))
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
        # Trunk at this commit either passed its own gate recently or it did not; running the
        # suite again proves the same thing and costs the studio 105 seconds per attempt.
        key = baseline_key(c, self.review_base)
        cached = baseline_cache(key, ttl=c.get("baseline_ttl", 86_400))
        if cached:
            ok, output = True, f"baseline: cached pass for {self.review_base[:12]}"
            self.evidence.record("baseline_cached", base_commit=self.review_base,
                                 at=cached["at"], key=key)
        else:
            ok, output = self.gate("baseline")
            baseline_cache(key, passed=ok)
        _, dirty = git(["status", "--porcelain"], cwd=wd, check=True)
        if not ok or dirty:
            return "baseline", "baseline tests failed or changed tracked/unignored files; no model calls spent:\n" + output, info
        fast = self.execution_class == "fast"
        self.mit = info["mit"] = "off" if fast else mit_arm(c)
        corpus = study(f"{task['title']} {task['detail']}", c) if self.mit == "on" else ""
        info["mit_injected"] = bool(corpus)
        self.evidence.record("study", mit=self.mit, injected=bool(corpus))
        # The code the task names, read once here rather than over and over by the model.
        kin = kin_text(task, self.q)
        context = context_pack(task, wd, also=kin)
        info["context_chars"] = len(context)
        self.evidence.record("context", chars=len(context), paths=named_paths(task, kin))
        lessons, pitfalls = ([], []) if fast else self.ledger.playbook()
        # Lessons shown to this implementer share the attempt's reward (see Ledger.credit).
        info["lessons"] = [l["id"] for l in lessons + pitfalls if l.get("kind") in ("lesson", "pitfall")]
        impl = self.pick()
        if impl is None:
            raise ProviderDown(None, "every model in the pool is resting after provider failures")
        info["implementer"] = impl
        prompt_contract = {k: v for k, v in self.contract.items() if k != "goal"}
        prompt_contract["detail"] = task["detail"]
        spec = json.dumps(prompt_contract, indent=2)
        if not fast:
            spec += "\nSTRATEGY: " + STRATEGIES[strategy]
        if not fast and c["steps"].get("architect", 0):
            spec += "\nARCHITECT NOTES (the contract still controls scope):\n" + self.call(
                "architect", ARCHITECT.format(goal=goal, title=task["title"], detail=task["detail"],
                 corpus=corpus, test_cmd=c["test_cmd"]), wd, c["steps"]["architect"],
                 self.pick({impl}, role="architect") or impl,
                avoid={impl})
        # What earlier attempts hit, as lines to act on. Parent handoffs stay artifacts on disk,
        # not mutable model memory, so only their outcome is repeated here.
        lines = attempt_lines(task)
        previous = f"\nWHAT EARLIER ATTEMPTS AT THIS TASK HIT\n{lines}\n" if lines else ""
        # Work a restart cut short, if there is any: re-apply it and go straight to the gate.
        # It still faces the tests and the adversary; nothing is trusted, only reused.
        resumed = self.adopt_retained(task, wd)
        if resumed:
            info["resumed_from"] = resumed.get("attempt")
            impl = info["implementer"] = resumed.get("implementer") or impl
            self.evidence.record("resumed", attempt=resumed.get("attempt"), implementer=impl,
                                 lines=resumed.get("lines"))
        else:
            self.evidence.record("implementing", implementer=impl)
            try:
                handoff = self.call("implementer", (FAST_IMPLEMENTER if fast else IMPLEMENTER).format(
                    goal=goal, spec=spec, previous=previous, corpus=corpus, test_cmd=c["test_cmd"],
                    context=context, title=task["title"][:200],
                    playbook=format_playbook(lessons, pitfalls, named_paths(task, kin))),
                    wd, c["steps"]["implementer"], impl)
                self.evidence.write("implementation.txt", handoff)
            except AgentTimeout:
                self.evidence.record("timeout_recovery", note="verify partial implementation through normal gates")
            except StepLimit:
                self.evidence.record("step_limit", note="verify the partial implementation before continuing")
            except ModelError as exc:
                info["implementer"] = self.last_model or impl
                return "model_error", str(exc), info
            # After a handoff the model that actually wrote the code is its author.
            impl = info["implementer"] = self.last_model or impl
        adv = self.pick({impl} | self.no_review(), role="adversary") or impl
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
            outside = self.outside_scope()
            if outside:
                return "rejected", "candidate changed paths outside its contract: " + ", ".join(outside), info
            if not diff.strip():
                info["edit_calls"] = tried = count_edits(w)
                stage = "agent_timeout" if self.agent_timed_out else "no_change"
                how = (f"{tried} edit call(s) changed nothing" if tried
                       else "the turn never reached an edit")
                return stage, f"no implementation changes: {how}; evidence: {self.evidence.path}", info
            allowed = bool(task.get("allow_test_changes"))
            lost = weakened_tests(diff, allow_test_changes=allowed)
            if lost:
                return ("weakened_tests",
                        f"{lost} existing assertion(s) removed with nothing in their place; "
                        "manual review required", info)
            ok, tests = self.gate(f"candidate-{cycle}")
            after, _ = self.snapshot()
            if after != tree:
                return "rejected", "test command modified the candidate; fix test isolation/ignored files", info
            review = None
            if ok:
                try:
                    review = self.review(tree, diff, tests, adv, avoid={impl}, allow_test_changes=allowed)
                except ReviewFormatError as exc:
                    info["reviewer"] = self.last_model or adv
                    return "review_format_error", str(exc), info
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
                                    failure=failure, test_cmd=c["test_cmd"], context=context), wd,
                                    c["steps"].get("repair", c["steps"]["implementer"]), impl, avoid={adv})
                self.evidence.write(f"repair-{cycle + 1}.txt", handoff)
            except AgentTimeout:
                self.evidence.record("timeout_recovery", note="verify partial repair through normal gates")
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
        with _run_lock:
            _run.update(last_accepted_at=time.time(), last_accepted_commit=info["commit"])
        mark_progress("accepted commit")
        log(f"{task['id']}: landed on {trunk_name(c)}; evidence: {self.evidence.path}", w)
        # A third model scores the landed change; its lesson and follow-ups feed the playbook.
        info["judge"] = self.pick({impl, adv}, role="judge") or adv
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
            reason = task_blocker(c, self.task)
            if reason or not self.q.get(self.task["id"]):
                return False, reason or "task was cancelled before integration"
            _, old = git(["rev-parse", t], cwd=c["repo"], check=True)
            rc, _ = git(["merge-base", "--is-ancestor", old, "HEAD"], cwd=self.wt)
            if rc != 0:
                rc, _ = git(["rebase", old], cwd=self.wt)
                if rc != 0:
                    git(["rebase", "--abort"], cwd=self.wt)
                    return False, f"conflicts with newer trunk; work retained on {self.branch}"
                self.review_base = old
                tree, diff = self.snapshot()
                if self.outside_scope():
                    return False, "rebased candidate changed paths outside its contract"
                ok, output = self.gate("rebased")
                after, _ = self.snapshot()
                if not ok or after != tree or weakened_tests(
                        diff, allow_test_changes=bool(self.task.get("allow_test_changes"))):
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
        items = goal_items(goal)
        priorities = PRIORITIES.format(
            items="\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))) if items else ""
        try:
            out = self.call("judge", JUDGE.format(
                goal=goal, priorities=priorities, title=task["title"],
                detail=task["detail"], verdict=verdict,
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
        if stage != "accepted" and failure_class(stage, info.get("note", ""),
                                                 edited=info.get("edit_calls")) == "harness":
            # The model answered; this swarm lost the turn. Scoring it down at penalty weight
            # teaches the bandit about our step limits, not about the model.
            journal("harness_failure", id=task["id"], title=task["title"], stage=stage,
                    implementer=impl, edit_calls=info.get("edit_calls"))
            log(f"{task['id']}: {stage} — harness failure, not scored against {impl}", self.name)
            return
        scores = info.get("scores")
        text = f"{task['title']} {task.get('detail', '')}"
        nov = novelty(text, L.accepted_texts()) if stage == "accepted" else 0.0
        if stage == "review_error" and info.get("reviewer"):
            # A reviewer that cannot deliver a verdict wastes the implementer's work.
            L.update("implementer", info["reviewer"], 0.0, weight=PENALTY_WEIGHT["model_error"])
            log(f"reviewer {info['reviewer']} failed to deliver a verdict; penalised", self.name)
        items = goal_items(read_goal(self.c))
        info["goal_item"] = int((scores or {}).get("goal_item") or 0) if items else None
        r = reward(stage, scores, nov, repairs=info.get("repairs", 0),
                   goal_weight=goal_item_weight(info["goal_item"], items))
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
                goal_item=info.get("goal_item"),
                scores={k: v for k, v in (scores or {}).items() if k != "follow_ups"},
                breakthrough=breakthrough)
        log(f"{task['id']}: {stage}, reward {r:.2f}{' ★ BREAKTHROUGH' if breakthrough else ''}", self.name)

    def follow_up(self, task, scores, r, breakthrough):
        """Recursion on success: strong work spawns the next step it made possible."""
        if self.c.get("roadmap_file") or task.get("origin") == "improvement":
            return  # Future packets are dispatched only by their recorded roadmap gates.
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

    def strand(self, task, how):
        """Park what depends on a task that can no longer finish, and say so loudly."""
        titles = self.q.strand(task["id"], f"prerequisite '{task['title'][:80]}' {how}")
        if titles:
            log(f"'{task['title'][:60]}' {how}; parked {len(titles)} task(s) that depended on it: "
                + "; ".join(t[:50] for t in titles), self.name)
            journal("stranded", id=task["id"], title=task["title"], how=how, parked=titles)
        return titles

    def decompose(self, task, goal, avoid=()):
        """Split after a timeout or repeated failure, within the recursion limits.

        A split that returns no subtasks strands the task and everything waiting on it, so one
        empty answer is retried on a different model before the task is given up on."""
        c = self.c
        if c.get("roadmap_file"):
            return 0  # A coordinator must repoint dependencies before splitting a packet.
        # Between attempts: not the last attempt's evidence, role-call budget or MIT arm.
        self.evidence, self.mit = None, None
        self.execution_class = "standard"
        model = (self.ledger.pick("planner", pool(c, "decomposer"), exclude=set(avoid))
                 or self.pick(set(avoid), role="decomposer"))
        if model is None:
            return 0
        with _view_lock:
            view = refresh_view(c)
            try:
                out = self.call("decomposer", DECOMPOSER.format(
                    goal=goal, title=task["title"], detail=task["detail"], test_cmd=c["test_cmd"],
                    notes="\n---\n".join(task.get("notes", [])) or task.get("note", "")),
                    view, c["steps"].get("decomposer", 6), model)
            except (StepLimit, ModelError, AgentTimeout) as e:
                self.ledger.update("planner", model, 0.0)
                log(f"decomposer {model} failed ({type(e).__name__}) on '{task['title']}'", self.name)
                return self.retry_split(task, goal, model, avoid, f"{type(e).__name__}")
        subtasks = json_array(out)
        if subtasks is None:
            self.ledger.update("planner", model, 0.0)
            subtasks = []
        added = 0
        dependencies = list(task.get("depends_on", []))
        for t in subtasks[:3]:
            if (not isinstance(t, dict) or not isinstance(t.get("title"), str)
                    or t.get("kind", "feature") not in KINDS):
                break  # Do not enqueue a dependent suffix without its prerequisite.
            child = self.q.add(t["title"], str(t.get("detail", "")), t.get("kind", "feature"),
                               persona=task.get("persona"), planner_model=model,
                               parent=task["id"], priority=max(1, task.get("priority", 1)), origin="split",
                               depends_on=dependencies, acceptance=t.get("acceptance"),
                               depth=task.get("depth", 0) + 1)
            if not child:
                break
            added += 1
            dependencies = [child["id"]]
        if not added:
            return self.retry_split(task, goal, model, avoid, "no usable subtasks")
        moved = self.q.repoint(task["id"], dependencies[0])
        log(f"split '{task['title']}' into {added} smaller task(s)"
            + (f"; {moved} waiting task(s) moved to the last child" if moved else ""), self.name)
        journal("split", id=task["id"], title=task["title"], added=added, model=model, repointed=moved)
        return added

    def retry_split(self, task, goal, model, avoid, why):
        """One more decomposition on a different model. Returns 0 once that has been tried."""
        if avoid:
            log(f"'{task['title']}' was not split: {why} from {model} as well", self.name)
            journal("split_failed", id=task["id"], title=task["title"], why=why,
                    models=[*avoid, model])
            return 0
        log(f"decomposer {model}: {why}; trying another model before giving up on "
            f"'{task['title']}'", self.name)
        return self.decompose(task, goal, avoid={*avoid, model})

    # -- loop --------------------------------------------------------------

    def outside_scope(self):
        scope = self.task.get("allowed_paths")
        if not scope:
            return []
        _, names = git(["diff", "--cached", "--name-only", "--no-renames", "-z", self.review_base],
                       cwd=self.wt, check=True)
        return [p for p in names.split("\0") if p and not improvement.contains(p, scope)]

    def clear_active(self):
        self.task, self.started, self.doing = None, 0, None
        for table in (_active_turns, _turn_logs, _waiting, _held, _ran_on):
            table.pop(self.name, None)

    def release_task(self, *args, **kwargs):
        try:
            return self.q.release(*args, **kwargs)
        finally:
            self.clear_active()

    def run(self):
        while not self.stop.is_set() and not self.tally.drain.is_set():
            # Between tasks, never inside one: an edited config reaches this run without a
            # restart, and a restart is what killed 54 attempts in this window.
            watch_config(self.c, self.budget)
            try:
                if self.c.get("roadmap_file"):
                    dispatch_roadmap(self.c, self.q)
                task = self.q.claim(eligible=lambda t: not t.get("blocked_reason"),
                                    improvements=improve_enabled(self.c))
            except Exception as exc:
                log(f"dispatch blocked: {exc}", self.name)
                self.stop.wait(20)
                continue
            if not task:
                self.stop.wait(20)
                continue
            try:
                goal = read_goal(self.c)
                ok, note = self.do_task(task, goal)
                # A timeout only forces a split when there was work to be too big for. With no
                # diff there is nothing to divide, and the timeout is ours, not the task's.
                final = self.release_task(task["id"], ok, note, failure_class=self.failure_class,
                                       split_now=self.agent_timed_out and self.stage != "agent_timeout")
                journal("task", id=task["id"], title=task["title"], ok=ok, stage=self.stage,
                        failure_class=None if ok else self.failure_class,
                        worker=self.name, note=note[:300])
                if final and final.get("status") == "split":
                    if not self.decompose(final, goal):
                        self.strand(final, "was split into no subtasks")
                elif final and final.get("status") == "parked":
                    self.strand(final, "was parked after repeated failures")
                self.tally.finished()
            except CapReached:
                log("quota/window pause — deferring task", self.name)
                self.release_task(task["id"], False, "quota/window pause", defer=60,
                               failure_class="harness")
                # 22 of these in one day, ten minutes each, while the local count read
                # 1025/1000 and OpenRouter reported fewer requests used. Ask before sleeping,
                # and sleep no longer than the interval at which the next ask is due.
                if recheck_allowance(self.c, self.budget):
                    continue
                self.stop.wait(min(600, allowance_recheck(self.c) or 600))
            except ProviderDown as e:
                self.release_task(task["id"], False, f"provider unavailable: {str(e)[:200]}", defer=30,
                               failure_class="harness")
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
                self.release_task(task["id"], False, "no credits")
                self.stop.set()
            except Exception as e:
                if self.stop.is_set():
                    self.release_task(task["id"], False, "run stopped; changes retained", defer=1,
                                   failure_class="harness")
                    return
                described = describe_error(e)
                log(f"error on {task['id']}: {described['kind']}: {described['err']}", self.name)
                journal("error", id=task["id"], tb=traceback.format_exc()[-1200:], **described)
                # A turn that timed out is still a failed attempt: let it split like any other,
                # or the task is shelved with no smaller pieces to try.
                # An unexpected exception in the supervisor is ours, not the task's.
                final = self.release_task(task["id"], False, str(e)[:400], failure_class="harness")
                if final and final.get("status") == "split":
                    try:
                        self.decompose(final, read_goal(self.c))
                    except Exception as exc:
                        log(f"could not split '{final['title']}': {exc}", self.name)
                self.stop.wait(30)
            finally:
                self.clear_active()


# ------------------------------------------------------------------ planning

def serving_the_goal(tasks, items):
    """(tasks to queue, how many were dropped) for one planner batch.

    The judge rewarded a thumbnail validator 0.94 and a configurable word list 0.97 while the
    four deep games the goal puts first landed nothing, and those rewards are what taught the
    planner to keep proposing tooling. With no ranked priorities nothing is dropped: a goal that
    does not say what comes first cannot say a task serves nothing."""
    if not items:
        return tasks, 0

    def serves(t):
        try:
            n = int(t.get("serves"))
        except (TypeError, ValueError):
            return 0
        return n if 1 <= n <= len(items) else 0
    on_goal = [t for t in tasks if serves(t)]
    first = [t for t in on_goal if serves(t) == 1]
    # Half the batch has to be the first priority, so the rest is capped by how much of it
    # there is. No first-priority task means no batch: the planner is asked again, on another
    # model, rather than filling the queue with fifth things.
    rest = [t for t in on_goal if serves(t) != 1][:len(first)]
    kept = [t for t in tasks if t in first or t in rest]
    return kept, len(tasks) - len(kept)


def plan(c, q, budget, n=6, ledger=None):
    ledger = ledger or Ledger(STATE / "learn.json")
    ensure_trunk(c)
    goal = read_goal(c)
    persona = ledger.pick("persona", list(PERSONAS)) or "builder"
    model = ledger.pick("planner", pool(c, "planner"))
    if model is None:
        log(f"planner: {all_resting(ledger, c)}; will retry")
        return 0
    items = goal_items(goal)
    priorities = PRIORITIES.format(
        items="\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))) if items else ""
    serves_rule = PLANNER_SERVES if items else ""
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
                goal=goal, priorities=priorities, serves_rule=serves_rule,
                serves_field=', "serves": 1' if items else "",
                persona_name=persona, persona=persona_text(persona, c), landed=landed,
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
    tasks = [t for t in tasks[:n] if isinstance(t, dict) and isinstance(t.get("title"), str)
             and isinstance(t.get("detail", ""), str) and t.get("kind", "feature") in KINDS]
    kept, dropped = serving_the_goal(tasks, items)
    added = 0
    for task in kept:
        if q.add(task["title"], task.get("detail", ""), task.get("kind", "feature"),
                 persona=persona, planner_model=model, origin="plan",
                 serves=task.get("serves") if items else None):
            added += 1
    if dropped:
        log(f"planner: dropped {dropped} task(s) that did not serve the goal's priorities")
    log(f"planner queued {added} task(s)")
    journal("plan", added=added, dropped=dropped, persona=persona, model=model,
            priorities=len(items))
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


def allowance_recheck(c):
    """Seconds between asking OpenRouter whether a spent free allowance is back; 0 or unset is off."""
    try:
        return max(0, int(c.get("allowance_recheck") or 0))
    except (TypeError, ValueError):
        return 0


def recheck_allowance(c, budget):
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
    with _recheck_lock:
        if time.time() - _rechecked["at"] < every:
            return False
        _rechecked["at"] = time.time()
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
            _rechecked["said"] = False
            log(f"free allowance is back: OpenRouter reports {used}/{limit} used "
                f"(counted {counted} here); free models again")
            journal("allowance_restored", used=used, limit=limit, counted=counted)
            return True
        if not _rechecked["said"]:
            _rechecked["said"] = True
            log(f"free allowance spent: OpenRouter reports {used}/{limit} used; "
                f"rechecking every {every / 60:g}m")
        return False


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


# Every key the swarm reads. Anything else in a config is a typo or something that was
# renamed, and both fail silently: the setting simply never takes effect.
KNOWN_KEYS = frozenset({
    "repo", "base_branch", "trunk", "test_cmd", "validation_commands", "test_timeout",
    "python", "goal_file", "roadmap_file", "workers", "tick", "models", "paid_models", "allow_paid",
    "model", "steps", "role_timeouts", "turn_timeout", "max_repairs", "max_diff",
    "max_depth", "max_queue", "max_children", "max_descendants", "plan_batch",
    "plan_cooldown", "reviewer_exclude", "daily_cap", "reserve", "owner_window",
    "daily_usd", "monthly_usd", "spend_reset_day", "allowance_recheck", "editor_wallet",
    "corpus_db", "corpus_root", "corpus_k", "study", "inject_corpus", "mit_experiment",
    "sandbox", "sandbox_write", "keep_worktrees", "notify", "baseline_ttl", "personas",
    "restart_on_change", "restart_min_interval", "role_models",
    "execution_class", "_comment"})
# Seconds a round of tool use really takes on these models, from the studio's own turns.
SECONDS_PER_ROUND = 47


def config_warnings(c):
    """Everything about this config that will not do what it looks like it does.

    A config is read in silence: an unknown key never takes effect, a step budget that cannot
    fit its timeout gets the turn SIGKILLed mid-work, and a pool of paid ids under allow_paid
    means every turn is paid however much free allowance is left."""
    out = []
    unknown = sorted(k for k in c if k not in KNOWN_KEYS and not k.startswith("_"))
    if unknown:
        out.append(f"unknown setting(s) {', '.join(unknown)} — nothing reads these, so they "
                   "do nothing; check for a typo or a rename")
    for role, steps in (c.get("steps") or {}).items():
        budget = turn_timeout(c, role)
        need = int(steps or 0) * SECONDS_PER_ROUND
        if need > budget:
            out.append(f"steps.{role} is {steps}, which at ~{SECONDS_PER_ROUND}s a round needs "
                       f"~{need}s, past this role's {budget}s timeout: the turn gets killed "
                       f"mid-work and its partial answer is lost")
    models = c.get("models") or []
    if c.get("allow_paid") and models and not any(str(m).endswith(":free") for m in models):
        out.append(f"`models` has no :free model ({', '.join(map(str, models))}) and allow_paid "
                   "is true, so every turn is paid even while the free allowance is untouched")
    return out


def warn_about_config(c, record=False):
    for line in config_warnings(c):
        log(f"WARNING config: {line}")
    if record and config_warnings(c):
        journal("config_warnings", warnings=config_warnings(c), path=str(CONFIG))
    return config_warnings(c)


def preflight(c):
    """Fail fast, before spending requests, on anything that would waste the night."""
    warn_about_config(c, record=True)
    paid = [m for m in (c.get("models") or [c.get("model")]) if m and not m.endswith(":free")]
    if paid and not c.get("allow_paid"):
        sys.exit(f"refusing non-free models {paid}: they spend credits. "
                 "Remove them or set allow_paid: true in swarm/config.json.")
    if c.get("paid_models") and not c.get("allow_paid"):
        sys.exit(f"paid_models {c['paid_models']} would spend credits. "
                 "Set allow_paid: true to let the swarm fall back to them, or remove them.")
    if spend_cap(c) is None and paid_pool(c):
        sys.exit("paid_models needs monthly_usd or daily_usd: a dollar budget for one window, so the fallback "
                 "cannot run up an open-ended bill.")
    stray = [m for m in (c.get("paid_models") or []) if m.endswith(":free")]
    if stray:
        sys.exit(f"paid_models must name paid model ids; {stray} are free. "
                 "Free models belong in `models`.")
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
    if paid_pool(c):
        left = spend_left(c)
        log(f"paid fallback ({len(paid_pool(c))}): {', '.join(paid_pool(c))} — "
            + (f"${spend_cap(c):.2f}/month shared, resetting {spend_resets(c)}" if monthly(c)
               else f"${spend_cap(c):.2f}/day for this repo")
            + (f", ${left:.2f} left" if left is not None else "")
            + "; used only once free capacity is gone")
    else:
        log("paid fallback: off — free models only")
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
    # The same cache the attempt gate uses. Startup runs this suite — 115s in the studio — and
    # the daemon now restarts itself whenever the harness is edited, so an unchanged trunk was
    # about to be re-proved several times an hour.
    _, head = git(["rev-parse", trunk_name(c)], cwd=c["repo"])
    key = baseline_key(c, head)
    if baseline_cache(key, ttl=c.get("baseline_ttl", 86_400)):
        log(f"baseline: cached pass for {trunk_name(c)} at {head[:12]}")
        return
    with _view_lock:
        view = refresh_view(c)
        ok, output = run_gate(view, c)
    baseline_cache(key, passed=ok)
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

def daemon_note():
    """The record a running daemon leaves while it holds this repository's lock, or None.
    A note left behind by a process that is gone is ignored."""
    try:
        d = json.loads((STATE / "daemon.pid").read_text())
        os.kill(int(d["pid"]), 0)
        return d
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def holder():
    """How to watch or stop the daemon already running here."""
    d = daemon_note()
    if not d:
        return ""
    return (f"\n  It started at {dt.datetime.fromtimestamp(d['started']):%H:%M:%S} with the goal: "
            f"{str(d.get('goal', ''))[:120]}\n"
            f"  Watch it:  swarm status\n"
            f"  Stop it:   kill -INT {d['pid']}   (or Ctrl-C in its terminal)")


def roadmap_context(c):
    """Read a single committed snapshot. Queue labels alone never unlock a packet."""
    repo, trunk = c["repo"], trunk_name(c)
    _, revision = git(["rev-parse", trunk], cwd=repo, check=True)
    def committed(path):
        _, raw = git(["show", f"{revision}:{path}"], cwd=repo, check=True)
        return json.loads(raw)
    packets = committed(c["roadmap_file"])["tasks"]
    ledger = committed("docs/roadmap/EXECUTION.json")
    if ledger.get("implementation_started") is not True:
        raise ValueError("roadmap implementation has not been activated")
    accepted = {}
    by_id = {p["id"]: p for p in packets}
    for pid, evidence in ledger.get("packets", {}).items():
        p = by_id.get(pid, {})
        if not p:
            continue
        commit = evidence.get("accepted_commit")
        if evidence.get("status") == "accepted" and evidence.get("evidence"):
            if p.get("dispatch") == "coordinator" or (commit and git(
                    ["merge-base", "--is-ancestor", commit, revision], cwd=repo)[0] == 0):
                accepted[pid] = evidence
    # A packet can also be accepted by a complete, supervisor-owned artifact bundle.
    for path in (STATE / "attempts").glob("*/attempt.json"):
        a = improvement.read(path)
        task = a.get("task", {})
        pid = task.get("packet_id")
        p = by_id.get(pid)
        if not p or pid in accepted or a.get("phase") != "accepted":
            continue
        sha = a.get("commit", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or git(
                ["merge-base", "--is-ancestor", sha, revision], cwd=repo)[0]:
            continue
        saved = improvement.read(path.parent / "contract.json")
        expected = criteria({"title": p["title"], "acceptance": p["acceptance"]})
        if saved.get("packet_id") != pid or saved.get("acceptance") != expected:
            continue
        _, tree = git(["rev-parse", sha + "^{tree}"], cwd=repo)
        gates = a.get("gates", [])
        if (a.get("reviewed_tree") != tree or not gates
                or not all(g.get("passed") is True and g.get("command")
                           and (path.parent / g.get("log", "")).is_file() for g in gates)):
            continue
        if not set(p.get("commands", [])).issubset({g["command"] for g in gates}):
            continue
        for review_path in path.parent.glob("review-*.json"):
            try:
                review = parse_review(review_path.read_text(), tree, expected)
            except (OSError, ValueError):
                continue
            if review["verdict"] == "approve":
                accepted[pid] = {"status": "accepted", "accepted_commit": sha,
                                 "queue_id": task["id"], "evidence": str(path.parent)}
                break
    # Remove chains with unmet prerequisites, even if a ledger says the child is accepted.
    while True:
        invalid = [pid for pid in accepted if any(d not in accepted for d in by_id[pid].get("depends_on", []))
                   or any(not ledger.get("gates", {}).get(g, {}).get("evidence")
                          for g in by_id[pid].get("coordinator_gates", []))]
        if not invalid:
            break
        for pid in invalid:
            accepted.pop(pid)
    return packets, ledger, accepted


def packet_fields(packet):
    scope = improvement.paths(packet.get("write_paths", []))
    return {"packet_id": packet["id"], "acceptance": packet["acceptance"],
            "allowed_paths": scope, "verification_commands": packet.get("commands", []),
            "detail": packet["detail"], "execution_class": packet.get("execution_class", "standard")}


def task_blocker(c, task):
    """Fail closed for broad roadmap requests, for every task origin."""
    if task.get("blocked_reason"):
        return task["blocked_reason"]
    if task.get("allowed_paths"):
        improvement.paths(task["allowed_paths"])
    if not c.get("roadmap_file"):
        return None
    packets, ledger, accepted = roadmap_context(c)
    if task.get("origin") == "improvement":
        if any(p["id"] not in accepted for p in packets):
            return "Assigned roadmap work remains; resolve its root blocker before improvements."
        return None
    packet = next((p for p in packets if p["id"] == task.get("packet_id")), None)
    if not packet or packet.get("dispatch") != "swarm":
        return "Resolve this request into a committed swarm packet; epics and standing rules cannot be implemented as one task."
    for key, value in packet_fields(packet).items():
        if key != "detail" and task.get(key) != value:
            return f"Packet {packet['id']} contract changed ({key}); refresh dispatch before claiming."
    missing = [d for d in packet.get("depends_on", []) if d not in accepted]
    missing += [g for g in packet.get("coordinator_gates", [])
                if not ledger.get("gates", {}).get(g, {}).get("evidence")]
    return "Waiting on " + ", ".join(missing) if missing else None


_roadmap_lock = threading.RLock()


def dispatch_roadmap(c, q):
    with _roadmap_lock:
        return _dispatch_roadmap(c, q)


def _dispatch_roadmap(c, q):
    packets, ledger, accepted = roadmap_context(c)
    by_id = {p["id"]: p for p in packets}
    by_title = {p["title"].strip().lower(): p for p in packets}
    # Canonicalize legacy exact-title tasks, but keep broad requests visible and unclaimable.
    with q.locked():
        pending, done = _read(q.path), _read(q.done)
        for task in pending:
            if task.get("claimed"):
                continue
            if task.get("origin") == "improvement":
                task.pop("blocked_reason", None)
                if len(accepted) < len(packets):
                    task["blocked_reason"] = "Resolve assigned roadmap work before improvements."
                continue
            packet = by_id.get(task.get("packet_id")) or by_title.get(task["title"].strip().lower())
            if packet and packet.get("dispatch") == "swarm":
                task.update(packet_fields(packet))
                task.pop("blocked_reason", None)
                missing = [d for d in packet.get("depends_on", []) if d not in accepted]
                missing += [g for g in packet.get("coordinator_gates", [])
                            if not ledger.get("gates", {}).get(g, {}).get("evidence")]
                if missing:
                    task["blocked_reason"] = "Waiting on " + ", ".join(missing)
                elif packet["id"] in accepted:
                    task["blocked_reason"] = "Packet already accepted; close the duplicate request."
            else:
                task["blocked_reason"] = "Resolve this broad request into committed roadmap packet IDs."
        _write(q.path, pending)
    history = {r.get("packet_id") or by_title.get(r["title"].strip().lower(), {}).get("id"): r
               for r in done + pending}
    added, blocked, blockers = [], {}, {}
    for packet in packets:
        pid = packet["id"]
        if pid in accepted:
            continue
        missing = [d for d in packet.get("depends_on", []) if d not in accepted]
        missing += [g for g in packet.get("coordinator_gates", [])
                    if not ledger.get("gates", {}).get(g, {}).get("evidence")]
        previous = history.get(pid, {})
        parked = previous.get("status") in ("parked", "split", "done", "cancelled")
        if missing or parked or packet.get("dispatch") != "swarm":
            reason = previous.get("note") if parked else "Waiting on " + ", ".join(missing)
            if packet.get("dispatch") != "swarm" and not missing:
                reason = "Coordinator evidence required"
            blocked[pid] = missing or [previous.get("status", "coordinator")]
            blockers[pid] = {"packet_id": pid, "title": packet["title"], "status": "Blocked",
                             "depends_on": missing, "reason": reason or "No verified acceptance",
                             "owner": packet.get("owner") or "repository coordinator",
                             "queue_id": previous.get("id"),
                             "recovery_action": "Record a scoped repair or decision, then requeue this packet; preserve its dependencies."
                             if parked else "Supply acceptance evidence for: " + ", ".join(missing or [pid])}
            continue
        if previous or len(pending) + len(added) >= min(5, q.max_queue):
            continue
        fields = packet_fields(packet)
        detail = fields.pop("detail") + "\nREAD: docs/roadmap/README.md and " + c["roadmap_file"]
        task = q.add(packet["title"], detail, "feature", origin="roadmap",
                     priority=packet.get("priority", 1), serves=1, **fields)
        if task:
            added.append(task)
            log(f"roadmap {pid}: queued {task['id']}")
    roots = [pid for pid, b in blockers.items() if not any(d in blockers for d in b["depends_on"])]
    report = {"updated": dt.datetime.now().isoformat(), "accepted": accepted,
              "blocked": blocked, "blockers": blockers, "root_blockers": roots,
              "pending": [{"id": t["id"], "title": t["title"], "packet_id": t.get("packet_id"),
                           "blocked_reason": t.get("blocked_reason")} for t in pending + added]}
    improvement.write(STATE / "roadmap-execution.json", report)
    return added


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


def start(c, hours=None, max_tasks=None, awake=False):
    if os.environ.get("FLINT_RUN_DEADLINE") and hours_left(hours) is None:
        log("the shift this daemon was restarted into is already over; not starting")
        return
    hours = hours_left(hours)
    if hours and not os.environ.get("FLINT_RUN_DEADLINE"):
        # Fix the end of the shift now, once. Every later restart inherits this exact time, so
        # a run that restarts ten times still ends when the first start said it would.
        os.environ["FLINT_RUN_DEADLINE"] = str(time.time() + hours * 3600)
    with open(STATE / "daemon.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"another swarm daemon is already running on this repository.{holder()}")
        (STATE / "daemon.pid").write_text(json.dumps(
            {"pid": os.getpid(), "started": time.time(), "goal": read_goal(c)[:200],
             "repo": str(Path(c["repo"]).expanduser().resolve()), "config": str(CONFIG)}))
        _stop.clear()
        begin_run(c)
        preflight(c)
        if awake:
            keep_awake()
        timer = None
        if hours:
            # Preflight can run a multi-minute baseline suite. It consumes the shift,
            # too; neither startup verification nor self-restarts may extend the deadline.
            seconds = hours * 3600
            try:
                seconds = min(seconds, max(0, float(os.environ["FLINT_RUN_DEADLINE"]) - time.time()))
            except (KeyError, ValueError):
                pass
            timer = threading.Timer(seconds, shutdown)
            timer.daemon = True
            timer.start()
        signal.signal(signal.SIGTERM, lambda *_: shutdown())
        # A shell starting a job in the background hands it SIGINT set to SIG_IGN, and CPython
        # keeps an inherited SIG_IGN rather than installing its own handler. So a daemon
        # launched with `&` or nohup — which is how every shift is started — silently ignored
        # `swarm stop` and the panel's Stop button. Taking the signal back makes them work.
        signal.signal(signal.SIGINT, lambda *_: shutdown())
        try:
            run_daemon(c, max_tasks, hours=hours)
        finally:
            if timer:
                timer.cancel()
            shutdown()
            (STATE / "daemon.pid").unlink(missing_ok=True)
            (STATE / "now.json").unlink(missing_ok=True)
            log("stopped. `swarm report` summarises the run.")


def run_daemon(c, max_tasks=None, hours=None):
    repo = Path(c["repo"])
    budget = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
                    owner_window=c.get("owner_window", ["00:00", "00:00"]))
    q, stop, tally = Queue(c.get("max_depth", 1)), _stop, Tally(max_tasks)
    ledger = Ledger(STATE / "learn.json")
    q.recover()
    note_interrupted(c, q)
    log(f"swarm up — repo={repo.name} trunk={trunk_name(c)} workers={c['workers']}")
    log(f"config: {CONFIG} ({config_kind()})")
    log(json.dumps(budget.snapshot()))
    # Rests persist in learn.json across runs; say so, or a fresh start looks stuck.
    live = set(pool(c))
    for model, until, why in ledger.resting():
        if model in live:
            log(f"{model} is resting until {dt.datetime.fromtimestamp(until):%H:%M:%S} ({why[:120]})")
    # A count left over from before the restart can say the allowance is spent when
    # OpenRouter says otherwise; settle that before the first turn is routed.
    recheck_allowance(c, budget)

    _cfg_seen["mtime"] = config_mtime()     # so the first check is a real change, not this one
    source = source_fingerprint()
    restart_for, last_restart = [], time.time()
    _drain_reason["stop_requested"] = False
    _scheduler_drain.clear()
    try:
        signal.signal(signal.SIGUSR1, lambda *_: drain(tally))
        log("edit the config to change settings without a restart; "
            f"`swarm stop --drain` or `kill -USR1 {os.getpid()}` stops after this task")
    except ValueError:
        pass                                # not the main thread; a bounded run in a test
    if c.get("roadmap_file"):
        dispatch_roadmap(c, q)
    workers = [Worker(i, c, q, budget, stop, ledger, tally) for i in range(c["workers"])]
    for w in workers:
        w.start()
    last_plan, last_sync, last_beat, dry_runs, plan_fails = 0.0, time.time(), time.time(), 0, 0
    last_bugs = 0.0
    said = {}
    discovery = None
    try:
        while not stop.is_set():
            if tally.drain.is_set():
                _scheduler_drain.set()
                while any(w.is_alive() for w in workers) or (discovery and discovery.is_alive()):
                    write_now(workers, c)
                    if stop.wait(1):
                        break
                if restart_for and _drain_reason["stop_requested"]:
                    log(f"not restarting into {', '.join(restart_for)}: this run was asked to "
                        "stop, and a stop means stop")
                    journal("self_restart_skipped", changed=restart_for, reason="stop requested")
                    restart_for = []
                if restart_for:
                    # Everything is finished and nothing is running: the only safe moment.
                    restart_into_new_code(restart_for)
                    restart_for = []          # it refused; carry on with the code we have
                    tally.drain.clear()
                    _scheduler_drain.clear()
                    workers = [Worker(i, c, q, budget, stop, ledger, tally)
                               for i in range(c["workers"])]
                    for w in workers:
                        w.start()
                    continue
                log(f"finished {tally.n} task(s); stopping")
                break
            # An editor that autosaves must not be able to thrash the daemon.
            if (not restart_for and c.get("restart_on_change", True)
                    and time.time() - last_restart > c.get("restart_min_interval", 300)):
                changed = source_changed(source)
                if changed:
                    source, restart_for, last_restart = source_fingerprint(), changed, time.time()
                    log(f"the harness changed on disk ({', '.join(changed)}); finishing the "
                        "task(s) in flight, then restarting into it")
                    journal("source_changed", changed=changed)
                    drain(tally, stop=False)
            if time.time() - last_sync > 900:
                last_sync = time.time()
                sync_usage(account())
            if time.time() - last_bugs > 300:
                last_bugs = time.time()
                try:
                    new = [] if c.get("roadmap_file") else import_bugs(c, q)
                    if new:
                        log(f"queued {len(new)} player bug report(s): "
                            + "; ".join(t["title"][:60] for t in new))
                except Exception as e:      # a player's report must never stop the run
                    log(f"could not read the bug reports: {type(e).__name__}: {e}")
            report_progress(workers, said)
            write_now(workers, c)
            if time.time() - last_beat > 3600:
                last_beat = time.time()
                log(f"heartbeat {json.dumps(budget.snapshot())}")
            if c.get("roadmap_file"):
                try:
                    dispatch_roadmap(c, q)
                except Exception as exc:
                    log(f"roadmap dispatch blocked: {exc}")
                    _run["dispatch_error"] = str(exc)
                    stop.wait(10)
                    continue
                _run.pop("dispatch_error", None)
            if (not tally.drain.is_set() and improve_enabled(c)
                    and not (discovery and discovery.is_alive())):
                discovery = threading.Thread(target=discover_improvement,
                    args=(c, q, budget, ledger), name="discovery", daemon=True)
                discovery.start()
            stop.wait(min(10, c.get("tick", 90)) if not max_tasks else 5)
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        shutdown()
        for w in workers:
            w.join(timeout=10)
        if discovery:
            discovery.join(timeout=10)
        _run["stop_reason"] = _run.get("stop_reason") or (
            "Shift deadline reached" if _run.get("deadline") and time.time() >= _run["deadline"]
            else "Task limit reached" if tally.limit and tally.n >= tally.limit else "Stop requested")
        write_now([], c)
        journal("run_stopped", reason=_run["stop_reason"])


# ------------------------------------------------------------------ report

def build_report(c, hours=24):
    L = Ledger(STATE / "learn.json").snapshot()
    since = time.time() - hours * 3600
    repo, base, t = c["repo"], c.get("base_branch", "main"), trunk_name(c)
    _, ahead = git(["rev-list", "--count", f"{base}..{t}"], cwd=repo)
    _, commits = git(["log", "--format=- %h %s", "-n", "30", f"{base}..{t}"], cwd=repo)
    accepted = [a for a in L["accepted"] if a["t"] >= since]
    rows = _read(STATE / "journal.jsonl")
    tasks = [j for j in rows if j["event"] == "task" and j["t"] >= since]
    attempts = [j for j in rows if j["event"] == "attempt" and j["t"] >= since]
    stages, classes = {}, {}
    for j in tasks:
        stages[j.get("stage", "?")] = stages.get(j.get("stage", "?"), 0) + 1
        if not j.get("ok") and j.get("failure_class"):
            classes[j["failure_class"]] = classes.get(j["failure_class"], 0) + 1
    parked = [d for d in _read(STATE / "done.jsonl")
              if d.get("status") in ("parked", "split") and d.get("finished", 0) >= since]
    # Each task's own last outcome, so the report says what happened rather than guessing.
    last = {j["id"]: j for j in rows if j["event"] == "task"}
    # What the window cost, from OpenRouter's own numbers rather than a local price table.
    spent = round(sum(a.get("usd") or 0.0 for a in attempts), 6)
    landed_n = sum(1 for a in attempts if a.get("stage") == "accepted")
    by_model = {}
    for a in attempts:
        m = by_model.setdefault(a.get("implementer") or "?", {"attempts": 0, "landed": 0, "usd": 0.0})
        m["attempts"] += 1
        m["landed"] += a.get("stage") == "accepted"
        m["usd"] = round(m["usd"] + (a.get("usd") or 0.0), 6)
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
    if classes:
        out += ["", "Whose failures these were: "
                + ", ".join(f"{k} {v}" for k, v in sorted(classes.items(), key=lambda kv: -kv[1]))
                + ". Only `task` failures say the work was wrong."]
    out += ["", "## Cost", ""]
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
                experiment.markdown(experiment.summary(STATE / "journal.jsonl"))]
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
    warn_config_mismatch(c["repo"], record=True)
    if a is not None and getattr(a, "workers", None):
        c["workers"] = a.workers
    return c


def cmd_grind(a):
    if a.repo:
        # Before configure() reads or rewrites anything: this repository may have a tuned file.
        use_config(a.repo)
    if a.new:
        if not a.goal:
            sys.exit("--new needs --goal describing the problem")
        repo = scaffold(a.new, a.goal)
        use_config(repo)
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
    note = daemon_note()
    print(json.dumps({
        "repo": c["repo"], "trunk": trunk_name(c), "trunk_ahead": ahead,
        "config_path": str(CONFIG), "config_kind": config_kind(),
        "config_warnings": config_warnings(c),
        "config_tuned_available": str(per_repo_config(c["repo"])) if per_repo_config(c["repo"]).is_file() else None,
        "daemon": {"running": bool(note),
                   "pid": note and note.get("pid"),
                   "since": note and dt.datetime.fromtimestamp(note["started"]).isoformat(timespec="seconds"),
                   "goal": note and str(note.get("goal", ""))[:200]},
        "budget": snap,
        "spend": {"used_usd": round(spend_used(c), 6), "cap_usd": spend_cap(c),
                  "window": spend_window(c), "resets": spend_resets(c),
                  "shared": monthly(c), "ledger": str(spend_file(c)),
                  "remaining_usd": (round(spend_left(c), 6) if spend_left(c) is not None else None),
                  "paid_fallback": paid_pool(c)},
        "pacing": {"allowed_now": ok, "wait_s": round(wait), "reason": why,
                   "allowance_recheck_s": allowance_recheck(c)},
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
    q = Queue(c.get("max_depth", 1), c.get("max_queue", 20))
    try:
        depends = q.resolve(a.depends_on) if a.depends_on else None
        t = q.add(a.title, a.detail or "", a.kind, priority=a.priority, origin="human",
                  acceptance=a.acceptance or None, depends_on=depends,
                  execution_class=getattr(a, "execution_class", "standard"),
                  allow_test_changes=getattr(a, "allow_test_changes", False) or None)
    except ValueError as e:
        sys.exit(f"not queued: {e}")
    if not t:
        sys.exit("not queued: that title was already tried, or the queue is full")
    print(f"queued {t['id']}: {t['title']}")
    for row in criteria(t):
        print(f"  {row['id']} {row['text'][:150]}")
    for dep in t.get("depends_on", []):
        print(f"  waits for {dep}")


def cmd_plan(a):
    c = _setup()
    b = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
               owner_window=c.get("owner_window", ["00:00", "00:00"]))
    plan(c, Queue(c.get("max_depth", 1)), b, a.n)


# ------------------------------------------------------------------ player bug reports

BUG_SEVERITY_PRIORITY = {"broken": 2, "annoying": 1, "cosmetic": 0}


def bug_task(row):
    """(title, detail, priority) for a player's bug report, or None if it is not usable.

    The arcade writes reports/bugs.jsonl "so a person or the swarm can read them", and nothing
    in swarm/ ever read it. A report from someone who actually played the game is the best task
    this swarm can be given, and it was going in a file nobody opened."""
    if not isinstance(row, dict):
        return None
    summary = str(row.get("summary") or "").strip()
    if not summary:
        return None
    game = str(row.get("game") or "site").strip() or "site"
    severity = str(row.get("severity") or "annoying")
    lines = [f"A player reported this while playing **{game}**.", "", f"> {summary}"]
    if row.get("details"):
        lines += ["", "What they added:", "", str(row["details"])[:2000]]
    facts = [(k, row.get(k)) for k in ("version", "page", "viewport", "severity") if row.get(k)]
    if facts:
        lines += ["", "Report: " + ", ".join(f"{k} {v}" for k, v in facts)]
    if row.get("state") is not None:
        # The seed and the run state are what make a report reproducible.
        lines += ["", "Game state at the time (use its seed to reproduce):", "",
                  "```json", json.dumps(row["state"], indent=2)[:1500], "```"]
    lines += ["", "Reproduce it first with a failing test, then fix it."]
    return (f"Bug in {game}: {summary[:100]}", "\n".join(lines),
            BUG_SEVERITY_PRIORITY.get(severity, 1))


def import_bugs(c, q, path=None, limit=5):
    """Queue the player bug reports this swarm has not seen. Returns the tasks it added.

    The cursor is a byte-free high-water mark on the report id, so a report is queued once even
    though the arcade's file only ever grows."""
    path = Path(path or Path(c["repo"]) / "reports" / "bugs.jsonl")
    if not path.is_file():
        return []
    try:
        cursor = int(json.loads((STATE / "bugs.cursor").read_text())["id"])
    except (OSError, ValueError, KeyError, TypeError):
        cursor = 0
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row.get("id"), int) and row["id"] > cursor:
            rows.append(row)
    added, seen = [], cursor
    for row in sorted(rows, key=lambda r: r["id"]):
        seen = max(seen, row["id"])
        made = bug_task(row)
        if not made:
            continue
        title, detail, priority = made
        try:
            task = q.add(title, detail, "bugfix", priority=priority, origin="player",
                         acceptance=[f"a test reproduces the report and fails without the fix",
                                     f"{row.get('game', 'the game')} no longer does it"])
        except ValueError:
            task = None
        if task:
            added.append(task)
            journal("bug_imported", id=task["id"], report=row["id"], game=row.get("game"),
                    severity=row.get("severity"), title=title)
        if len(added) >= limit:
            break
    if seen > cursor:
        (STATE / "bugs.cursor").write_text(json.dumps({"id": seen, "at": time.time()}))
    return added


# ------------------------------------------------------------------ launchd service

LAUNCH_AGENTS = Path("~/Library/LaunchAgents")


def service_label(repo):
    return f"com.flint.swarm.{repo_slug(repo)}"


def service_plist_path(repo):
    return (LAUNCH_AGENTS / f"{service_label(repo)}.plist").expanduser()


def service_plist(repo, hours=None, autostart=False, goal="GOAL.md"):
    """The launchd job for one repository's swarm.

    RunAtLoad is off unless asked for: this daemon spends real money, so coming back by itself
    after a reboot has to be a decision someone made, not a side effect of installing."""
    repo = str(Path(repo).expanduser().resolve())
    args = [python_for(load_cfg(repo)), str(HERE / "swarmd.py"), "grind", repo]
    if goal:
        args += ["--goal", goal]
    if hours:
        args += ["--hours", str(hours)]
    # Not LOGS: use_repo rebinds that to the per-repo directory, and this is written before
    # any repo is in use. launchd needs the directory to exist before it will start the job.
    logs = HERE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = str(logs / f"{repo_slug(repo)}.out")
    return {
        "Label": service_label(repo),
        "ProgramArguments": args,
        "EnvironmentVariables": {"FLINT_SWARM_CONFIG": str(config_path_for(repo))},
        "WorkingDirectory": str(ROOT),
        "StandardOutPath": out,
        "StandardErrorPath": out,
        # Restart a crash, but never fight a clean `swarm stop`.
        "KeepAlive": {"SuccessfulExit": False},
        "RunAtLoad": bool(autostart),
        "ProcessType": "Background",
    }


def launchctl(*args):
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def cmd_service(a):
    """Install, inspect or remove the launchd job that keeps this repository's swarm running."""
    if sys.platform != "darwin":
        sys.exit("swarm service manages a macOS launchd agent; this is not macOS")
    repo = str(Path(a.repo or load_cfg().get("repo", ".")).expanduser().resolve())
    path, label, uid = service_plist_path(repo), service_label(repo), os.getuid()
    if a.action == "status":
        rc, out = launchctl("print", f"gui/{uid}/{label}")
        print(json.dumps({"label": label, "plist": str(path), "installed": path.is_file(),
                          "loaded": rc == 0, "autostart": (
                              plistlib.loads(path.read_bytes()).get("RunAtLoad", False)
                              if path.is_file() else None)}, indent=2))
        if rc == 0:
            print("\n".join(l for l in out.splitlines() if re.search(r"state|pid|last exit", l)))
        return
    if a.action == "uninstall":
        rc, out = launchctl("bootout", f"gui/{uid}/{label}")
        path.unlink(missing_ok=True)
        print(f"removed {path}" + ("" if rc == 0 else f" (it was not loaded: {out[:200]})"))
        return
    job = service_plist(repo, hours=a.hours, autostart=a.autostart, goal=a.goal)
    path.parent.mkdir(parents=True, exist_ok=True)
    launchctl("bootout", f"gui/{uid}/{label}")     # replace any earlier copy
    path.write_bytes(plistlib.dumps(job))
    rc, out = launchctl("bootstrap", f"gui/{uid}", str(path))
    if rc != 0:
        sys.exit(f"wrote {path} but launchctl refused it: {out[:400]}")
    print(f"installed {label}\n  config: {job['EnvironmentVariables']['FLINT_SWARM_CONFIG']}"
          f"\n  log:    {job['StandardOutPath']}"
          f"\n  {'starts at login and after a reboot' if a.autostart else 'does NOT start on its own — run `launchctl kickstart gui/%d/%s` to start it' % (uid, label)}"
          f"\n  stop:   swarm stop --drain   ·   remove: swarm service uninstall --repo {repo}")


def cmd_stop(a):
    """Stop this repository's daemon: now, or after the task(s) in flight finish."""
    _setup()
    note = daemon_note()
    if not note:
        print("no swarm daemon is running on this repository")
        return
    pid, how = int(note["pid"]), ("drain" if a.drain else "stop")
    try:
        os.kill(pid, signal.SIGUSR1 if a.drain else signal.SIGINT)
    except (ProcessLookupError, PermissionError) as e:
        sys.exit(f"could not {how} pid {pid}: {type(e).__name__}: {e}")
    print(f"sent {how} to pid {pid}"
          + (" — it will finish the task(s) in flight first" if a.drain
             else " — work in progress is kept on its branch"))


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
    p.add_argument("--execution-class", choices=("standard", "fast"), default="standard",
                   help="fast: explicit mechanical edit; no study/history, at most 4 rounds and 300s")
    p.add_argument("--acceptance", action="append", metavar="TEXT",
                   help="one thing the reviewer must verify; repeat for each (default: the detail)")
    p.add_argument("--depends-on", action="append", metavar="TITLE_OR_ID", dest="depends_on",
                   help="a task that must finish first, by exact title or id; repeat for each")
    p.add_argument("--allow-test-changes", action="store_true", dest="allow_test_changes",
                   help="this task may change or delete existing tests (e.g. removing a game "
                        "and its tests). Only you can set this; the adversary is told.")
    p.set_defaults(fn=cmd_add)
    p2 = sub.add_parser("plan")
    p2.add_argument("-n", type=int, default=5)
    p2.set_defaults(fn=cmd_plan)
    sv = sub.add_parser("service", help="a launchd agent that restarts this repository's swarm")
    sv.add_argument("action", choices=("install", "status", "uninstall"))
    sv.add_argument("--repo", help="the repository to grind (default: the configured one)")
    sv.add_argument("--hours", type=float, help="stop after this many hours per run")
    sv.add_argument("--goal", default="GOAL.md")
    sv.add_argument("--autostart", action="store_true",
                    help="also start at login and after a reboot. This daemon spends money, so "
                         "it is off unless you ask for it.")
    sv.set_defaults(fn=cmd_service)
    st = sub.add_parser("stop", help="stop this repository's daemon")
    st.add_argument("--drain", action="store_true",
                    help="finish the task(s) in flight first, instead of killing them mid-turn")
    st.set_defaults(fn=cmd_stop)
    sub.add_parser("wake", help="end every model's rest now").set_defaults(fn=cmd_wake)
    m = sub.add_parser("models", help="list free tool-capable models; --write sets the pool")
    m.add_argument("--write", action="store_true")
    m.set_defaults(fn=cmd_models)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
