# Work order: decompose `swarm/swarmd.py`

**Audience:** a headless coding agent (DeepSeek) with `file_read`, `grep`, `list_dir`,
`file_edit`, `file_write`, `shell`, `update_plan`, `task`.
**Baseline commit:** `8cc415a` — every line number below was measured at that commit.
**Repo root:** `/Users/devonodell/Desktop/OpenRouterSwarm`

## How to use this document

Do **one packet per session**. Each packet below is self-contained: it names the files it
touches, the steps, the acceptance check, and the commit message. Read only the packet you
are executing plus the four reference sections (Ground truth, Landmines, Contracts,
Invariants). Do not read ahead — the later packets depend on earlier ones existing and will
mislead you if you try to anticipate them.

Rules for every packet, without exception:

1. **One packet per commit.** Never combine an extraction with a behavior change.
2. **Run the suite before you start and after you finish.** Both must give the same result.
   ```sh
   cd /Users/devonodell/Desktop/OpenRouterSwarm && .venv/bin/python -m pytest tests/ -q
   ```
   Use `.venv/bin/python`. The system `python3` has no pytest installed.
   Baseline verification at `8cc415a` passed outside the sandbox: **828 passed, 29 subtests
   passed**. A sandboxed run produced seven environmental failures: five daemon-control tests
   require process inspection/signalling, and two local-model tests require binding localhost
   sockets. Those seven failures disappeared in the unrestricted run.
3. **If the suite is not green before you start, stop and report.** Do not refactor on top of
   a red suite — you will not be able to attribute the failure.
4. **If a packet's acceptance check fails, revert the packet.** Do not patch forward.
   `git checkout -- .` and report what happened.
5. **Report real command output, never a remembered number.** When a packet asks you to
   verify something, paste the actual terminal output of the command you ran. Do not assert
   that a count "matches the baseline" unless you ran both and can show both. This document
   records no expected pass counts, so any claim of matching one is a fabrication.
6. Stay inside the packet's declared file list. If the work seems to require touching a file
   outside that list, stop and report — that is a sign the packet was mis-scoped, not a
   licence to widen it.

Packets are numbered `P0`–`P14`. **Do not call them "phases."** In this repo
`tests/test_phase2_modules.py` already means something unrelated (receipts, hardware,
routing, undo, hooks, extensions). Using "Phase 2" for refactor work will collide with it.

---

## Objective

`swarm/swarmd.py` is 6,212 lines holding several distinct systems wired together through
mutable module globals. Turn it into a modular, distribution-ready daemon with explicit
boundaries.

This is a **pure architectural refactor**. It must not change:

- swarm behavior
- state file formats (`queue.jsonl`, `done.jsonl`, `journal.jsonl`, `run.json`, `learn.json`)
- CLI contracts or their JSON output
- bridge JSON output
- attempt artifact layout under `state/<slug>/attempts/`
- the landing guarantees listed in **Invariants**

Do **not** turn this into networked microservices. Build a modular monolith first: one daemon
per repository, same filesystem, same worktrees, same subprocess boundary around `flint.py`.
Once the boundaries are stable, moving workers to other processes or machines becomes an
infrastructure change rather than another rewrite. Distribution notes are at the end, for
later, and are not part of this work order.

---

## Ground truth (verified at `8cc415a`)

### Sizes

| Component | Lines | Location |
|---|---|---|
| `swarmd.py` total | 6,212 | — |
| `Worker` class | ~1,012 | 3081–4092, 28 methods |
| `Queue` class | ~510 | 812–1321, 23 methods |
| `Worker._attempt` | ~223 | 3500–3722 |
| `flint()` | ~212 | 2009–2220 |
| `run_daemon()` | ~169 | 5238–5406 |
| `bridge.py` | 1,468 | consumes `swarmd` as a de facto API |

Everything else in `swarm/` is already reasonably sized and should stay: `learn.py` (479),
`providers.py` (370), `corpus_index.py` (309), `workflow.py` (308), `mit_corpus.py` (286),
`metrics.py` (179), `experiment.py` (164), `receipts.py` (157), `extensions.py` (154),
`wallet.py` (148), `hardware.py` (147), `budget.py` (143), `undo.py` (140),
`classify_no_change.py` (140), `hooks.py` (131), `routing.py` (115), `swallow.py` (106),
`pacing.py` (100), `sandbox.py` (83), `budget_requests.py` (48).

### Mutable global state in `swarmd.py`

Repository context, rebound at runtime by `use_repo()` (line 294):

```python
def use_repo(c):
    global STATE, LOGS, SLUG
    SLUG = repo_slug(c["repo"])
    STATE = HERE / "state" / SLUG
    LOGS  = HERE / "logs" / SLUG
```

Every subsystem reads `STATE`/`LOGS`/`SLUG` implicitly. No function in this file can be
understood or tested without knowing which repository was most recently activated.

Runtime state, all module-level (lines 66–90):

```
_print_lock _journal_lock _process_lock _trunk_lock _view_lock
_processes _stop _sync_failed
_study_lock _study_calls _edit_calls _request_calls _charges
_held _waiting _worker_clocks _ran_on _active_turns _turn_logs
_run _run_lock _progress_seen _recheck_lock _rechecked
_drain_reason _roadmap_said
_cfg_lock _cfg_seen
```

Plus `CONFIG`, `STATE`, `LOGS`, `SLUG`, `HERE`, `ROOT`, `MAX_ATTEMPTS`.

Consequence: two independent swarm contexts cannot coexist in one process, and dependencies
are invisible at every call site.

### `Queue.release()` (line 1043) does seven jobs at once

Verified from the body: JSONL persistence (`_read`/`_write`), task state transitions,
retry/backoff policy with cost-aware delays, split/park decisions, per-task model failure
tracking, **run-level circuit breaking via the `_run`/`_run_lock` globals**, and
**`ledger.cool()` calls into the learning ledger**. It also appends to `done.jsonl` directly.

### `Worker._attempt()` (line 3500) owns the whole product lifecycle

Scope validation → worktree → evidence → baseline verification (with cache) → context
construction → model selection → implementation → snapshot → gate → adversarial review →
repair cycles → commit → rebase/integrate → judge → learning.

### `run_daemon()` (line 5238) is both supervisor and scheduler

Worker management, planning, roadmap dispatch, bug imports, idle improvements, allowance
handling, trunk sync, status generation, source-change restarts, shutdown and draining.

### `swarmd` is already an accidental public API

`bridge.py` imports it (line 70) and uses **41 symbols**, including private ones:

```
Budget CONFIG KINDS LOGS NO_CORPUS Queue STATE _read account attempts_for
build_report config_kind config_path_for config_warnings daemon_note failure_class
git health idle_state import_bugs landed_commits load_cfg log monthly paid_pool
per_repo_config pool python_for queue_for quota_mode read_now read_roadmap
reportable save_cfg scope_gap spend_cap spend_left spend_resets spend_used
trunk_name use_repo
```

`_read` and `STATE` are the ones that matter — the bridge reads the daemon's private
persistence layer directly.

**24 of 31 test modules** import `swarmd`:

