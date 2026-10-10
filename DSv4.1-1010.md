# swarmd.py de-monolith — shift 1

**Date:** 2026-10-10
**Model:** DeepSeek (whatever's currently in the free pool — budget earmarked for this specifically)
**Sizing:** ~10 hours of implementer time across 19 packets. Each packet is independently landable; stop after any packet and the repo is still correct, just not finished.
**Baseline, recorded before touching anything:** `tests/` → **874 passed, 38 subtests passed, 54.41s** (root suite, via `.venv/bin/python -m pytest tests/ -q`). Any packet that doesn't reproduce this count (or a higher one, if it's adding coverage) failed, full stop — go back and find what broke before moving to the next packet.

---

## Why this file exists

`swarm/swarmd.py` is 6,152 lines. The next-biggest file in the repo is `flint.py` at 1,985, and that one's already correctly scoped. This is the single biggest piece of structural debt in the harness, named explicitly in an earlier code review: *"the swarm's own swarm couldn't even decompose it — VECTOR stops at depth 2, the file defeats its own decomposition tool."*

The good news, found by actually reading the file rather than guessing: **it already marks its own seams.** It has 24 `# ------ section` comment dividers (plumbing, queue, resuming interrupted work, failure classes, context pack, flint, roles, gate, provisioning, worker, planning, spare time, account, setup, daemon, report, commands, surroundings, bug reports, launchd...), and the test suite's file names mirror those sections almost 1:1 (`test_context_pack.py`, `test_failure_classes.py`, `test_provisioning.py`, `test_resume_and_restart.py`, `test_weakened_tests.py`, `test_bug_reports.py`...). That's independent confirmation these are real module boundaries, not just visual dividers, and it hands every packet below a pre-existing verification gate for free.

## What breaks first, concretely

Ranked by how soon it bites, not by how interesting it is to talk about:

1. **`source_files()` (line 630) hardcodes a 5-file watch list** (`flint.py`, `nonstop.py`, `swarmd.py`, `budget.py`, `learn.py`, `workflow.py`, `sandbox.py`) that drives the daemon's self-restart-on-edit safety net. Every file this shift creates falls outside it until it's updated. This breaks *during this exact refactor*, not someday — it's packet 0's job.
2. **~25 module-level mutable globals** (`_active_turns`, `_run`, `_held`, `_charges`, `_study_calls`, several locks) assume **one daemon process per repo**. A standalone app that ever wants to supervise multiple repos' grinds from one running process (rather than one OS process per daemon, which is the current model) hits this immediately — two "sessions" would silently share state. Not in scope to fix this shift; just don't make it worse, and flag it for whoever designs the standalone app's process model.
3. **Config is resolved once at import time** (`CONFIG = Path(os.environ.get("FLINT_SWARM_CONFIG") or ...)`, `HERE`/`ROOT` from `__file__`). Fine for a CLI invoked fresh per command. Breaks the moment something wants to switch repos inside one long-lived process without re-importing the module.
4. **The only API surface is "spawn a subprocess, parse stdout JSON"** (`bridge.py`). Works for the current VS Code extension (it already does exactly this). A standalone app wanting a live-updating dashboard instead of polling will want a push channel eventually — additive, not urgent, but it's the next interface boundary and worth knowing it's coming.
5. **macOS-only system calls** (`sandbox-exec` via `sandbox_profile`/`sandboxed`, `osascript`, `caffeinate`) assume this platform. A cross-platform standalone app hits this on day one of a Windows/Linux build.
6. **State and config live inside the source checkout** (`swarm/state/`, `swarm/configs/`, `swarm/logs/`), not an OS-appropriate per-user data directory. Breaks the first time this is packaged rather than run from a git clone.

None of #2–6 are this shift's job. They're named so whoever scopes the standalone app isn't surprised later. This shift only fixes #1, because it's caused by the work itself.

## Ground rules for every packet (don't repeat per-packet, just follow)

