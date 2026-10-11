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
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # LingAI-Trader/
load_dotenv(ROOT / ".env", override=False)
sys.path.insert(0, str(HERE))
from budget import Budget               # noqa: E402
from metrics import (RequestLog, BucketClock, Reconcile, WALL_BUCKETS,  # noqa: E402
                     ACTIVE_MODEL, TESTING, REVIEWING, ALLOWANCE_BLOCKED, IDLE)
from pacing import LatencyLearner, effective_steps        # noqa: E402
from learn import (Ledger, format_playbook, is_breakthrough, json_array,  # noqa: E402
                   novelty, parse_scores, reward, weight, defect, exhibit,
                   FAULTY, CRIME, BREAKTHROUGH_WEIGHT, PENALTY_WEIGHT)
from workflow import Attempt, STRATEGIES, REVIEW, contract, criteria, parse_review, failure_signature  # noqa: E501
import sandbox                          # noqa: E402
import swallow                          # noqa: E402
from workflow import validate_scope, path_allowed
from runtime_context import (RepoPaths, ConfigSnapshot, RunState, EventSink,  # noqa: E402
                             ProcessRegistry, RuntimeContext, TurnBook)
try:  # one class identity for package imports; script mode keeps the historical top-level path
    from .task_queue import (JsonlTaskStore, TaskPolicy, TaskQueue, TaskTransition)  # noqa: E402
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    from task_queue import (JsonlTaskStore, TaskPolicy, TaskQueue, TaskTransition)  # noqa: E402

try:  # workspace resolves against the same module identity as the daemon
    from . import workspace
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import workspace


try:  # agent_turn resolves against the same module identity as the daemon
    from . import agent_turn
    from .agent_turn import (CapReached, NoCredits, StepLimit, ModelError,
                             ReviewFormatError, AgentTimeout, ExplorationExhausted,
                             Stopped, ProviderDown, ProviderBusy, ModelGone,
                             RequestBudget, ModelPoolExhausted)
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import agent_turn
    from agent_turn import (CapReached, NoCredits, StepLimit, ModelError,
                            ReviewFormatError, AgentTimeout, ExplorationExhausted,
                            Stopped, ProviderDown, ProviderBusy, ModelGone,
                            RequestBudget, ModelPoolExhausted)

try:  # attempt_runner resolves against the same module identity as the daemon
    from . import attempt_runner
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import attempt_runner

try:  # worker resolves against the same module identity as the daemon
    from . import worker as worker_loop
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import worker as worker_loop

try:  # reporting resolves against the same module identity as the daemon
    from . import reporting
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import reporting

try:  # planning resolves against the same module identity as the daemon
    from . import planning
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import planning

try:  # roadmap resolves against the same module identity as the daemon
    from . import roadmap
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import roadmap

try:  # supervisor resolves against the same module identity as the daemon
    from . import supervisor
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import supervisor

ROUND_LINE = reporting.ROUND_LINE

BLOCKER_ACTIONS = roadmap.BLOCKER_ACTIONS

# ------------------------------------------------------------------ P2 extracted leaves
#
# prompts / task_rules / context_builder / verification / provisioning hold the code;
# swarmd re-exports every name the rest of the daemon (and the tests) still use, with
# compatibility wrappers where the new module needs an explicit dependency. No new module
# imports swarmd in any form.
from prompts import (FAST_IMPLEMENTER, PERSONAS, ARCHITECT, IMPLEMENTER, ADVERSARY,  # noqa: E402
                     JUDGE, PRIORITIES, PLANNER, PLANNER_SERVES, DECOMPOSER, persona_text,
                     REPAIR)
from task_rules import (PRIORITY_HEADING, NUMBERED, GOAL_ITEM_WEIGHT,  # noqa: E402
                        goal_items, goal_item_weight,
                        STATIC_REQUEST_ESTIMATES, estimate_requests,
                        TASK_STAGES, NAMES_A_PATH, NAMES_A_SYMBOL, BROAD_ASK,
                        HARNESS_STAGES, HARNESS_NOTE, MODEL_NOTE,
                        scope_gap, failure_class, FAILING, first_failing_line)
from context_builder import (CODE_PATH, GAME_DIR, DEFINITION,  # noqa: E402
                             numbered, outline, task_text, named_paths,
                             kin_text as _kin_text, covering_tests as _covering_tests,
                             context_pack as _context_pack, attempt_lines as _attempt_lines)
from verification import (ASSERT_CALL, GENERIC_DIR, assert_counts, identifiers,  # noqa: E402
                          deleted_paths, test_hunks, names_deleted, names_its_source,
                          weakened_tests, last_error)

try:  # verification resolves against the same module identity as the daemon
    from . import verification
except ImportError:  # pragma: no cover - exercised by bridge.py / direct swarmd.py execution
    import verification