```
test_attempt_latency test_bug_reports test_circuit_breakers test_config_resolution
test_context_pack test_daemon_control test_dismissed_tasks test_failure_classes
test_goal_priorities test_implementer_rounds test_mit_bridge test_paid_fallback
test_panel_data test_prompt_hygiene test_provisioning test_records
test_recovery_contracts test_reliability test_resume_and_restart test_roadmap_dispatch
test_shift_review test_swarm_learning test_usage_reporting test_weakened_tests
```

They patch module attributes directly (`CONFIG`, `STATE`, `LOGS`, `HERE`). Moving a
definition to a new module **breaks the patch target even if the name is re-exported**,
because the patched attribute and the one the moved code reads are then different objects.

---

## Landmines

Four hazards that will silently corrupt this refactor if you do not handle them. Two of them
are not obvious from reading the code linearly.

### L1 — `swarmd` has two module identities, so every global exists twice

There is **no `swarm/__init__.py`**. `swarm/` is a PEP 420 namespace package. Therefore:

- `tests/` do `from swarm import swarmd` → module object `swarm.swarmd`
- `bridge.py` does `sys.path.insert(0, str(HERE))` then `import swarmd` (line 70) → a
  **second, separate** module object `swarmd`

Both are live in the same process. `STATE`, `LOGS`, `CONFIG`, `_run`, `_stop` exist **twice**,
independently. The tests already know this and work around it:

```python
# tests/test_config_resolution.py:22
# bridge.py runs as a script and imports swarmd as a top-level module, so there are two module
# objects for it in a test process. Patch both or the bridge keeps reading the real checkout.
SWARMDS = (swarmd, bridge.swarmd)
```

`tests/test_bug_reports.py`, `tests/test_daemon_control.py` and `tests/test_mit_bridge.py` do
the same, for four tests total including `tests/test_config_resolution.py` above.

**Implications you must respect:**

- Any claim of the form "the global is X" is false. There are two.
- Re-exporting a name from `swarmd.py` does **not** unify state across the two copies.
- A test that patches only one copy will read the real `swarm/state/` directory and can
  corrupt live state. If you add a test that patches `swarmd`, patch both copies.
- This is a strong argument for the ordering in this document: moving repository state into
  an explicitly passed `RuntimeContext` (P2) **fixes** the duplicate-global problem, because
  there is then no module-level state to duplicate. Do not try to fix it by adding
  `swarm/__init__.py` — that changes import resolution for `bridge.py`, `flint.py` and the
  worktree copies under `swarm/wt/` all at once, and is a behavior change.

### L2 — the restart watcher is hard-coded to 7 files, and must be fixed *first*

```python
def source_files():                                      # line 631
    return sorted({ROOT / "flint.py", ROOT / "nonstop.py",
                   *(HERE / name for name in ("swarmd.py", "budget.py", "learn.py",
                                              "workflow.py", "sandbox.py"))})
```

A running daemon fingerprints these by `(mtime, size)` (`source_fingerprint`, 636), restarts
via `os.execve` when one changes (`restart_into_new_code`, 665), and refuses to restart into
code that does not compile (`source_compiles`, 653).

Nothing else is watched — not `bridge.py`, `providers.py`, `metrics.py`, `pacing.py`,
`routing.py`, `receipts.py`, or anything you are about to create.

**This inverts the ordering given in most plans for this refactor.** The watcher cannot be
fixed "before the daemon relies on extracted modules" as a late step, because the daemon
relies on them from the very first extraction. Concretely: the commit that extracts
`prompts.py` also edits `swarmd.py`, so that one restart works. But **every subsequent edit to
`prompts.py` alone changes no watched file**, so a live daemon under `nonstop.py` keeps running
the prompts it loaded at startup while you believe you changed them. That is the
"half old daemon, half new child process" failure, and it is silent.

So the watcher fix is **P1, before any extraction**.

When generalizing it, you must exclude generated trees or the fingerprint will churn and the
daemon will restart-loop:

- `swarm/state/` — per-slug live state (`queue.jsonl`, `run.json`, `attempts/`, locks)
- `swarm/wt/` — worktrees. These contain full `_view/` copies of the checkout, including
  `_view/swarm/bridge.py`, `_view/tests/*.py`, and generated `tools/swarm_handoff.py` files.
  Their mtimes change constantly while the swarm works.
- `__pycache__/`, `.venv/`, `.sessions/`, `.pytest_cache/`, `.albatross/`

### L3 — config reload races an in-flight attempt

Workers share one mutable config dict. `watch_config` (180) / `load_cfg` (249) / `cfg` (271)
can reload it between one worker's tasks while another worker is mid-attempt, so an attempt
can observe its model pool, spend cap or trunk name change underneath it.

`RELOADABLE` (155) and `RESTART_ONLY` (164) already distinguish which keys may change live.
Fix is an immutable snapshot taken at claim time (P4), not a lock.

### L4 — moving a definition breaks the tests' patch targets

Because 24 test modules patch `swarmd` attributes by name, a plain move plus re-export is
**not** behavior-preserving for the test suite. For each packet, after moving code, grep for
patches of the names you moved and repoint them at the module that now owns them:

```sh
grep -rn "CONFIG\|STATE\|LOGS\|HERE\|_read\|_run\b" tests/ | grep -i "patch\|setattr"
```

---

## Target architecture

One deployable daemon per repository, divided into bounded components. Arrows are the only
permitted direction of dependency.

```
CLI / bridge
      │
      ▼
SwarmApplication  (public facade)
      │
      ▼
Supervisor ───── Planner / Roadmap / Bug ingestion
      │
      ▼
Worker loop
      │
      ▼
AttemptRunner
  ├── AgentTurnRunner + ModelRouter
  ├── GateRunner (verification)
  ├── WorkspaceManager + IntegrationService
  ├── ArtifactStore (evidence)
  └── Learning (existing learn.py)

Every component receives: RuntimeContext + typed repositories + event sink.
```

### Module layout

Flat, cohesive modules under `swarm/`. Not dozens of tiny files.

| Module | Owns |
|---|---|
| `runtime_context.py` | `RepoPaths`, `ConfigSnapshot`, `RunState`, `EventSink`, `ProcessRegistry`, `RuntimeContext`, locks |
| `task_queue.py` | Task persistence, claim/release transitions, dependencies, retry/split/park policy |
| `prompts.py` | Prompt constants and rendering only |
| `context_builder.py` | Named paths, outlines, context packs, prior-attempt summaries |
| `agent_turn.py` | `flint` subprocess contract, routing, handoffs, typed turn errors/results |
| `verification.py` | Gates, baseline cache, weakened-test detection, provisioning-before-gate |
| `workspace.py` | Git commands, worktrees, retained patches, trunk sync, CAS landing |
| `attempt_runner.py` | The implement → gate → review → repair → integrate state machine |
| `worker.py` | Claim a task, invoke `AttemptRunner`, release outcome; thread mechanics only |
| `planning.py` | Planner, decomposition, idle improvement, goal-priority filtering |
| `roadmap.py` | Roadmap dispatch and blocker calculation |
| `supervisor.py` | Daemon lifecycle, workers, reloads, timers, restarts, periodic jobs |
| `reporting.py` | Status snapshots, health, landed commits, reports |
| `provisioning.py` | Repository detection, `swarm init`, provisioning recipes |
| `cli.py` | Argument parsing and command presentation |
| `swarmd.py` | Compatibility facade and executable shim — target under 200 lines |