- **One packet, one commit.** Pure move: cut the section out of `swarmd.py`, paste into the new file, fix imports on both sides, add nothing, remove nothing, rename nothing. If something looks like it deserves a rename or a cleanup, write it down for later — don't do it in the same commit as a move.
- **Line numbers drift after every packet.** Re-`grep -n "^def \|^class "` before starting each one rather than trusting the numbers in this doc — they're accurate as of baseline, not after packet 3.
- **Before moving anything, `grep -rn` the symbol name across the *whole repo* (not just `swarmd.py`)** — `bridge.py`, `saber.py`, `vector_runner.py`, `bench_swarm.py`, and the test files can all import directly from `swarmd`. This doc names the test file(s) found for each packet, but it was not exhaustively checked against every non-test file, and that check is each packet's job, not this doc's.
- **Circular imports are the real hazard, not line count.** New modules should import shared low-level bits (`log`, `journal`, `sh`, `clean_env`, path constants, the locks/dicts) from the packet-0 module, never reach back into `swarmd.py` itself. If a function you're moving calls something that's still only in `swarmd.py` and isn't in packet 0's list, that's a signal packet 0 was scoped too narrowly — pull that function into packet 0 too before continuing, don't create the circular import.
- **Acceptance, every packet, no exceptions:**
  1. The named test file(s) pass in isolation (`.venv/bin/python -m pytest tests/test_X.py -v`).
  2. The full suite passes at ≥ the baseline count (874 passed, 38 subtests) — more is fine (you added coverage), fewer means something broke or got silently dropped.
  3. `source_files()` in the packet-0 module includes every file that exists by that point in the shift.
  4. `wc -l swarm/swarmd.py` has dropped by roughly the section's line count.
  5. Commit message names the packet (e.g. `swarmd: extract provisioning into swarm/provisioning.py`).

---

## Packet 0 — shared runtime module (do this first, nothing else works without it)

**Target:** `swarm/_runtime.py`

Move, unchanged:
- The locks and dicts: `_print_lock`, `_journal_lock`, `_process_lock`, `_trunk_lock`, `_view_lock`, `_processes`, `_stop`, `_sync_failed`, `_study_lock`, `_study_calls`, `_edit_calls`, `_request_calls`, `_charges`, `_held`, `_waiting`, `_worker_clocks`, `_ran_on`, `_active_turns`, `_turn_logs`, `_run`, `_run_lock`, `_progress_seen`, `_recheck_lock`, `_rechecked`, `_drain_reason`, `_cfg_lock`, `_cfg_seen`.
- Path/constant globals: `HERE`, `ROOT`, `CONFIG`, `STATE`, `LOGS`, `SLUG`, `NO_CORPUS`, `SECRET_ENV`, `GIT_IDENT`, `DEFAULT_MODEL`, `KINDS`, `META`, `TEST_CHANGE_ORIGINS`, `READ_ONLY_ROLES`, `MAX_ATTEMPTS`.
- Functions: `log`, `journal`, `sh`, `git`, `clean_env`, `kill_group`, `process` (the context manager), `shutdown`, `describe_error`, `reportable`.
- **`source_files()`, `source_fingerprint()`, `source_changed()`, `source_compiles()`, `restart_into_new_code()`** — and **update `source_files()`'s list** to include every module this entire shift creates (list them all now, even the ones that don't exist until packet 17 — they will by the time this is deployed, and `.stat()` on a missing path just gets skipped by the existing `except OSError: continue`, so it's safe to list in advance).