from provisioning import (RUNTIME_MARKERS, PROV_RECIPES, embedded_repos, _subdirs, sub_projects,  # noqa: E402
                          test_cmd_hint, test_cmds, project_test_cmd, other_test_cmds,
                          why_unrunnable, probe_surroundings, build_prov_block,
                          detect_test_cmd as _detect_test_cmd,
                          scaffold as _scaffold, configure as _configure,
                          render_surroundings as _render_surroundings)



# ------------------------------------------------------------------ P2 compat wrappers
def _git_run(args, cwd):
    """swarmd.git returns (rc, out) as a tuple; the extracted modules want the same shape."""
    return git(args, cwd=cwd)


def attempt_lines(task, limit=3, max_chars=1_200):
    """swarmd compatibility: reads attempts under STATE (P2 wrapper)."""
    return _attempt_lines(task, STATE, limit=limit, max_chars=max_chars)


def kin_text(task, q):
    """swarmd compatibility: reads the queue's done rows via _read (P2 wrapper)."""
    return _kin_text(task, q, read_done=_read)


def covering_tests(wd, terms, limit=2):
    """swarmd compatibility: git subprocess via the daemon's git() (P2 wrapper)."""
    return _covering_tests(wd, terms, limit=limit, git_run=_git_run)


def context_pack(task, wd, limit=20_000, also=""):
    """swarmd compatibility: git subprocess via the daemon's git() (P2 wrapper)."""
    return _context_pack(task, wd, limit=limit, also=also, git_run=_git_run)


def detect_test_cmd(repo):
    """swarmd compatibility: log() wired (P2 wrapper)."""
    return _detect_test_cmd(repo, log=log)


def configure(repo, test_cmd=None):
    """swarmd compatibility: config I/O and logging wired (P2 wrapper)."""
    return _configure(repo, test_cmd=test_cmd, load_cfg=load_cfg, save_cfg=save_cfg,
                      python_for=python_for, log=log)


def scaffold(path, goal):
    """swarmd compatibility: git identity wired (P2 wrapper)."""
    return _scaffold(path, goal, git_ident=GIT_IDENT)


def render_surroundings(repo, slug, s, config_path, existing=None):
    """swarmd compatibility: branch detection via git_branch (P2 wrapper)."""
    return _render_surroundings(repo, slug, s, config_path, existing=existing,
                                git_branch=git_branch)


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
        "packet", "allowed_paths", "verification_commands", "request_class", "must_run_first")
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
_turns = TurnBook(lock=_study_lock)   # per-worker turn accounting; the RuntimeContext carries this same book
_study_calls = _turns.study_calls
_edit_calls = _turns.edit_calls
_request_calls = _turns.request_calls
_charges = _turns.charges
_held = {}            # worker -> when it last said it was waiting for the allowance
_waiting = {}         # worker -> why it is not working, or absent while it is
_worker_clocks = {}   # worker -> its BucketClock for wall-time buckets (§13)
_ran_on = _turns.ran_on
_active_turns = _turns.active_turns
_turn_logs = _turns.turn_logs
_run = {}
_run_lock = threading.RLock()
_progress_seen = {}
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
    "validation_commands", "test_cmd", "keep_worktrees", "scope_gate", "idle_improvement", "auto_plan",
    "idle_improvement_cooldown", "max_review_formats", "task_request_cap", "run_request_cap",
    "max_role_calls", "shutdown_on_quota_exhausted"})
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
    """Queue, lessons, logs and worktrees are kept per target repository.

    Builds and returns a ``RuntimeContext`` for this repository and continues to populate
    the module compatibility globals (``STATE``/``LOGS``/``SLUG``) so every existing
    caller and test keeps reading the same paths it always did. New modules take the
    returned context explicitly; the globals are retired one caller at a time in later
    packets (P5, DECOMPOSE-SWARMD.md).

    Race-safety: every path is computed into locals first and the context is built from
    those locals before the compatibility globals are assigned, so ``_build_context``
    never re-reads the globals and two simultaneous ``use_repo`` calls cannot return
    contexts with mixed paths.
    """
    global STATE, LOGS, SLUG
    slug = repo_slug(c["repo"])
    state = HERE / "state" / slug
    logs = HERE / "logs" / slug
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    context = _build_context(c, slug, state, logs)
    SLUG, STATE, LOGS = slug, state, logs
    return context


def _build_context(c, slug, state, logs):
    """Assemble the per-repository RuntimeContext for a config dict and pre-computed
    repo paths.

    Takes ``slug``/``state``/``logs`` explicitly (never the module globals) so the
    returned context is exactly the repository ``use_repo`` was called for, even when
    another thread rebinds the globals between the call and the build. P3 intentionally
    stops at construction: no attempt runs on it yet, so it does not re-read or own the
    mutable config, and it does not apply a saved ``run.json``.
    """
    run_state = RunState()
    stop = threading.Event()
    paths = RepoPaths(
        repo=Path(c["repo"]).expanduser().resolve(),
        state=state, logs=logs, worktrees=HERE / "wt" / slug,
        config=config_path_for(c["repo"]), slug=slug)
    return RuntimeContext(
        paths=paths,
        config=ConfigSnapshot(values=dict(c), mtime=config_mtime()),
        events=EventSink(paths, run_state),
        run_state=run_state,
        processes=ProcessRegistry(stop=stop),
        stop=stop,
        turns=_turns)