### The one design rule

> **No extracted module may read `STATE`, `LOGS`, `SLUG`, `CONFIG`, `_run`, `_stop` or any
> other global from `swarmd`.** State arrives as an argument or it does not arrive.

---

## Contracts

Replace the free-form `dict` flow with typed objects. The current dicts are a large part of
why an agent must reread thousands of lines to learn which keys exist.

```python
@dataclass(frozen=True)
class RepoPaths:
    repo: Path
    state: Path          # was the STATE global
    logs: Path           # was the LOGS global
    worktrees: Path
    config: Path
    slug: str            # was the SLUG global

@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable. Taken once when a task is claimed; an in-flight attempt never sees a reload."""
    values: Mapping[str, Any]
    mtime: float

@dataclass
class RuntimeContext:
    paths: RepoPaths
    config: ConfigSnapshot
    events: EventSink          # replaces log() / journal()
    run_state: RunState        # replaces _run / _run_lock
    processes: ProcessRegistry # replaces _processes / _process_lock
    stop: threading.Event      # replaces _stop
```

### `AttemptOutcome`

The stage vocabulary is **not** open for invention. These are the exact strings `_attempt`
returns today, and `failure_class()`, `TASK_STAGES`, `HARNESS_STAGES` and every consumer
depend on them:

```python
STAGES = {
    "accepted",             # landed
    "refused", "out_of_scope", "baseline", "conflict",
    "rejected", "tests_failed", "weakened_tests",
    "no_change", "agent_timeout",
    "model_error", "review_error", "review_format_error",
    # raised as exceptions today, surfaced as stages by the caller:
    "model_pool_exhausted", "exploration_exhausted", "request_budget",
    "interrupted", "deferred",
}

@dataclass(frozen=True)
class AttemptOutcome:
    stage: str               # one of STAGES
    note: str
    failure_class: str | None  # "task" | "model" | "harness" | "review_format" | None
    implementer: str | None
    reviewer: str | None
    commit: str | None
    requests: int
    evidence_path: Path | None
    scores: dict | None
```

Normal task outcomes are **returned as data**. Exceptions are reserved for genuine
infrastructure failures.

### `failure_class` must stay a pure function

`failure_class(stage, note, edited)` (1517) returns exactly `task` / `model` / `harness` /
`review_format`, and classifies via `TASK_STAGES` (1438), `HARNESS_STAGES` (1506),
`HARNESS_NOTE` (1509) and `MODEL_NOTE` (1513). It has no I/O and no global reads. Keep it
that way — it moves early and cheaply, and it is the one piece of policy that is already
clean.

Its documented semantics, which must not drift:

- `task` — the change was wrong: tests failed, or the reviewer proved a defect.
- `model` — the model produced unusable output, or edited and still changed nothing.
- `harness` — this swarm lost the turn: rounds, wall clock, a restart, a sandbox, a provider.
  **A harness failure must never cost the task one of its attempts.**

### `TaskTransition`

`TaskPolicy.release()` returns a decision; it does not act on it:

```python
@dataclass(frozen=True)
class TaskTransition:
    status: str              # "done" | "retry" | "split" | "parked" | "held"
    not_before: float | None
    failure_class: str
    cool_model: tuple[str, str] | None   # (model, reason) for the Ledger, applied by the caller
    counters: Mapping[str, int]          # attempts, harness_failures, exploration_failures, ...
    note: str | None
```

The caller applies it: writes JSONL, appends `done.jsonl`, updates run state, calls
`ledger.cool()`. Policy stays pure and table-testable without a filesystem.

---

## Work packets

### P0 — Pin existing behavior

**Touch:** `tests/` only. No production file.

This is the safety net for everything after it. Do not skip it, and do not shorten it.

1. Add black-box characterization tests covering:
   - every CLI command's JSON output (`swarm status`, `report`, `add`, `plan`, `models`,
     `exceptions`, `stop`, `wake`)
   - `bridge.py` status JSON, field for field
   - queue lifecycle: add → claim → release → done, and the dependency/`blocked_by` path
   - attempt artifact layout under `state/<slug>/attempts/` including `HANDOFF.md`
   - daemon start / drain / stop, and that a requested stop is never converted to a restart
2. Add a **table-driven** test for every `Queue.release()` branch. From the body at 1043 the
   cases are: `done`, `retry` (plain), `retry` (deferred), `retry` (harness, each backoff
   tier: provider-busy 0s, timeout 30m, incomplete/no-usable 15m, transport 2m), `split`
   (attempts exhausted), `split` (`exploration_exhausted` at depth < max), `split`
   (`model_pool_exhausted` at depth < max), `parked` (depth exhausted), `parked`
   (`review_format`), `parked` (harness failures ≥ `MAX_HARNESS_FAILURES`), and the
   run-level model circuit breaker firing at the second non-harness failure.
   Assert the resulting task dict field by field, including `attempts`, `harness_failures`,
   `exploration_failures`, `requests_spent`, `turn_seconds`, `role_turns`, `not_before`,
   `failed_models` truncation to 8, and `notes` truncation to 2.
3. Record the expected `_attempt` stage sequence and fault behavior as a test.
4. Add import-mode smoke tests for **both** identities from **L1**: `from swarm import swarmd`
   and `sys.path`-inserted `import swarmd`. Assert they are distinct module objects today, so
   the test documents the hazard rather than hiding it.
5. Write `tests/test_compat_surface.py` containing the frozen list of the 41 `swarmd` symbols
   `bridge.py` uses (listed in **Ground truth**), asserting each still resolves. This is the
   compatibility manifest; later packets check against it.

**Acceptance:** suite green; no production file modified (`git diff --stat` shows only
`tests/`).
**Commit:** `test(swarm): characterize swarmd CLI, bridge, queue and attempt contracts`

---

### P0.1 — Close two holes in the P0 net

**Status at `b8fe88f`:** P0 landed 961 lines across four test files and is substantially
sound — the `Queue.release()` table covers all 18 branches, the CLI/bridge/artifact
characterization is complete, and `test_attempt_stages.py` genuinely drives `do_task`
against a real temp git repo with a real worktree and a fake flint. Two gaps remain, both in
the part P9 depends on most. Close them before P1.

**Touch:** `tests/test_attempt_stages.py` only.

#### Hole 1 — the stage→`failure_class` mapping is not actually pinned

`test_the_frozen_stage_vocabulary_is_closed` asserts:

```python
self.assertIn(swarmd.failure_class(stage), ("task", "model", "harness", "review_format"))
```

This is **vacuous**. `failure_class()` ends in `return "task"` (line ~1548), so *every*
string returns one of those four. Demonstrated:

```
failure_class('banana')      -> 'task'
failure_class('not_a_stage') -> 'task'
failure_class('')            -> 'task'
```