`swarmd.py` becomes `from swarm._runtime import *` (or explicit names — implementer's call, but be consistent across all 19 packets) at the top.

**Acceptance:** full suite ≥ 874/38 green. `swarmd.py` line count drops by ~550. Nothing else changes yet — this packet is infrastructure, not visible behavior.

**Est. 45–60 min** (the widest blast radius of any packet — every other file touches this one).

---

## Packets 1–17 — leaf extractions, safest first

Order matters: each one is chosen to minimize what it depends on that hasn't moved yet.

### 1. `swarm/provisioning.py`
**Source:** `# provisioning` section + the tiny `# trunk` section folded in (it's 8 lines, `trunk_locked()`, used only by what's moving here).
**Functions:** `prov_block`, `run_prov_hooks`, `run_gate_with_prov`, `ensure_trunk`, `checked_out_at`, `refresh_view`, `refresh_view_prov`, `sync_refresh_view`, `sync_trunk`, `trunk_locked`.
**Test:** `tests/test_provisioning.py` — 24 tests, already exercises this exact boundary (this is the file I already fixed a real bug in last session — `run_prov_hooks` env scoping — so it's a known-good, already-trusted seam).
**Note:** `run_gate_with_prov` calls `run_gate`, which is packet 2. Either do packet 2 first, or have this packet temporarily import `run_gate` back from `swarmd` and fix it when packet 2 lands. Cleaner: do packet 2 before packet 1 if that's easier in practice — the order within 1–2 doesn't matter, just do both before anything that depends on either.
**Est. 30 min.**

### 2. `swarm/gate.py`
**Source:** `# gate` section.
**Functions:** `baseline_key`, `baseline_cache`, `run_gate`, `assert_counts`, `identifiers`, `deleted_paths`, `test_hunks`, `names_deleted`, `names_its_source`, `weakened_tests`, `last_error`.
**Test:** `tests/test_weakened_tests.py` (primary), also referenced by `test_prompt_hygiene.py` and `test_swarm_learning.py` — run all three.
**Est. 30 min.**

### 3. `swarm/resume.py`
**Source:** `# resuming interrupted work`.
**Functions:** `resume_file`, `load_resume`, `save_resume`, `take_resume`, `retained_diff`, `find_interrupted`, `note_interrupted`.
**Test:** `tests/test_resume_and_restart.py` — exact, singular match.
**Est. 20 min.**

### 4. `swarm/failures.py`
**Source:** `# failure classes` + `# earlier attempts` (folded together — ~160 lines combined, one coherent "what went wrong and how do we describe it" concern).
**Functions:** `scope_gap`, `failure_class`, `first_failing_line`, `attempt_lines`. Constants: `TASK_STAGES`, `NAMES_A_PATH`, `NAMES_A_SYMBOL`, `BROAD_ASK`, `HARNESS_STAGES`, `HARNESS_NOTE`, `MODEL_NOTE`, `FAILING`.
**Test:** `tests/test_failure_classes.py` (name match), plus `test_circuit_breakers.py`, `test_prompt_hygiene.py`, `test_recovery_contracts.py`, `test_shift_review.py` all reference `scope_gap`/`failure_class` — run all five.
**Est. 30 min.**

### 5. `swarm/context_pack.py`
**Source:** `# context pack`.
**Functions:** `numbered`, `outline`, `task_text`, `named_paths`, `kin_text`, `covering_tests`, `context_pack`, `study`. Constants: `CODE_PATH`, `GAME_DIR`, `DEFINITION` (and whatever else `grep` turns up in the span — re-check before starting, this section's constant list wasn't fully enumerated here).
**Test:** `tests/test_context_pack.py` — exact, singular match.
**Est. 25 min.**

### 6. `swarm/roles.py`
**Source:** `# roles`.
**Functions:** `persona_text` (this is nearly the entire section — one ~230-line function, not many small ones).
**Test:** **none found.** No test file references `persona_text` directly. This is the one packet in the shift with no automated safety net. Before moving it, manually capture the output of `persona_text(name, c)` for a couple of representative `(name, c)` pairs (pick 2–3 roles from `READ_ONLY_ROLES` and 2–3 that aren't), diff byte-for-byte after the move. Do not skip this just because the test suite stays green — the suite genuinely cannot catch a regression here.
**Est. 30 min** (the extra time is the manual diff, not the move itself).

### 7. `swarm/flint_turn.py`
**Source:** `# flint` section.
**Functions/classes:** `CapReached`, `NoCredits`, `StepLimit`, `ModelError`, `ReviewFormatError`, `AgentTimeout`, `ExplorationExhausted`, `Stopped`, `ProviderDown`, `ProviderBusy`, `ModelGone`, `RequestBudget`, `ModelPoolExhausted`, `turn_timeout`, `rest`, `next_wake`, `all_resting`, `read_charges`, `route_to_paid`, `flint` (the function that shells out to `flint.py` per turn — this is swarmd's own wrapper, not the harness file itself).
**Test:** no single dedicated file; this is deep plumbing the `worker` section (not yet extracted) calls constantly. Run the full suite, not a targeted subset — if anything's going to catch a problem here, it's the integration-shaped tests (`test_implementer_rounds.py`, `test_paid_fallback.py`, `test_providers.py`).
**Note:** `worker` (staying in `swarmd.py` this shift) will need `from swarm.flint_turn import flint, route_to_paid, ...` — this is the packet most likely to reveal a dependency packet 0 missed. Watch for it.
**Est. 40 min.**

### 8. `swarm/queue.py`
**Source:** `# queue` section (the biggest leaf, ~560 lines, almost entirely one class).
**Functions/class:** `_read`, `_write`, `estimate_requests`, `class Queue`, `queue_for`. Constant: `STATIC_REQUEST_ESTIMATES`.
**Test:** no single dedicated file — `Queue` is imported directly by nearly 20 test files (`test_attempt_latency.py`, `test_bug_reports.py`, `test_circuit_breakers.py`, `test_config_resolution.py`, `test_daemon_control.py`, `test_dismissed_tasks.py`, `test_goal_priorities.py`, `test_mit_bridge.py`, `test_paid_fallback.py`, `test_panel_data.py`, `test_provisioning.py`, `test_recovery_contracts.py`, `test_reliability.py`, `test_resume_and_restart.py`, `test_roadmap_dispatch.py`, `test_shift_review.py`, `test_swarm_learning.py`, `test_weakened_tests.py`...). Run the full suite; this is the packet where "full suite green" is doing all the work, not a targeted file.
**Est. 45 min** — biggest single leaf, budget the extra time.

### 9. `swarm/account.py`
**Source:** `# account`.
**Functions:** `account`, `sync_usage`, `allowance_recheck`, `resync_allowance`, `recheck_allowance`, `free_tool_models`.
**Test:** `tests/test_paid_fallback.py`.
**Est. 25 min.**

### 10. `swarm/planning.py`
**Source:** `# planning`.
**Functions:** `serving_the_goal`, `plan`.
**Test:** `tests/test_goal_priorities.py`, `tests/test_swarm_learning.py`.
**Est. 20 min.**

### 11. `swarm/spare_time.py`
**Source:** `# spare time`.
**Functions:** `json_object`, `idle_state`, `write_idle`, `settle_idle`, `idle_ready`, `idle_improvement`.
**Test:** `tests/test_shift_review.py`.
**Est. 25 min.**

### 12. `swarm/bug_reports.py`
**Source:** `# player bug reports`.
**Functions:** `bug_task`, `import_bugs`.
**Test:** `tests/test_bug_reports.py`, `tests/test_paid_fallback.py`.
**Est. 15 min.**

### 13. `swarm/init_probe.py`
**Source:** the **init-probe half** of `# surroundings (swarm init)` — not the whole section, it's mislabeled/overloaded (see packet 17).
**Functions:** `probe_surroundings`, `build_prov_block`, `render_surroundings`, `cmd_init`, `git_branch`.
**Test:** `tests/test_provisioning.py` — same file as packet 1, this is the other half of the same feature.
**Est. 30 min.**

### 14. `swarm/report.py`
**Source:** `# report`.
**Functions:** `build_report`.
**Test:** `tests/test_records.py`, `tests/test_swarm_learning.py`.
**Est. 15 min.**

### 15. `swarm/setup.py`
**Source:** `# setup`.
**Functions:** `detect_test_cmd`, `embedded_repos`, `_subdirs`, `sub_projects`, `test_cmd_hint`, `test_cmds`, `project_test_cmd`, `other_test_cmds`, `scaffold`, `configure`, `why_unrunnable`, `config_warnings`, `warn_about_config`, `preflight`, `keep_awake` (this last one is the macOS `caffeinate` call — see "what breaks first" #5, don't fix it here, just move it).
**Test:** `tests/test_attempt_latency.py`, `tests/test_paid_fallback.py`, `tests/test_resume_and_restart.py`, `tests/test_swarm_learning.py`.
**Est. 35 min.**

### 16. `swarm/launchd.py`
**Source:** the **service half** of `# launchd service` — not `cmd_stop`/`cmd_wake`/`cmd_models`/`main()`, those stay (packet 17/the eventual `main()` glue).
**Functions:** `service_label`, `service_plist_path`, `service_plist`, `launchctl`, `cmd_service`.
**Test:** `tests/test_daemon_control.py`.
**Est. 20 min.**

### 17. `swarm/cli_commands.py`
**Source:** `# commands` + the **CLI-dispatch half** of `# surroundings (swarm init)` + the remaining bits of `# launchd service`.
**Functions:** `_setup`, `cmd_grind`, `cmd_run`, `cmd_status`, `cmd_report`, `cmd_add`, `cmd_plan`, `cmd_stop`, `cmd_wake`, `cmd_models`.
**Test:** `tests/test_provisioning.py`, `tests/test_swarm_learning.py`, `tests/test_daemon_control.py`.
**Note:** `main()` (the argparse wiring) stays in `swarmd.py` — it's the thin glue that imports everything, including this module. Don't move it; it's what's left holding the pieces together.
**Est. 35 min.**

---

## Packet 18 — cleanup sweep (do last)

1. Delete the now-vestigial empty `# corpus` section header (it was already empty before this shift — one line, no content, dead weight).
2. Re-grep the whole repo (`grep -rn` across every `.py` file, not just `tests/`) for any remaining bare reference to a moved symbol that still points at `swarmd` instead of the new module — this catches anything packet-level checks missed.
3. Confirm `source_files()` (packet 0) lists exactly the files that now exist — delete any speculative entries for modules that ended up merged into a sibling (e.g. if packet 16/17 ended up as one file instead of two, fix the list).
4. Full suite, one more time, record the final count next to this doc's baseline.
5. `wc -l swarm/swarmd.py` — record the before/after. Expect somewhere around 2,900–3,100 lines remaining (worker + daemon + plumbing/config/spend/goal + `main()`), down from 6,152.
6. One commit: "swarmd.py de-monolith shift 1 complete — N lines moved across 17 files."

**Est. 30–45 min.**

---

## What's explicitly NOT in this shift (shift 2+, not detailed here)

- **`# worker`** (~1,295 lines) — the per-task attempt loop, bandit/model selection, retry-then-split logic. The actual orchestration core. This is where the real coupling lives and where the review's "VECTOR stops at depth 2" complaint is aimed. Don't touch it until everything above has landed and the remaining file is small enough to see its seams clearly — this will very likely split further into something like a bandit-selection module and a turn-sequencing module, but that's a decision for whoever's staring at ~1,300 lines in isolation, not something to pre-guess from this doc.
- **`# daemon`** (~523 lines) — the `grind`/`run`/`stop`/`status` CLI-facing lifecycle. Depends on `worker`; do it after.
- **The remaining `# plumbing` split** — config resolution (`repo_slug`, `per_repo_config`, `config_path_for`, `load_cfg`, `use_config`, `use_repo`, `pool`, `paid_pool`, ~17 functions), spend/budget math (`spend_file` through `quota_exhausted`, ~13 functions), and goal parsing (`goal_items`, `goal_item_weight`, `read_goal`) are three distinct concerns currently living together in one 532-line section, only partially extracted into packet 0 (just the truly cross-cutting low-level bits). These weren't pulled apart this shift because the leaf sections above take `c` as an explicit parameter rather than calling `cfg()` themselves — they didn't need it. `worker`/`daemon` do call these directly, so this split makes more sense done alongside shift 2, not before it.
- **Note for whoever scopes shift 2:** there's already a `swarm/budget.py` (flint's own per-request RPM/daily-cap enforcement, keyed on `FLINT_HOME`) and a `swarm/wallet.py` (the editor's paid-fallback wallet). `swarmd.py`'s own `spend_*` cluster is a *third*, related-but-distinct concern (the daemon's per-repo cap/reserve tracking against `spend_file(c)`). Don't merge these three without understanding why they're separate first — they answer different questions for different callers.