def wt_root():
    return HERE / "wt" / SLUG


def pool(c, role=None):
    return agent_turn.pool(c, role, default_model=DEFAULT_MODEL)


def paid_pool(c):
    return agent_turn.paid_pool(c)


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
    return agent_turn.paid_stand_in(c, model, paid_pool=paid_pool, spend_left=spend_left)


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


def quota_mode(c):
    return agent_turn.quota_mode(c)


def quota_exhausted(c, budget):
    return agent_turn.quota_exhausted(c, budget, quota_mode=quota_mode,
                                      paid_pool=paid_pool, paid_stand_in=paid_stand_in)


def trunk_name(c):
    return workspace.trunk_name(c)


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
# The direct files of swarm/ plus the two repo-root entry points are the harness. Trees like
# state/ and wt/ are generated (and wt/ holds 43k worktree *.py files); pruning them at walk
# time keeps the per-check fingerprint ~1 ms instead of ~2.7 s while watching everything else.
WATCH_SKIP = ("state", "wt", "__pycache__", ".venv", ".sessions",
              ".pytest_cache", ".albatross")


def source_files():
    out = {ROOT / "flint.py", ROOT / "nonstop.py"}
    for dirpath, dirnames, filenames in os.walk(HERE):
        dirnames[:] = [d for d in dirnames if d not in WATCH_SKIP]
        for name in filenames:
            if name.endswith(".py"):
                out.add(Path(dirpath) / name)
    return sorted(out)


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
    return supervisor.drain(tally, stop, run=_run, run_lock=_run_lock, journal=journal,
                            log=log, drain_reason=_drain_reason)


def sh(cmd, cwd=None, timeout=900, env=None):
    return workspace.sh(cmd, cwd=cwd, timeout=timeout, env=env,
                        process=process, kill_group=kill_group)


def git(args, cwd, check=False):
    return workspace.git(args, cwd, check=check, sh=sh, ident=GIT_IDENT)


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
    return JsonlTaskStore.read(path)


def _write(path, rows):
    JsonlTaskStore.write(path, rows)


class Queue(TaskQueue):
    """Compatibility constructor bound to the active repository context."""

    def __init__(self, max_depth=2, max_queue=20, max_children=3, max_descendants=12):
        store = JsonlTaskStore(
            STATE / "queue.jsonl", STATE / "done.jsonl", STATE / "queue.lock")
        super().__init__(
            store, TaskPolicy(max_depth=max_depth, max_attempts=MAX_ATTEMPTS),
            max_depth=max_depth, max_queue=max_queue,
            max_children=max_children, max_descendants=max_descendants,
            run_state=_run, run_lock=_run_lock)


def queue_for(c):
    """Build a queue with every recursion and capacity limit from this repo's config."""
    return Queue(c.get("max_depth", 1), c.get("max_queue", 20),
                 c.get("max_children", 3), c.get("max_descendants", 12))


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
    return workspace.retained_diff(c, attempt, git=git)


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


def rest(ledger, e):
    return agent_turn.rest(ledger, e, config=CONFIG)


def next_wake(ledger, c):
    return agent_turn.next_wake(ledger, c, pool=pool)


def all_resting(ledger, c):
    return agent_turn.all_resting(ledger, c, next_wake=next_wake)


def read_charges(path, role, worker, model):
    """Bank what a finished turn was charged, one `charge` row each, and return the total."""
    return agent_turn.read_charges(path, role, worker, model,
                                   journal=journal, count_usd=count_usd)


def route_to_paid(c, role, worker, model, why):
    """Swap in a paid stand-in for this turn, or None when none is reachable or affordable."""
    return agent_turn.route_to_paid(
        c, role, worker, model, why,
        paid_stand_in=paid_stand_in, spend_left=spend_left, spend_phrase=spend_phrase,
        spend_used=spend_used, spend_window=spend_window, log=log, journal=journal,
        ran_on=_ran_on)


def _turn_deps():
    """The daemon services a turn uses, read now so a test's patch of any of them bites."""
    return agent_turn.TurnDeps(
        pool=pool, default_model=DEFAULT_MODEL, turns=_turns, stop=_stop,
        route_to_paid=route_to_paid, recheck_allowance=recheck_allowance,
        quota_exhausted=quota_exhausted, quota_mode=quota_mode, shutdown=shutdown,
        log=log, journal=journal, waiting=waiting, worker_publish=worker_publish,
        allowance_blocked=ALLOWANCE_BLOCKED, held=_held, read_only_roles=READ_ONLY_ROLES,
        no_corpus=NO_CORPUS, spend_file=spend_file, spend_cap=spend_cap,
        spend_reset_day=spend_reset_day, logs=LOGS, root=ROOT, sandboxed=sandboxed,
        turn_timeout=turn_timeout, process=process, kill_group=kill_group,
        count_requests=count_requests, count_study=count_study, count_edits=count_edits,
        read_charges=read_charges, request_log=RequestLog)