The test therefore still passes if `baseline` is dropped from `HARNESS_STAGES` and starts
classifying as `task` — which would silently make a harness failure cost the task one of its
two lives, breaking **invariant 7**. That is the single most important thing P9 must not
change, and right now nothing guards it.

Replace the vacuous assertion with the explicit mapping. These values were measured at
`b8fe88f` — assert them exactly, do not recompute or infer them:

```python
STAGE_CLASS = {
    "accepted":              "task",       # never routed through failure_class in practice
    "refused":               "harness",
    "out_of_scope":          "task",
    "baseline":              "harness",
    "conflict":              "harness",
    "rejected":              "task",
    "tests_failed":          "task",
    "weakened_tests":        "task",
    "agent_timeout":         "harness",
    "review_format_error":   "review_format",
    "model_pool_exhausted":  "harness",
    "exploration_exhausted": "harness",
    "request_budget":        "harness",
    "interrupted":           "harness",
    "deferred":              "harness",
}
# note- and edited-dependent, so each needs both directions asserted:
#   failure_class("model_error",  "")                 -> "model"
#   failure_class("model_error",  "connection reset") -> "harness"   (HARNESS_NOTE hit)
#   failure_class("review_error", "")                 -> "model"
#   failure_class("review_error", "timed out")        -> "harness"   (HARNESS_NOTE hit)
#   failure_class("no_change", "", edited=None)       -> "harness"   (never reached an edit)
#   failure_class("no_change", "", edited=3)          -> "model"     (edited, changed nothing)
```

Also add a negative test: an unknown stage string must be recognizable as unmapped. Assert
`"banana" not in STAGE_CLASS` and that the mapping's key set equals the 18-stage vocabulary
minus the three note-dependent ones, so a stage added later cannot slip through unasserted.

#### Hole 2 — only 3 of 18 stages are exercised end to end

`test_attempt_stages.py` drives `do_task` for `refused`, `out_of_scope` and `accepted`. The
failure paths are unexercised, and they are exactly the ones that run through the gate/review/
repair middle of `_attempt` — the hardest part of P9 to move without drift.

Add one end-to-end test per stage below, reusing the existing `RepoCase` + fake-flint pattern
already in the file. Assert the stage string, and assert `failure_class(stage, note, edited)`
on the real note the attempt produced:

| Stage | How to provoke it with the fake flint |
|---|---|
| `baseline` | `test_cmd` that fails on the untouched checkout (e.g. `"false"`) — assert **no model calls were spent** |
| `tests_failed` | implementer writes a file; `test_cmd` fails on the candidate |
| `rejected` | implementer writes a file; adversary returns `verdict="reject"` with a blocker finding (`review(prompt, "reject")` already supports this) |
| `weakened_tests` | implementer deletes or guts an existing test file; assert it is refused and `TEST_CHANGE_ORIGINS` is what permits the exception |
| `no_change` | implementer returns without touching the worktree — assert `edited=None` path gives `harness`, and the edited path gives `model` |
| `model_error` | implementer raises `swarmd.ModelError` |
| `conflict` | land a competing commit on `swarm/trunk` between snapshot and integrate so the rebase cannot apply |

`conflict` is the fiddliest; if it cannot be provoked reliably in under ~30 lines, mark it
with a clear `TODO(P9)` comment naming what is unproven and say so in your report — an
honest gap is fine, a silent one is not.

**Acceptance:**
- Suite green. Paste the actual `pytest` output, before and after.
- `git diff --stat` shows only `tests/test_attempt_stages.py`.
- Deleting `"baseline"` from `HARNESS_STAGES` in `swarmd.py` makes the new mapping test
  **fail**. Verify this by actually doing it, confirming the failure, then reverting with
  `git checkout -- swarm/swarmd.py`. Report the failure message you saw. A guard you have not
  seen fail is not a guard.

**Commit:** `test(swarm): pin the stage/failure_class mapping and the unexercised attempt stages`

---

### P1 — Generalize restart detection *before* any extraction

**Touch:** `swarm/swarmd.py` (`source_files`), `tests/test_resume_and_restart.py`.
**Why first:** see **L2**. Every later packet is unsafe under a live daemon until this lands.

Replace the hard-coded set with the production Python files under `swarm/` plus the two repo
root entry points, excluding generated trees:

```python
WATCH_SKIP = ("state", "wt", "__pycache__", ".venv", ".sessions",
              ".pytest_cache", ".albatross")

def source_files():
    out = {ROOT / "flint.py", ROOT / "nonstop.py"}
    for p in HERE.rglob("*.py"):
        if any(part in WATCH_SKIP for part in p.relative_to(HERE).parts):
            continue
        out.add(p)
    return sorted(out)
```

Prefer this over resolving the import closure of the entry point: the closure misses a module
that is imported lazily inside a function, and this codebase does that (`from swarm import
swallow` inside function bodies in `corpus_index.py:77`, `providers.py:301`, `routing.py:51`,
`hardware.py:50`).

**Acceptance:**
- `source_files()` includes every file in `swarm/*.py` and excludes everything under
  `swarm/state/` and `swarm/wt/`. Assert the exclusion explicitly — a worktree file appearing
  here causes a restart loop.
- Editing any `swarm/*.py` triggers the existing drain-and-restart path.
- A syntactically invalid edit is still refused by `source_compiles()` and journals
  `self_restart_refused`.
- A requested stop is still never converted into a restart (`_drain_reason`).
- Fingerprinting the larger file set does not measurably slow the daemon loop.

**Commit:** `fix(swarm): watch all production sources for self-restart, excluding state and worktrees`

---

### P2 — Extract low-coupling leaves

**Touch:** new `swarm/prompts.py`, `swarm/context_builder.py`, `swarm/task_rules.py`,
`swarm/provisioning.py`; `swarm/swarmd.py`; affected tests.

Move, **without redesigning anything**:

- → `prompts.py`: `FAST_IMPLEMENTER` (1886), `PERSONAS` (2203), `persona_text` (2221),
  `ARCHITECT` (2235), `IMPLEMENTER` (2266), `ADVERSARY` (2309), `JUDGE` (2339),
  `PRIORITIES` (2373), `PLANNER` (2381), `PLANNER_SERVES` (2423), `DECOMPOSER` (2429), `REPAIR`
- → `task_rules.py`: `goal_items` (507), `goal_item_weight` (530), `failure_class` (1517) with
  `TASK_STAGES`/`HARNESS_STAGES`/`HARNESS_NOTE`/`MODEL_NOTE`, `scope_gap` (1456) with
  `NAMES_A_PATH`/`NAMES_A_SYMBOL`/`BROAD_ASK`, `first_failing_line` (1555) + `FAILING`,
  `estimate_requests` (797) + `STATIC_REQUEST_ESTIMATES`
- → `context_builder.py`: `numbered` (1609), `outline` (1614), `task_text` (1623),
  `named_paths` (1629), `kin_text` (1636), `covering_tests` (1651), `context_pack` (1665),
  `attempt_lines` (1561) + `CODE_PATH`/`GAME_DIR`/`DEFINITION`