def flint(prompt, cwd, c, role, worker, budget, max_steps, model=None):
    """One headless flint turn, paced against the daily allowance.

    The turn may end up on a paid stand-in rather than the model asked for, so it records
    what actually ran in `_ran_on` for the caller to credit. Stale entries are cleared
    first: a turn that runs on the model it was given must not inherit the last one's.
    """
    return agent_turn.AgentTurnRunner(_turn_deps()).run(
        prompt, cwd, c, role, worker, budget, max_steps, model).text


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
    return reporting.waiting(worker, why, books=_waiting)


def worker_publish(worker, state):
    return reporting.worker_publish(worker, state, clocks=_worker_clocks)


def forget_worker(worker):
    return reporting.forget_worker(worker, turns=_turns, waiting=_waiting, held=_held)


def live_workers(workers):
    return reporting.live_workers(workers)


def count_study(worker, n=0, reset=False):
    """Study-tool calls made by one worker's turns since its attempt began."""
    return agent_turn.count_study(_turns, worker, n, reset)


def count_usd(worker, usd=0.0, reset=False):
    """What one worker's turns have been charged since its attempt began."""
    return agent_turn.count_usd(_turns, worker, usd, reset)


def count_edits(worker, n=0, reset=False):
    """Edit calls one worker's turns made since its attempt began.

    An attempt that ends with no diff means two different things. A model that called edit_file
    and still changed nothing got it wrong; a model that never reached an edit ran out of the
    rounds this harness gave it. Only the first is the model's failure."""
    return agent_turn.count_edits(_turns, worker, n, reset)


def count_requests(worker, n=0, reset=False):
    """API request starts attributable to this worker's current task."""
    return agent_turn.count_requests(_turns, worker, n, reset, run=_run, run_lock=_run_lock)


def mit_arm(c, rng=random):
    return agent_turn.mit_arm(c, rng)

# ------------------------------------------------------------------ flint

def turn_timeout(c, role):
    return c.get("role_timeouts", {}).get(role, c.get("turn_timeout", 1800))


# ------------------------------------------------------------------ gate

def baseline_key(c, commit):
    return verification.baseline_key(c, commit)


def baseline_cache(key, passed=None, ttl=86_400):
    return verification.baseline_cache(key, passed=passed, ttl=ttl, state=STATE)


def run_gate(cwd, c):
    return verification.run_gate(cwd, c, sandboxed=sandboxed, sh=sh, clean_env=clean_env)


# ------------------------------------------------------------------ trunk

@contextmanager
def trunk_locked():
    with workspace.trunk_locked(thread_lock=_trunk_lock, state=STATE):
        yield


# ------------------------------------------------------------------ provisioning
#
# A fresh git worktree contains exactly the files git tracks: no node_modules/, no .venv,
# no vendored deps, no nested-repo content. A test gate that needs any of those fails on
# a bare checkout before a single model request, which is how the dj-bot swarm spent its
# whole shift red-baselined with "Cannot find package '@strudel/core'" and "every task
# would be rejected". The `prov` config block fixes that: it is a list of shell hooks the
# swarm runs inside each fresh worktree (and the _view) before the gate, so a checkout in
# a new codespace or on a new laptop reaches green by itself.


def prov_block(c):
    return verification.prov_block(c)


def run_prov_hooks(c, wd, label, env_extra=None, log_on_ok=False):
    return verification.run_prov_hooks(c, wd, label, env_extra=env_extra, log_on_ok=log_on_ok,
                                       prov_block=prov_block, sh=sh, clean_env=clean_env, log=log)


def run_gate_with_prov(c, wd, label):
    return verification.run_gate_with_prov(c, wd, label,
                                           run_prov_hooks=run_prov_hooks, run_gate=run_gate)


def ensure_trunk(c):
    return workspace.ensure_trunk(c, git=git, locked=trunk_locked, log=log)


def checked_out_at(c, branch):
    return workspace.checked_out_at(c, branch, git=git)


def refresh_view(c):
    return workspace.refresh_view(c, git=git, ensure_trunk=ensure_trunk, worktrees=wt_root)


def refresh_view_prov(c):
    return workspace.refresh_view_prov(c, refresh_view=refresh_view, prov_block=prov_block,
                                        run_prov_hooks=run_prov_hooks, log=log)


def sync_refresh_view(c):
    return workspace.sync_refresh_view(c, refresh_view=refresh_view)


def sync_trunk(c):
    return workspace.sync_trunk(c, git=git, ensure_trunk=ensure_trunk, locked=trunk_locked,
                                worktrees=wt_root, run_gate_with_prov=run_gate_with_prov,
                                checked_out_at=checked_out_at, log=log, sync_failed=_sync_failed)


# ------------------------------------------------------------------ worker


def turn_round(worker):
    return reporting.turn_round(worker, turn_logs=_turn_logs)


def write_now(workers, c=None):
    return reporting.write_now(workers, c, state=STATE, ran_on=_ran_on, run=_run,
                               waiting_books=_waiting, pool=pool, live_workers=live_workers,
                               turn_round=turn_round, run_status=run_status)


def mark_progress(kind):
    return reporting.mark_progress(kind, run=_run, run_lock=_run_lock)


def run_status(workers, c):
    return reporting.run_status(workers, c, state_dir=STATE, progress_seen=_progress_seen,
                                read=_read, run=_run, run_lock=_run_lock, stop=_stop,
                                turn_logs=_turn_logs, mark_progress=mark_progress,
                                queue_for=queue_for, read_roadmap=read_roadmap)


TRAILER = reporting.TRAILER


def promotion_state(c):
    return reporting.promotion_state(c, git=git, trunk_name=trunk_name)


def landed_commits(c, limit=20):
    return reporting.landed_commits(c, limit, state=STATE, read=_read, git=git, trunk_name=trunk_name)


def health(hours=24):
    return reporting.health(hours, state=STATE, read=_read)


def read_now(max_age=180):
    return reporting.read_now(max_age, state=STATE)


def report_progress(workers, said, every=180):
    return reporting.report_progress(workers, said, every,
                                     active_turns=_active_turns, waiting_books=_waiting, log=log)


Tally = reporting.Tally


def _attempt_deps():
    """The daemon services an attempt uses, read now so a test's patch of any of them bites."""
    return attempt_runner.AttemptDeps(
        log=log, journal=journal, notify=notify, git=git, trunk_name=trunk_name,
        state=STATE,
        flint=flint, rest=rest, turn_timeout=turn_timeout, count_requests=count_requests,
        pool=pool, refresh_view=refresh_view, read_goal=read_goal,
        read_only_roles=READ_ONLY_ROLES, kinds=KINDS,
        ran_on=_ran_on, run=_run, view_lock=_view_lock,
        run_lock=_run_lock, attempt_lines=attempt_lines, baseline_cache=baseline_cache,
        baseline_key=baseline_key, context_pack=context_pack, count_edits=count_edits,
        kin_text=kin_text, mark_progress=mark_progress, mit_arm=mit_arm, study=study,
        checked_out_at=checked_out_at, trunk_locked=trunk_locked,
        count_study=count_study, count_usd=count_usd, forget_worker=forget_worker,
        paid_pool=paid_pool, spend_left=spend_left, ensure_trunk=ensure_trunk,
        wt_root=wt_root, take_resume=take_resume,
        run_gate=run_gate, run_prov_hooks=run_prov_hooks)


def _worker_deps():
    """The daemon services the worker loop uses, read now so a test's patch of any of them bites."""
    return worker_loop.WorkerDeps(
        log=log, journal=journal, watch_config=watch_config, read_goal=read_goal,
        recheck_allowance=recheck_allowance, allowance_recheck=allowance_recheck,
        next_wake=next_wake, all_resting=all_resting, describe_error=describe_error,
        forget_worker=forget_worker, config_mtime=config_mtime, cfg_lock=_cfg_lock,
        run=_run, new_ledger=lambda: Ledger(STATE / "learn.json"), new_tally=Tally,
        clocks=_worker_clocks)


class Worker(worker_loop.Worker):
    """The worker loop bound to this daemon's services and attempt runner."""

    def services(self):
        return _worker_deps()

    def runner(self):
        return attempt_runner.AttemptRunner(self, _attempt_deps())


# ------------------------------------------------------------------ planning

def serving_the_goal(tasks, items):
    return planning.serving_the_goal(tasks, items)


def plan(c, q, budget, n=6, ledger=None):
    return planning.plan(c, q, budget, n, ledger, kinds=KINDS, state=STATE,
                         view_lock=_view_lock, all_resting=all_resting,
                         ensure_trunk=ensure_trunk, flint=flint, git=git, journal=journal,
                         log=log, pool=pool, read_goal=read_goal, refresh_view=refresh_view,
                         study=study, trunk_name=trunk_name)


def json_object(text):
    return planning.json_object(text)


# ------------------------------------------------------------------ spare time

IDLE_MIN_SECONDS = planning.IDLE_MIN_SECONDS
IDLE_IMPROVEMENT = planning.IDLE_IMPROVEMENT


def idle_state():
    return planning.idle_state(state_dir=STATE)


def write_idle(state):
    return planning.write_idle(state, state_dir=STATE)


def settle_idle(state, q):
    return planning.settle_idle(state, q, read=_read, log=log)


def idle_ready(c, q, hours=None):
    return planning.idle_ready(c, q, hours, hours_left=hours_left,
                               read_roadmap=read_roadmap)


def idle_improvement(c, q, budget, ledger=None, hours=None):
    return planning.idle_improvement(c, q, budget, ledger, hours, kinds=KINDS,
                                     state_dir=STATE, view_lock=_view_lock,
                                     can_take_a_turn=can_take_a_turn, flint=flint, git=git,
                                     journal=journal, log=log, pool=pool,
                                     read_goal=read_goal, refresh_view=refresh_view,
                                     trunk_name=trunk_name, idle_state=idle_state,
                                     write_idle=write_idle, settle_idle=settle_idle,
                                     idle_ready=idle_ready)


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
    return supervisor.allowance_recheck(c)