- → `verification.py` (create, partial): `assert_counts` (2502), `identifiers` (2511),
  `deleted_paths` (2521), `test_hunks` (2533), `names_deleted` (2557), `names_its_source` (2566),
  `weakened_tests` (2575), `last_error` (2618), `ASSERT_CALL`, `GENERIC_DIR`
- → `provisioning.py`: `detect_test_cmd` (4546), `embedded_repos` (4562), `_subdirs` (4571),
  `sub_projects` (4578), `test_cmd_hint` (4586), `test_cmds` (4606), `project_test_cmd` (4644),
  `other_test_cmds` (4649), `scaffold` (4662), `configure` (4683), `why_unrunnable` (4704),
  `probe_surroundings` (5608), `build_prov_block` (5650), `render_surroundings` (5705)

Re-export every moved name from `swarmd.py` for now.

**Acceptance:**
- Prompt text is **byte-for-byte** identical. Verify mechanically, not by eye:
  ```sh
  git stash && .venv/bin/python -c "
  from swarm import swarmd; import json,hashlib
  names=['ARCHITECT','IMPLEMENTER','ADVERSARY','JUDGE','PLANNER','DECOMPOSER','PRIORITIES','FAST_IMPLEMENTER','PLANNER_SERVES']
  print(json.dumps({n:hashlib.sha256(getattr(swarmd,n).encode()).hexdigest() for n in names},indent=0))
  " > /tmp/prompts-before.json && git stash pop
  ```
  Re-run after the move and `diff` the two files.
- No new module reads a `swarmd` global. Verify: `grep -n "swarmd\." swarm/prompts.py
  swarm/task_rules.py swarm/context_builder.py swarm/verification.py swarm/provisioning.py`
  returns nothing.
- `tests/test_compat_surface.py` from P0 still passes.
- Patch targets repointed per **L4**. `tests/test_prompt_hygiene.py`,
  `test_context_pack.py`, `test_failure_classes.py`, `test_goal_priorities.py`,
  `test_weakened_tests.py` and `test_provisioning.py` are the ones most likely to need it.

Expected: 1,000–1,500 lines out of `swarmd.py` with no runtime behavior touched.

**Commit:** `refactor(swarm): extract prompts, task rules, context building, verification helpers, provisioning`

---

### P3 — Introduce `RuntimeContext`

**Touch:** new `swarm/runtime_context.py`; `swarm/swarmd.py` (`use_repo`); tests.

Add `RepoPaths`, `ConfigSnapshot`, `RunState`, `EventSink`, `ProcessRegistry`,
`RuntimeContext` as defined in **Contracts**.

`use_repo()` constructs and returns a context **and continues to populate the compatibility
globals** so old callers keep working. New modules take the context explicitly. Globals are
retired one caller at a time in later packets.

**Acceptance — this is the packet's whole point:**
- Two repository contexts coexist in one Python process and do not write to each other's
  queue, logs, baseline cache, worktrees or attempt artifacts. Write this as a test with two
  tempdir repos; it is the test that proves the refactor is worth doing.
- `EventSink` reproduces `log()` (95) and `journal()` (103) output byte-for-byte, including
  the existing lock behavior.
- `ProcessRegistry` preserves `process()` (564) / `kill_group()` (554) / `shutdown()` (580)
  semantics, including process-group kill.
- Both module identities from **L1** still work.

**Commit:** `refactor(swarm): add RuntimeContext for repo paths, run state, events and processes`

---

### P4 — Immutable attempt configuration

**Touch:** `swarm/swarmd.py` (`Worker.run`, `Worker._attempt`, `cfg`/`load_cfg`/`watch_config`).

Fixes **L3**. Capture one `ConfigSnapshot` when a task is claimed and thread it through the
entire attempt. Nothing inside an attempt may call `cfg()` or re-read config.

**Acceptance:**
- An in-flight attempt keeps the configuration it started with across a reload. Test it:
  claim a task, mutate the config file, assert the attempt's model pool, spend cap and trunk
  name are unchanged.
- A reload still affects the **next** claimed task.
- `RELOADABLE` (155) and `RESTART_ONLY` (164) semantics are unchanged: restart-only keys still
  force a restart rather than applying live.

**Commit:** `fix(swarm): each attempt runs on an immutable config snapshot taken at claim time`

---

### P5 — Split queue persistence from queue policy

**Touch:** new `swarm/task_queue.py`; `swarm/swarmd.py`; `tests/`.

Split `Queue` (812) into three:

```
JsonlTaskStore   fcntl locking, atomic replace, pending/done files
TaskPolicy       pure claim ranking + release transitions → TaskTransition
TaskQueue        public orchestration API (what bridge.py and Worker call)
```

`TaskPolicy.release()` returns a `TaskTransition` (see **Contracts**). It must not touch
`_run`, call `ledger.cool()`, or write a file. `TaskQueue` applies the transition.

**Keep JSONL.** Changing storage and behavior in the same packet makes failures impossible to
attribute.

**Acceptance:**
- Existing `queue.jsonl` and `done.jsonl` files remain readable, and newly written rows are
  schema-identical. Diff a round-tripped fixture.
- Claim ordering is unchanged — `selection_score` (977) must produce identical rankings on the
  P0 fixtures.
- Dependency behavior unchanged (`resolve` 831, `blocked_by` 1171, `repoint` 906).
- Multiprocess locking tests pass; `fcntl` locking and atomic replacement preserved.
- **Every** `Queue.release()` case from the P0 table produces a byte-identical resulting task.
- `TaskPolicy` tests run with no filesystem access at all.

**Commit:** `refactor(swarm): split Queue into JsonlTaskStore, pure TaskPolicy and TaskQueue`

---

### P6 — Extract workspace and integration services

**Touch:** new `swarm/workspace.py`; `swarm/swarmd.py`.

Centralize all Git access. Today several call sites wrap `git` independently; after this
packet exactly one module does.

- `WorkspaceManager`: `wt_root` (304), `Worker.ensure_worktree` (3273),
  `Worker.adopt_retained` (3284), `Worker.own_branch` (3311), `Worker.snapshot` (3424),
  `Worker._cleanup` (3890), `retained_diff` (1363), `sh` (716), `git` (729)
- `IntegrationService`: `trunk_name` (492), `trunk_locked` (2629), `ensure_trunk` (2704),
  `checked_out_at` (2713), `refresh_view` (2724), `refresh_view_prov` (2738),
  `sync_refresh_view` (2750), `sync_trunk` (2755), `Worker.integrate` (3723)

**Acceptance:**
- No agent and no other module can move `swarm/trunk`; only `IntegrationService` writes that
  ref. Assert by grep: no `git(["update-ref"...])`, `checkout`, `reset` or `merge` touching the
  trunk name outside `workspace.py`.
- Rebased work is still re-verified and re-reviewed before landing.
- Interrupted work still resumes (`find_interrupted` 1386, `note_interrupted` 1416,
  `load_resume` 1338, `take_resume` 1352).
- `_trunk_lock` / `_view_lock` semantics preserved via the context, not globals.