def resync_allowance(c, budget):
    return supervisor.resync_allowance(c, budget, account=account, journal=journal, log=log)


def recheck_allowance(c, budget):
    return supervisor.recheck_allowance(
        c, budget, recheck_lock=_recheck_lock, rechecked=_rechecked, account=account,
        journal=journal, log=log, allowance_recheck=allowance_recheck)


def free_tool_models():
    key = os.environ.get("OPENROUTER_API_KEY", "")
    sys.path.insert(0, str(ROOT))
    import flint as F
    models = F.openrouter_get("/models", key, timeout=20)["data"]
    return {m["id"]: m for m in models if m["id"].endswith(":free")
            and "tools" in (m.get("supported_parameters") or [])}


# ------------------------------------------------------------------ setup

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
    "scope_gate", "idle_improvement", "idle_improvement_cooldown", "max_review_formats",
    "task_request_cap", "run_request_cap", "max_role_calls",
    "keep_attempts", "execution_class", "auto_plan", "prov", "shutdown_on_quota_exhausted",
    "_comment"})
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
        view = refresh_view_prov(c)
        ok, output = run_gate_with_prov(c, view, "view")
    baseline_cache(key, passed=ok)
    if not ok:
        if "provisioning failed:" in output:
            sys.exit("provisioning failed before the gate could run. `swarm init <repo>` can "
                     "write the hooks this repository needs; `swarm init --check <repo>` shows "
                     f"what it would run.\n{output[-1500:]}")
        sys.exit(f"`{c['test_cmd']}` fails on {trunk_name(c)} before any work, so every task "
                 f"would be rejected. Fix the tests or pass --test-cmd."
                 f"{why_unrunnable(c['test_cmd'])}{other_test_cmds(c['repo'], c['test_cmd'])}"
                 f"\n{output[-1500:]}")
    log(f"baseline green on {trunk_name(c)}")


def keep_awake():
    return supervisor.keep_awake(log=log)


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
    return supervisor.holder(daemon_note=daemon_note)


# What holds a roadmap packet, and the one thing that would release it. A blocker with no
# stated next action is the same as no blocker at all: the run stalls and the report says
# only that it stalled.
_roadmap_said = {}      # the last set of root blockers logged, so a stable stall says so once


def retry_recorded(evidence, since):
    return reporting.retry_recorded(evidence, since)


# ------------------------------------------------------------------ roadmap

def dispatch_roadmap(c, q):
    return roadmap.dispatch_roadmap(c, q, state=STATE, read=_read, git=git, journal=journal,
                                    log=log, retry_recorded=retry_recorded,
                                    trunk_name=trunk_name, said=_roadmap_said)


def attempts_for(tid):
    return reporting.attempts_for(tid, state=STATE)


def prune_attempts(keep=300):
    return reporting.prune_attempts(keep, state=STATE, log=log)


def read_roadmap():
    return roadmap.read_roadmap(state=STATE)


def hours_left(hours):
    return supervisor.hours_left(hours)


def _supervisor_deps():
    """The daemon services the lifecycle uses, read now so a test's patch of any of them bites."""
    return supervisor.SupervisorDeps(
        log=log, journal=journal, account=account, preflight=preflight,
        read_goal=read_goal, run_status=run_status, shutdown=shutdown,
        holder=holder, hours_left=hours_left, keep_awake=keep_awake,
        run_daemon=run_daemon, queue_for=queue_for, note_interrupted=note_interrupted,
        prune_attempts=prune_attempts, trunk_name=trunk_name, config_kind=config_kind,
        resync_allowance=resync_allowance, pool=pool, config_mtime=config_mtime,
        source_fingerprint=source_fingerprint, source_changed=source_changed,
        restart_into_new_code=restart_into_new_code, drain=drain,
        dispatch_roadmap=dispatch_roadmap, quota_exhausted=quota_exhausted,
        quota_mode=quota_mode, sync_usage=sync_usage, import_bugs=import_bugs,
        report_progress=report_progress, write_now=write_now,
        can_take_a_turn=can_take_a_turn, sync_trunk=sync_trunk, plan=plan,
        rest=rest, idle_improvement=idle_improvement, read=_read,
        worker_cls=Worker, tally_cls=Tally, state=STATE, config=CONFIG,
        stop=_stop, run=_run, run_lock=_run_lock, cfg_seen=_cfg_seen,
        drain_reason=_drain_reason)


def start(c, hours=None, max_tasks=None, awake=False):
    return supervisor.start(c, hours, max_tasks, awake, d=_supervisor_deps())


def run_daemon(c, max_tasks=None, hours=None):
    return supervisor.run_daemon(c, max_tasks, hours, d=_supervisor_deps())


# ------------------------------------------------------------------ report