**Commit:** `refactor(swarm): centralize git, worktrees and trunk landing in workspace services`

---

### P6 is split into P6a–P6d (added at `7caa081`)

P6 as written above was attempted twice and stalled both times in exploration: more than ten
read calls and no edit. Two causes. First, every line number in P6 is from `8cc415a`, when
`swarmd.py` was 6,212 lines; at `7caa081` it is 4,773, so none of them point at the right
code. Second, P6 moves 68 `git([...])` call sites and six `Worker` methods in one commit.
**Do the slices below in order, one per session, one commit each.** The P6 acceptance checks
apply to the slices as a whole, once P6d lands.

**Line numbers in this section were measured at `7caa081`.** They drift after each slice
lands, so re-locate a name with `grep -n "def NAME" swarm/swarmd.py` before you read around
it. Do **not** read `swarmd.py` top to bottom. Use `file_read` with a line range around each
`grep` hit.

#### The wrapper pattern (mandatory for every slice)

27 test call sites do `patch.object(swarmd, "git" | "ensure_trunk" | "refresh_view" |
"sync_trunk" | "checked_out_at" | "wt_root")`, and both module identities from **L1** have to
keep honouring those patches. So:

1. Move the function **body** to `swarm/workspace.py`, and pass every global it used as an
   argument: `git`, `log`, the state path, the lock, the worktree root. Under the one design
   rule, `workspace.py` must not import `swarmd` or read its globals.
2. In `swarmd.py`, keep a function **with the same name and signature** whose body is a
   one-line delegation that passes `swarmd`'s own `git`/`log`/etc. Those names are resolved at
   call time, so a test that patches `swarmd.git` still intercepts every git call made through
   `workspace.py`.
3. Leave every existing caller inside `swarmd.py` calling the `swarmd` name. Do not repoint
   callers to `workspace.` directly in this packet.

Example — this is the whole of the expected change for `checked_out_at`:

```python
# swarm/workspace.py
def checked_out_at(c, branch, git):
    _, out = git(["worktree", "list", "--porcelain"], cwd=c["repo"])
    ...                                   # body moved verbatim

# swarm/swarmd.py
def checked_out_at(c, branch):
    return workspace.checked_out_at(c, branch, git=git)
```

Import `workspace` in `swarmd.py` the same way `task_queue` is imported: the
`try: from . import ... / except ImportError: import ...` form at the top of
`swarm/task_queue.py`.

#### P6a — module-level git and trunk primitives

**Touch:** new `swarm/workspace.py`; `swarm/swarmd.py`. No test file should need changing.

Move these, using the wrapper pattern (lines at `7caa081`):

| Name | Line | Injected arguments |
|---|---|---|
| `sh` | 807 | `process`, `kill_group` |
| `git` | 820 | `sh`, `ident` (= `GIT_IDENT`, line 149) |
| `trunk_name` | 618 | none (pure) |
| `trunk_locked` | 1463 | `thread_lock` (= `_trunk_lock`), `state` (= `STATE`) |
| `ensure_trunk` | 1538 | `git`, `locked` (= `trunk_locked`), `log` |
| `checked_out_at` | 1547 | `git` |
| `refresh_view` | 1558 | `git`, `ensure_trunk`, `worktrees` (= `wt_root()`) |
| `sync_refresh_view` | 1584 | `refresh_view` |

Do **not** move `wt_root`, `refresh_view_prov`, `sync_trunk`, `retained_diff` or any `Worker`
method in P6a.

**Acceptance:**
- Suite green before and after. Paste both outputs.
- `git diff --stat` shows only `swarm/workspace.py` (new) and `swarm/swarmd.py`.
- `grep -n "swarmd" swarm/workspace.py` returns nothing.
- Each of the eight `swarmd` wrappers is at most 3 lines of body.

**Commit:** `refactor(swarm): move git, sh and trunk primitives into workspace.py (P6a)`

#### P6b — trunk sync and retained work

Same pattern. Move `sync_trunk` (1589), `refresh_view_prov` (1572) and `retained_diff` (922).
`sync_trunk` calls `run_gate_with_prov`, which belongs to P7: pass it in as a callable and do
not move it. `_sync_failed` (a module dict) is passed in as an argument too.
**Commit:** `refactor(swarm): move trunk sync and retained-diff into workspace.py (P6b)`

#### P6c — worktree lifecycle

Move the bodies of `Worker.ensure_worktree` (2136), `Worker.adopt_retained` (2148),
`Worker.own_branch` (2175), `Worker.snapshot` (2290) and `Worker._cleanup` (2775) into
**stateless functions** in `workspace.py`. They take the worktree path, branch, review base
and stage as arguments and return results. They do not live in a class that holds that state.
`self.wt` is read in 27 places in `swarmd.py`, and tests assign `.wt` and `.branch` directly,
so `Worker` keeps owning those fields until P9/P10. The `Worker` methods stay as short
delegations that assign whatever the function returns, because tests and `do_task` call them
by those names.
**Commit:** `refactor(swarm): move worktree lifecycle into WorkspaceManager (P6c)`

#### P6d — integration

Move only the **git operations** out of `Worker.integrate` (2599–2641) into `workspace.py`,
not the whole method: the rebase onto trunk, and the compare-and-swap landing under
`trunk_locked` (the only code outside `sync_trunk` that runs `update-ref` on trunk). The
re-verify / re-review loop calls `snapshot`, `gate`, `reviewed` and `scope_violations` and
mutates `review_base`, so it is attempt orchestration. It stays in `Worker.integrate` and moves
with `AttemptRunner` in P9. Then run the P6 acceptance checks
above, all of them, and paste the grep output.
**Commit:** `refactor(swarm): move trunk landing into IntegrationService (P6d)`

---

### P7 — Extract gate execution

**Touch:** `swarm/verification.py` (extend from P2); `swarm/swarmd.py`.

Move `GateRunner`: `baseline_key` (2459), `baseline_cache` (2465), `run_gate` (2487),
`prov_block` (2646), `run_prov_hooks` (2658), `run_gate_with_prov` (2693), `Worker.gate` (3385).

**Acceptance:**
- Baseline cache keys and TTL (86,400s) unchanged; a cached pass still short-circuits and
  still records the `baseline_cached` evidence event.
- Provisioning still runs before a gate, with identical hook ordering and env.
- Gate evidence records are byte-identical.
- `weakened_tests` enforcement is unchanged, and `TEST_CHANGE_ORIGINS` (59) still gates who may
  weaken a test.
- Verification **never mutates task state**. Grep: no queue writes in `verification.py`.

**Commit:** `refactor(swarm): move gate execution, baseline cache and provisioning hooks into verification`

---

### P8 — Extract agent-turn execution

**Touch:** new `swarm/agent_turn.py`; `swarm/swarmd.py`.

- `AgentTurnRunner` from `flint()` (2009): prompt files, subprocess invocation, timeouts,
  request sidecars, charge sidecars, exit-code translation, turn logs. Returns a typed
  `TurnResult` or raises a typed turn error.