def build_report(c, hours=24):
    return reporting.build_report(c, hours, state=STATE, read=_read, git=git, trunk_name=trunk_name,
                                  promotion_state=promotion_state, reportable=reportable)


# ------------------------------------------------------------------ commands

def _setup(a=None):
    if a is not None and getattr(a, "repo", None):
        use_config(a.repo)
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


# ------------------------------------------------------------------ surroundings (swarm init)

def cmd_init(a):
    """Write a tuned config for a repository, or report on one that already exists.

    `swarm init <repo>` is the idempotent door into a repository for the first time: it runs
    the same detection the harness uses, names nested repositories and runtime deps, writes
    swarm/configs/<slug>.json with a `prov` block that a bare checkout can run, and then
    verifies the gate actually turns green once provisioned — failing loudly before any model
    work instead of waiting for a mid-shift red baseline. `--check` (or `-n`) prints the
    same surroundings without writing a config.
    """
    if a.repo:
        repo = Path(a.repo).expanduser().resolve()
    elif (HERE / "config.json").exists() and load_cfg().get("repo"):
        repo = Path(load_cfg()["repo"]).expanduser().resolve()
    else:
        sys.exit("no repository given: `swarm init <repo>` or `swarm init --check <repo>`")
    slug = repo_slug(repo)
    config_path = per_repo_config(repo)
    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        sys.exit(f"{repo} needs a Git repository with at least one commit.\n  git init && git add -A && git commit -m baseline")
    rc, _ = git(["rev-parse", "--verify", "-q", "HEAD"], cwd=str(repo))
    if rc != 0:
        sys.exit(f"{repo} needs at least one commit before a swarm can run on it.")

    surroundings = probe_surroundings(repo)
    if a.check:
        print(render_surroundings(repo, slug, surroundings, config_path))
        return

    existing = json.loads(config_path.read_text()) if config_path.is_file() else None
    if existing is not None and existing.get("repo") == str(repo):
        # Idempotent: never overwrite a tuned file someone has edited.
        print(render_surroundings(repo, slug, surroundings, config_path, existing=existing))
        if existing.get("prov", {}).get("hooks"):
            print("provisioning hooks already configured; nothing to write.")
        return

    test_cmd = detect_test_cmd(repo) or "python3 -m unittest discover -s tests -t . -q"
    # The interpreter the swarm's own flint turns use; fall back to the running one rather
    # than resolving a config that may not exist yet in a fresh checkout.
    py = (HERE / ".venv" / "bin" / "python") if (HERE / ".venv" / "bin" / "python").is_file() else sys.executable
    python = str(py)
    c = {
        "repo": str(repo),
        "base_branch": git_branch(repo),
        "trunk": "swarm/trunk",
        "goal_file": "GOAL.md",
        "test_cmd": test_cmd,
        "python": python,
        "workers": 1,
        "daily_cap": 800,
        "reserve": 200,
        "prov": build_prov_block(surroundings, test_cmd),
        "_comment": "Written by `swarm init`. Re-run `swarm init <repo>` to refresh the surroundings; edit the test gate, models or prov hooks freely.",
    }
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(c, indent=2) + "\n")
    tmp.replace(config_path)
    print(render_surroundings(repo, slug, surroundings, config_path))
    print(f"wrote {config_path}")
    print("Next: `swarm grind <repo> --goal '...'` — the daemon provisions and verifies before any task.")


def git_branch(repo):
    rc, out = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo))
    return out.strip() if rc == 0 and out.strip() and out.strip() != "HEAD" else "main"


def cmd_run(a):
    if a.hours <= 0:
        sys.exit("--hours must be positive")
    start(_setup(a), hours=a.hours, max_tasks=a.max_tasks)


def cmd_status(a):
    c = _setup(a)
    b = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
               owner_window=c.get("owner_window", ["00:00", "00:00"]))
    snap = b.snapshot()
    ok, wait, why = b.check()
    q = queue_for(c)
    pend = q.pending()
    done = _read(q.done)
    _, ahead = git(["rev-list", "--count", f"{c.get('base_branch', 'main')}..{trunk_name(c)}"], cwd=c["repo"])
    note = daemon_note()
    journal_rows = _read(STATE / "journal.jsonl")[-12:]
    errs = [t for t in journal_rows if t.get("event") == "error"]
    claimed = sum(1 for t in pend if t.get("claimed"))
    landed = sum(1 for t in done if t.get("status") == "done")
    parked = sum(1 for t in done if t.get("status") == "parked")
    remaining = spend_left(c)
    # Human-first summary: the one glance that tells you what is happening. The JSON
    # below is the machine contract (the extension and scripts parse it); this block
    # is for the person at the terminal.
    print(f"repo     {c['repo']}")
    if note:
        print(f"daemon   running (pid {note['pid']}, since "
              f"{dt.datetime.fromtimestamp(note['started']).strftime('%H:%M')}, "
              f"goal: {str(note.get('goal', ''))[:60] or '—'})")
    else:
        print("daemon   stopped")
    if ok:
        print(f"budget   free requests available today ({snap.get('status', 'ok')})")
    else:
        print(f"budget   blocked: {why} (resumes in ~{wait / 60:.0f}m)")
    if remaining is not None:
        print(f"spend    ${remaining:.4f} of ${spend_cap(c)} left this {spend_window(c)} window")
    print(f"queue    {len(pend)} pending ({claimed} claimed)")
    print(f"trunk    {ahead} commit(s) ahead of {c.get('base_branch', 'main')}")
    print(f"landed   {landed} done, {parked} parked")
    sw = swallow.summary()
    if sw:
        last = sw[0]
        print(f"exceptions {sum(g['count'] for g in sw)} swallowed (latest: {last['site']} — "
              f"see `swarm exceptions`)")
    if errs:
        last = errs[-1]
        print(f"⚠ last error ({last.get('iso', '?')}): {str(last.get('tb') or last.get('note') or '')[:120]}")
    print()
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
                  "remaining_usd": (round(remaining, 6) if remaining is not None else None),
                  "paid_fallback": paid_pool(c)},
        "pacing": {"allowed_now": ok, "wait_s": round(wait), "reason": why,
                   "allowance_recheck_s": allowance_recheck(c)},
        "queue": {"pending": len(pend), "claimed": claimed},
        "landed": landed,
        "split": sum(1 for t in done if t.get("status") == "split"),
        "parked": parked,
        "promotion": promotion_state(c),
        "top_arms": Ledger(STATE / "learn.json").leaderboard()[:8],
        "resting": [{"model": m, "until": dt.datetime.fromtimestamp(u).isoformat(timespec="seconds"),
                     "reason": why} for m, u, why in Ledger(STATE / "learn.json").resting()],
    }, indent=2))
    for t in journal_rows:
        print(f"  {t['iso']}  {t['event']:<8} {t.get('stage') or t.get('role') or ''} "
              f"{t.get('title') or t.get('model') or ''}")


def cmd_report(a):
    print(build_report(_setup(a), a.hours))


def cmd_add(a):
    c = _setup(a)
    q = queue_for(c)
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
    c = _setup(a)
    b = Budget(cap=c.get("daily_cap"), reserve=c.get("reserve", 10),
               owner_window=c.get("owner_window", ["00:00", "00:00"]))
    plan(c, queue_for(c), b, a.n)


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


def cmd_exceptions(a):
    """Surface exceptions the harness chose to swallow: the quiet fallbacks in support
    modules (recorded by swarm/swallow.py) plus the daemon's journaled task errors.

    The recorder file is the daemon's own bookkeeping (swarm/state/swallowed.jsonl);
    the journal is the existing `journal("error", ...)` stream. Together they turn
    "worked yesterday, not today, and nothing in any log said why" into a table.
    """
    _setup()
    groups = swallow.summary()
    journal_errs = [t for t in _read(STATE / "journal.jsonl") if t.get("event") == "error"]
    print(f"swallowed-exception recorder: {sum(g['count'] for g in groups)} recorded"
          f" across {len(groups)} site(s)" if groups else "no swallowed exceptions recorded")
    for g in groups:
        print(f"  {g['count']:>3}×  {g['site']:<40} last {g['last_iso']}  {g['last_msg'][:80]}")
    if journal_errs:
        print(f"\ndaemon journal: {len(journal_errs)} error event(s) in {STATE / 'journal.jsonl'}")
        for t in journal_errs[-8:]:
            print(f"  {t.get('iso', '?')}  {t.get('kind', '') or t.get('id', '')}: "
                  f"{str(t.get('err') or '')[:90]}")
    return 0


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
    run.add_argument("--repo", help="target Git repository (default: configured one)")
    run.add_argument("--hours", type=float, default=24, help="stop after this many hours (default 24)")
    run.add_argument("--workers", type=int)
    run.add_argument("--max-tasks", type=int)
    run.set_defaults(fn=cmd_run)
    init = sub.add_parser("init", help="write a tuned config + prov hooks for a repository (see --check)")
    init.add_argument("repo", nargs="?", help="target Git repository")
    init.add_argument("-n", "--check", action="store_true",
                      help="print the repository's surroundings (nested repos, runtime deps, "
                           "venv, node_modules) without writing a config")
    init.set_defaults(fn=cmd_init)
    st = sub.add_parser("status", help="what is running and where")
    st.add_argument("--repo", help="target Git repository (default: configured one)")
    st.set_defaults(fn=cmd_status)
    r = sub.add_parser("report", help="what landed, breakthroughs, leaderboard, playbook")
    r.add_argument("--repo", help="target Git repository (default: configured one)")
    r.add_argument("--hours", type=float, default=24)
    r.set_defaults(fn=cmd_report)
    p = sub.add_parser("add")
    p.add_argument("--repo", help="target Git repository (default: configured one)")
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
    p2.add_argument("--repo", help="target Git repository (default: configured one)")
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
    ex = sub.add_parser("exceptions",
                        help="show exceptions the harness swallowed (swallow.py recorder + journal errors)")
    ex.set_defaults(fn=cmd_exceptions)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