- `ModelRouter`: `pool` (308), `paid_pool` (326), `paid_stand_in` (424), `route_to_paid` (1993),
  `rest` (1948), `next_wake` (1963), `all_resting` (1969), `Worker.pick` (3113),
  `Worker.select_implementer` (3131), `Worker.excluded_models` (3144), `mit_arm` (1848)
- Move the typed error hierarchy with it: `CapReached` (1866), `NoCredits` (1870),
  `StepLimit` (1874), `ModelError` (1878), `ReviewFormatError` (1882), `AgentTimeout` (1903),
  `ExplorationExhausted` (1912), `Stopped` (1920), `ProviderDown` (1924), `ProviderBusy` (1932),
  `ModelGone` (1936), `RequestBudget` (1940), `ModelPoolExhausted` (1944)

**Keep the subprocess boundary around `flint.py`.** It provides real crash and state
isolation and is not the thing that needs changing.

**Acceptance:**
- Exit-code translation identical for every code `flint.py` can return.
- Spend and request accounting identical: `read_charges` (1976), `count_requests` (1835),
  `count_usd` (1813), `count_edits` (1822), `estimate_requests` all agree with pre-packet
  values on the P0 fixtures.
- Resting/waking and paid routing decisions unchanged, including `quota_mode` (452) and
  `quota_exhausted` (464).
- The per-worker counter globals (`_charges`, `_request_calls`, `_edit_calls`, `_ran_on`,
  `_active_turns`, `_turn_logs`) move onto the context, not into a new module global.

**Commit:** `refactor(swarm): extract AgentTurnRunner and ModelRouter from flint()`

---

### P9 — Build `AttemptRunner`

**Touch:** new `swarm/attempt_runner.py`; `swarm/swarmd.py`.

`AttemptRunner.run(task, ctx, config_snapshot) -> AttemptOutcome` becomes the single
synchronous application operation. Move the lifecycle out of `Worker._attempt` (3500) along
with `Worker.call` (3176), `Worker.review` (3432), `Worker.reviewed` (3456),
`Worker.judge` (3764), `Worker._reinforce` (3782), `Worker.follow_up` (3846),
`Worker.celebrate` (3862), `Worker.hang` (3874), `Worker.strand` (3908),
`Worker.decompose` (3917), `Worker.retry_split` (3971), `Worker.scope_violations` (3984).

Make the phases explicit, in this order:

```
validate scope
→ prepare workspace
→ verify baseline
→ build context
→ implement
→ snapshot
→ verify candidate
→ adversarial review
→ repair if required
→ commit candidate
→ integrate
→ judge
→ record learning
```

Every exit returns an `AttemptOutcome` with a stage from the frozen `STAGES` set in
**Contracts**. Exceptions only for true infrastructure failure.

**Acceptance:**
- Every stage string in `STAGES` is reachable and produces the same `failure_class` as before.
- The invariants below hold, each with a test that fails if the guard is removed.
- `AttemptRunner` is testable without a real model: inject a fake `AgentTurnRunner`.

**Commit:** `refactor(swarm): extract the attempt state machine into AttemptRunner`

---

### P10 — Thin the `Worker`

**Touch:** new `swarm/worker.py`; `swarm/swarmd.py`.

`Worker` does exactly five things: claim a task, capture a config snapshot, run
`AttemptRunner`, release the outcome, apply backoff or stop. Thread mechanics only.

**Target: under 200 lines** (from ~1,012).

**Acceptance:** suite green; `wc -l swarm/worker.py` under 200; `worker.py` contains no git
command, no prompt text, no model selection and no gate execution.
**Commit:** `refactor(swarm): reduce Worker to claim, execute, release, back off`

---

### P11 — Extract the `Supervisor`

**Touch:** new `swarm/supervisor.py`, `swarm/planning.py`, `swarm/roadmap.py`,
`swarm/reporting.py`; `swarm/swarmd.py`.

- `supervisor.py` from `run_daemon` (5238) and `start` (5133): worker construction, daemon
  locks (`holder` 4894), signals, deadlines (`hours_left` 5115), draining (`drain` 700),
  config reloads, source-change restarts, `keep_awake` (4873), allowance reconciliation
  (`allowance_recheck` 4429, `resync_allowance` 4437, `recheck_allowance` 4493).
  Schedule these as **explicit periodic jobs**, not branches in one polling function.
- `planning.py`: `plan` (4119), `serving_the_goal` (4093), `json_object` (4220),
  `idle_state` (4240), `write_idle` (4255), `settle_idle` (4265), `idle_ready` (4283),
  `idle_improvement` (4306), `IDLE_IMPROVEMENT`, `IDLE_MIN_SECONDS`
- `roadmap.py`: `dispatch_roadmap` (4945), `read_roadmap` (5107), `BLOCKER_ACTIONS` (4908),
  `_roadmap_said` (onto the context)
- `reporting.py`: `run_status` (2870), `write_now` (2811), `read_now` (3043),
  `report_progress` (3052), `mark_progress` (2865), `promotion_state` (2955),
  `landed_commits` (2981), `health` (3018), `build_report` (5407), `attempts_for` (5073),
  `prune_attempts` (5084), `retry_recorded` (4922), `turn_round` (2800), `Tally` (3066),
  `waiting` (1765), `worker_publish` (1773), `forget_worker` (1784), `live_workers` (1799)

**Acceptance:**
- Status JSON and report content byte-identical to the P0 baselines.
- Reporting **reads** state and never changes it. Grep `reporting.py` for queue writes.
- Stop-vs-restart distinction (`_drain_reason`, 697) preserved.

**Commit:** `refactor(swarm): extract Supervisor, planning, roadmap and reporting from run_daemon`

---

### P12 — Public application API

**Touch:** new `swarm/app.py`; `swarm/bridge.py`; new `swarm/cli.py`.

```python
app = SwarmApplication.for_repo(repo)
app.status(); app.add_task(...); app.report(...); app.start(...); app.stop(...)
```

Migrate `bridge.py` onto it and move argument parsing to `cli.py` (`main` 6135, `_setup` 5531,
`cmd_*` 5542–6135).

**Acceptance:**
- `bridge.py` no longer touches `STATE`, `LOGS`, `CONFIG`, `_read`, `_run` or `_stop`.
  Verify: `grep -n "swarmd\._\|swarmd\.STATE\|swarmd\.LOGS\|swarmd\.CONFIG" swarm/bridge.py`
  returns nothing.
- Because `bridge.py` no longer carries module state, **L1 stops mattering** for it. Confirm
  the `SWARMDS = (swarmd, bridge.swarmd)` workaround can be deleted from
  `test_bug_reports.py`, `test_config_resolution.py`, `test_daemon_control.py` and
  `test_mit_bridge.py`, and delete it. This is the packet where that scar comes out.
- Existing CLI commands and their JSON remain compatible against the P0 baselines.

**Commit:** `refactor(swarm): add SwarmApplication facade; bridge and CLI stop using swarmd internals`

---

### P13 — Enforce the dependency rules

**Touch:** new `tests/test_module_boundaries.py`.

A test that fails the build on a boundary violation:

1. No production module under `swarm/` imports `swarmd` (allow `bridge.py` only until P12 is
   complete, then allow nobody).
2. `prompts.py` imports no runtime state.
3. `task_queue.py` imports no git and no provider module.
4. `verification.py` performs no queue writes.
5. `workspace.py` contains no retry-policy decisions.
6. `reporting.py` performs no state mutation.
7. Lower layers never import higher ones, per the arrows in **Target architecture**.

Implement by walking `ast` over each module's imports rather than by grep, so aliased imports
are caught too.

**Commit:** `test(swarm): enforce module dependency direction`

---

### P14 — Reduce `swarmd.py` to a facade

**Touch:** `swarm/swarmd.py`.

Keep only compatibility re-exports and `main()`. Remove a compatibility export only once no
caller and no test uses it — check against `tests/test_compat_surface.py` from P0.

**Acceptance:** `wc -l swarm/swarmd.py` under 200; suite green; every CLI command works;
daemon starts, drains and stops.
**Commit:** `refactor(swarm): reduce swarmd.py to a compatibility facade and entry point`

---

### P14 is blocked until P14a lands (added after P13, 2026-10-11)

P14's two rules contradict each other at the end of P13. "Under 200 lines" cannot coexist with
"remove a compatibility export only once no caller and no test uses it": the tests touch **230
distinct `swarmd` names** and patch **83 of them at 353 sites in 31 files**. A patch-honoring
wrapper is at least two lines, so the surface alone is over 400 lines. The tests have to stop
patching `swarmd` first. Measured at P13 (`b9e12dc`):

| Patch target kind | Sites | Examples | Where the seam belongs |
|---|---|---|---|
| Services | ~250 | `flint` 77, `log` 37, `process` 13, `git` 9, `account` 8, `sandboxed` 7, `refresh_view` 11 | the deps objects: `TurnDeps`, `AttemptDeps`, `WorkerDeps`, `SupervisorDeps` |
| Repository paths | ~60 | `STATE` 35, `HERE` 7, `LOGS` 5, `ROOT` 5, `CONFIG` | `RuntimeContext.paths` |
| Entry points | ~25 | `_setup` 13, `run_daemon` 4, `start`, `cmd_status`, `cmd_add` | `cli.py` handlers, `supervisor` |

### P14a — Migrate the tests off `swarmd`'s patch surface

**Touch:** `tests/` only, plus one new `tests/fixtures.py`. No production file.

Slice it by test file, largest first; one slice per session, one commit each. For every slice:

1. Add to `tests/fixtures.py`, once, a `daemon_services(**fakes)` context manager that builds the
   real deps objects (`swarmd._turn_deps()`, `_attempt_deps()`, `_worker_deps()`,
   `_supervisor_deps()`) with `dataclasses.replace(..., **fakes)` and patches the four builders to
   return them. A test then writes `with daemon_services(flint=fake):` instead of
   `patch.object(swarmd, "flint", fake)`.
2. Add a `repo_paths(root)` fixture that runs `use_repo` against a temp `HERE` and yields the
   returned `RuntimeContext`, replacing hand-patched `STATE`/`LOGS`/`HERE`.
3. Repoint the slice's patch sites. A test's assertions do not change. If an assertion has to
   change, the slice found a behavior difference: stop and report it.
4. Acceptance per slice: suite count unchanged; `grep -c 'patch.object(swarmd' <file>` drops to
   0 for that file; `tests/mutate_invariants.py` still kills every mutant.

**Done when:** `grep -rn "patch.object(swarmd\|swarmd\.[A-Z_]* = " tests/` returns nothing
outside `tests/fixtures.py`. Then P14 can delete each wrapper whose name no test references, and
the 200-line target becomes reachable.

Two further prerequisites for P14 that P14a does not cover:
- The composition root still keeps repository state in module globals (`STATE`, `LOGS`, `CONFIG`,
  `_run`, `_stop`). "Two repository contexts coexist safely in one process" needs those read from
  the `RuntimeContext` that `use_repo` returns; give that its own packet.
- `bridge.py` still imports `swarm.swarmd` for its pure helpers and the `cmd_*` surface. It
  stops importing it once those move to `cli.py`/`app.py`; P13's allow-list entry for `bridge`
  comes out in the same commit.

---

## Invariants

These are the product. Each must keep a test that fails if its guard is removed. If a packet
cannot preserve one of these, **stop and report** rather than proceeding.

1. Tests pass before review is requested.
2. Adversarial review approves the **exact** candidate tree.
3. The committed tree equals the reviewed tree.
4. A rebase triggers new verification **and** new review.
5. Existing tests cannot be weakened without human authorization
   (`weakened_tests` + `TEST_CHANGE_ORIGINS`).
6. Only the integration service moves `swarm/trunk`.
7. A harness failure never costs a task one of its `MAX_ATTEMPTS` lives.
8. A requested stop is never converted into a restart.
9. The daemon never restarts into code that does not compile.
10. Atomic file replacement and `fcntl` locking are preserved on every state file.

## Do not

- Do not move `Worker` first. It is the centre of the dependency knot; it should be the
  **result** of P5–P9, not the first cut.
- Do not change persistence formats while extracting queue behavior.
- Do not replace threads while also decomposing the worker.
- Do not convert anything to a network service in this work order.
- Do not combine file movement with behavior change in one commit.
- Do not remove the `swarmd` compatibility surface in one pass.
- Do not let modules wrap git independently — `workspace.py` owns git after P6.
- Do not create one-function modules just to reduce line count.
- Do not add `swarm/__init__.py` to "fix" **L1**. P3 and P12 fix it properly.
- Do not exceed ~500 lines per production module or ~150 lines per orchestration method.

## Later: multi-machine distribution

Only once the boundaries above are stable. Then you replace **adapters**, not orchestration:

| Now | Then |
|---|---|
| `JsonlTaskStore` | Postgres tasks with lease IDs, expiry, heartbeat, atomic claim, idempotency keys |
| Local artifact paths | `ArtifactStore` over object storage |
| Local event journal | Append-only event stream |
| In-process budget | Central allowance/spend coordinator |
| Local trunk mutation | One integration coordinator doing compare-and-swap |
| Unix signals | Persisted cancellation and drain tokens |
| Worker threads | Stateless worker processes advertising capabilities |

Do **not** deploy planner, implementer, adversary and judge as separate services. They are
roles within one attempt, not independently valuable deployment units. **The whole
`AttemptRunner` is the useful remote boundary.**

## Done when

- `swarmd.py` is a thin entry point and compatibility facade (under 200 lines).
- No production module imports `swarmd`.
- No subsystem depends on mutable repository globals.
- `Worker` is a small task-execution loop.
- `AttemptRunner` is independently testable with a fake turn runner.
- Queue policy is testable with no filesystem access.
- Two repository contexts coexist safely in one process.
- Every attempt runs on one immutable config snapshot.
- Bridge and CLI use `SwarmApplication`; the dual-module workaround is deleted.
- Queue, evidence, CLI, bridge and state formats are all unchanged.
- The full suite passes after every packet.
- All ten invariants retain their guards and their tests.
