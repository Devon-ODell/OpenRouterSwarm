# SkyRing: model guidance and run findings

Written 2026-10-11 from the first SkyRing run (daemon pid 64328, started 15:42, harness 53c7846).
Everything here was checked against logs, attempt records or code. Line numbers are as of 53c7846.

## What the models are taught, and where

Implementers see the repository's playbook (`swarm/state/SkyRing-24c77a/learn.json`, rendered by
`learn.format_playbook`). Up to three **pinned** lessons always appear first. The remaining slots, up
to six in total, go to the best-ranked earned lessons. Lessons shown in an attempt share that
attempt's reward, so their value is measured. Reviewers and judges never see the playbook.

Playbook as of 16:50 (revision 2; revision 1 was installed at about 16:30). Backups:
`learn.json.before-20261011-guidance`, `GOAL.md.before-20261011-guidance` in the state directory.

| id | pinned | axis | text (abridged) | evidence |
|---|---|---|---|---|
| Lcfabe00a | ★ | speed, cost | Don't re-read files shown in full; for "definitions only" files read just the needed ranges (`read_file` offset/limit, `read_files`). Write the target file by round 3 (no edit by round 8 stops the turn); batch fixes into one `write_file` | 7 ir-validator attempts read files the prompt already held and died with no edits. Rev 1 said "never re-read", which is wrong for outline-only files: hit-arbitration (1105-line `player.rs` given as definitions only) spent 5 of 8 rounds on `search`. The evidence task spent 5 rounds on single `edit_file` calls and ran out of rounds |
| Lfc9fb101 | ★ | speed, cost | Narrow SkyRing test commands; ignore `tests.test_table_games`; the supervisor runs verify.py | `prompts.py:118` example module doesn't exist here; cargo is not on PATH. Both commands verified; `core/Cargo.lock` stays clean |
| L15a58b3b | ★ | accuracy | Test every rule in the task detail, not only the acceptance list; compute expected values (`hashlib`) from the bytes the test wrote | The accepted IR validator accepts `source: "owner"` with a null fingerprint (`tools/combat_ir.py:47`), which the spec forbids. The evidence test hard-coded the SHA-256 of `"Hello, World!"` and failed gate 1 |
| L7be56758 |   | accuracy | ABI changes are append-only, mirrored in `er_combat.h` | Unpinned in rev 2: both stamina packets spell this out in their detail. It still shows through ranking |
| La0e70e7c |   | accuracy | Run deterministic traces from fresh ErCombat instances | Restored from the first task's lost judge reply (bug 3) |
| L66c5b893 |   | — | Earned by the swarm from the ir-validator judge | — |

Lcfabe00a's stats were reset (mean 0.7, n 0) when its text changed, so its record measures the
current wording only.

### Reviewers and judges: the goal text

Reviewers and judges don't see the playbook, but all 7 prompt templates include `{goal}`, read
for each task from `swarm/state/SkyRing-24c77a/GOAL.md`. An "Answer discipline" section was
appended there (1332 characters in total). It contains no heading containing "priorities" or
"work on now", so `task_rules.goal_items` still returns `[]`. It covers:
- **Consistency:** exactly the requested JSON, with every bracket closed.
- **Review format:** findings hold defects only; severity must be blocker, major or minor.
- **Accuracy:** reviewers check every rule in the detail.
- **Follow-up quality:** judges put files and a runnable check in the follow-up's detail.
- **Honesty:** don't claim to have run commands you didn't run.

To inspect: `python3 -c 'import json;print(json.dumps(json.load(open("swarm/state/SkyRing-24c77a/learn.json"))["lessons"],indent=1))'`.
To change one, edit that file while holding `learn.lock`, or go through `learn.Ledger`.
The lowercase identity `skyRing-988b86` has no ledger, so running there starts without these lessons.

## Run so far (15:42–16:30)

- **Landed:** `tecd4b3f26215` byte-stable trace test, 22 requests, about 6 min. Independently re-run on
  `swarm/trunk` (5311b95): `cargo test --test determinism` passes.
- **Stalled:** `t8f534e27a14d` ir-validator, 8 attempts, no edits yet. The implementer role is
  limited to dots and laguna (`role_models.implementer`). Laguna was rate-limited upstream on every
  one of its turns; dots failed mid-reply twice and is resting until 17:23. Meanwhile
  nemotron-3.5-lightning and nemotron-3-super answered all three of their review/judge calls with
  exit 0 (nemotron-3-ultra was never called), but no nemotron model may implement. That split is
  the largest throughput loss in the run.

## Run, 16:30–16:55

- 16:31: Flint's code changed on disk (the fix commits below). The daemon drained, then restarted
  into the new code at 16:43. Laguna's in-flight attempt (878 s, 10 requests, gate failed) was
  dropped; it predates the bug 4 fix.
- **Landed:** `t8f534e27a14d` ir-validator, implemented by nemotron-3-super in 60 s on 13 requests,
  reviewed by nemotron-3-ultra, reward 0.91. Independently checked: 7 tests pass and 13 of 14 spec
  rules hold. The miss is that owner provenance with a null fingerprint is accepted. The error says
  "Absolute damage…" (capital A); the tests compare in lowercase.
- **Exploration exhausted:** `t2eede859fa24` hit-arbitration, 8 rounds of search/read, no edit (bug 10).
- **Evidence tool:** implementer and repair both hit their round limits, gate 2 passed, and laguna was
  reviewing at 16:51.
- **Auto-queued:** `t0b288a52e583` "Integrate combat_ir validation into local verification pipeline"
  (judge follow-up, priority 0, no acceptance, no path scope; bugs 11 and 12). It would edit
  `tools/verify.py` and `verification/contract.json`, both of which have uncommitted changes in
  the SkyRing checkout.
- **All turns so far:** 18 turns, 101 requests, 41 of them on successful turns. Seconds per round:
  laguna 68.8, dots 18.0, nemotron-3-super 8.1. Laguna was rate-limited on 5 of its 6 turns;
  nemotron models hit no provider errors.

## Run, 16:55–17:10

- **Landed:** `tce1353d548e0` evidence tool (3 of 6 wave tasks done).
- **Packet defect, so I put it on hold:** `t76a32b90af3a` host-stamina-pool failed `tests_failed` after 26 requests
  (implementer plus 2 repairs). The packet can't pass as written. Appending `stamina_current` and
  `stamina_max` makes `sizeof(ErWorld)` 44, and `tools/verify.py:197` hard-codes
  `static_assert(sizeof(ErWorld) == 36)`, but `tools/verify.py` isn't in the packet's
  `allowed_paths`. The model fixed the Rust twin (`ffi.rs:652`, in scope) by candidate 2; the C
  check failed on all 3 candidates with "44 == 36". `t40eb13efcf88` stamina-spent-report has the
  same defect (`sizeof(ErOutput) == 224`) and depends on it. At 17:08 I held `t76a32b90af3a` for 24 h
  (`not_before`, with a note saying why); `t40eb13efcf88` is blocked behind it. Release with
  `TaskQueue.retry_now('t76a32b90af3a')` after choosing a fix. Either add `tools/verify.py` to both
  packets' `allowed_paths` and acceptance (this changes wave 1, so record it as wave 1b), or
  pre-commit the new expected sizes on SkyRing `main` yourself.
- **Lesson L7be56758 rewritten (rev 3):** update every size check, and stop and say so if one is
  outside allowed_paths. Stats reset.
- `skyRing-988b86.json` got `follow_ups: false` to match.
- Bug 10's fix (79cb823) was checked by tracing the new branch order at a 10-round budget: the
  70% mark gives one edit-only round; no edit in it stops the turn after round 8; an edit restores
  all tools for rounds 9–10.

## Bugs found in Flint

1. **Provider failures aren't charged to the task.** `swarm/worker.py:197-198`: the `ProviderDown`
   handler calls `q.release()` without `requests=`, `turn_seconds=` or `role_turns=`, and writes no
   `task` journal row. The ir-validator task shows `requests_spent: 0` after at least 16 started
   requests, so `task_request_cap` can't bound it and `bench/scorecard.py` can't see these attempts.
   The daily cap is unaffected because it uses a separate counter.
2. **Waiting is reported as working.** `metrics.PROVIDER_BACKOFF` is never entered. During an
   "every model is resting" wait (`worker.py:199-207`) the worker keeps its task, `started` and
   state, so `reporting.report_progress` prints "still on … (12m): implementer with laguna" while
   nothing runs, and `now.json` records the wait as `active_model` (1069 s at 16:04).
3. **Judge failure blames the wrong model.** `attempt_runner.py:149` logs the model that was
   requested, not the one that answered after the handoff (laguna was named, nemotron-3.5-lightning
   answered). That reply was missing its final `}`, so `learn.parse_scores` rightly rejected it.
   The result: no scores, no lesson, and a default reward of 0.7 sent to the bandit.
4. **Interrupted edits are only reused after a restart.** `swarmd.note_interrupted`
   (`swarmd.py:977`) runs once at daemon start. A turn killed by a provider error leaves its edits
   on a branch that later attempts in the same run never use.
5. **Deferred attempts keep their worktrees.** There are 8 worktrees for one unfinished task, and
   attempts that made zero edits still keep theirs.
6. **Outage noise in the retry prompt.** "WHAT EARLIER ATTEMPTS AT THIS TASK HIT" lists provider
   deferrals as `deferred on ?: harness — attempt interrupted before completion`: `?` for the model,
   and an outage labelled as a harness fault. That gives the next model nothing to act on.
7. **Reviewer format waste.** The first adversary filed a finding with `severity: "info"` and no
   `verification` field. `workflow.py:181-185` rejects the whole review, which cost 5 requests and
   39 s. The prompt (`workflow.py:282`) doesn't say to leave non-defects out of `findings`.
8. **Repo identity is case-sensitive.** `swarmd.repo_slug` hashes the path as typed, so
   `.../SkyRing` and `.../skyRing` are two swarms. Both now have free-only configs.
9. **Tests write to the real receipt ledger.** `~/.albatross/routes.jsonl` contains fixture rows
   (`test:free`, round costs, millisecond-apart timestamps) dated today, so it can't be used for
   accounting.
10. **The exploration guard never gives the edit-only round.** `flint.py:1612-1630`, with an
    implementer budget of 10: round 7 takes the `left == 3` branch, so the 70% check is skipped
    and fires at round 8. In that same iteration the guard sets edit-only tools and then raises
    `ExplorationExhausted`. The model never gets the round where only edit tools remain; a
    simulation of the branch order reproduces the "Stopped after 8 rounds" message exactly.
11. **Follow-ups lose their acceptance criteria.** The judge prompt asks only for title and detail
    (`prompts.py:199`), and `learn.parse_scores` keeps only those (`learn.py:222-225`). The first
    judge reply today included `acceptance_criteria`, which were dropped. `attempt_runner.follow_up`
    adds the task with no `acceptance` or `allowed_paths`, and `task_rules.scope_gap` passes a task
    whose acceptance is missing entirely (`task_rules.py:136-140`). Only `roadmap_file` disables
    follow-ups, so a run goal of "work only on the queued packets" can't stop them.
12. **The scorecard counts follow-ups as splits.** `bench/scorecard.py:56` gathers every row with
    `root == tid`. Follow-ups inherit the parent's root (`t0b288a52e583` has root `t8f534e27a14d`),
    so their attempts and requests are added to the parent's benchmark row as "split_into".

## Status (updated 2026-10-11 16:40)

- **Applied:** `nvidia/nemotron-3-super-120b-a12b:free` added to `role_models.implementer` and
  `role_models.repair` in `swarm/configs/SkyRing-24c77a.json`. Measure it on this wave before keeping it.
- **Fixed, with regression tests in `tests/test_live_run_regressions.py`:** bug 1 (6e824f0, ca65382:
  deferred attempts charge requests, turn time and role turns, and journal `attempt_deferred`, which
  `bench/scorecard.py` now counts), bug 2 (6e824f0: the wait is reported as waiting and banked as
  `provider_backoff`), bug 4 (6e824f0: a deferred attempt's branch work is banked for the task's next
  claim in the same run, and it was not offered even after a restart before, because `finish()` marks
  deferred attempts finished).
- Attempts made before 6e824f0 wrote no `attempt_deferred` row, so the scorecard undercounts this
  run's earlier outages.
- Still open: bugs 3, 5–12, and the review-prompt line below. The goal-text guidance works around
  bug 7 and part of bug 11 for this repository only.

## Status (updated 2026-10-11 18:10, after the run stopped at 17:12)

A second pass over the whole run found more bugs, numbered N1–N7, and eight stalls, S1–S8.
Each fix has a regression test in `tests/test_live_run_regressions.py` (or `test_context_pack.py`
for N1). The suite was at 1067 passing. Nothing has been committed yet.

- **N1, the root cause of S4. Rust files got a useless outline.** The outline pattern only knew
  Python and JS, so Rust files were outlined as their `let` lines: `player.rs` showed 69 `let`
  lines and none of its 61 items, and `receive_hit` wasn't in it. Both hit-arbitration attempts
  (15 requests, 0 edits) spent their rounds searching for it. Fixed: there are now Rust and C/C++
  outline patterns, and when a file is outlined, any definition the task names as code
  (`ErCombat::receive_hit`) is also shown in full. `.h`, `.c` and `.cpp` paths now count as named
  paths too.
- **Bug 1 was only partly fixed.** The stop, quota-pause, no-credits and error paths now charge
  requests, turn time and role turns, like the outage path does. `attempt_stopped` and
  `attempt_error` journal rows are written, and the scorecard counts them.
- **Bug 3 fixed.** The judge log names the model that answered.
- **Bug 5 fixed.** With `keep_worktrees: true`, only worktrees whose branch holds unlanded work
  are kept; `"all"` keeps every one. Branches are still deleted only under `false`.
- **Bug 6 and N2 fixed.** Failure notes are condensed to the lines that name the failure
  (`task_rules.condense`) instead of being cut by head or tail. The stamina note now reads
  `abi.cpp:4:31: note: expression evaluates to '44 == 36'`, not `Bundling source/chr/…`.
- **Bug 7 and S7 fixed.** "info", "note" and "nit" findings are filed as minor, and only blocker
  and major findings need a verification. The review prompt says only defects go in findings.
- **Bug 8 fixed.** Repo identity now uses the filesystem's spelling (`F_GETPATH`). A swarm that
  already has a journal under another spelling keeps its name, so `SkyRing` and `skyRing` both
  map to `SkyRing-24c77a`.
- **Bug 9 fixed.** `tests/conftest.py` sends test receipts to a temp file.
- **N3 fixed.** `other_unattributed` is computed against the local count, and `provider_at_stop`
  is re-read at stop.
- **N4 and S3 fixed.** A drain now ends the worker's backoff, idle and error sleeps
  (`Worker.rest`).
- **N5 and S6 fixed.** Judge replies go through the reviewer's `salvaged_object`. Both lost
  replies from this run now give scores, and `acceptance_criteria` is accepted for `acceptance`.
- **N7 fixed.** `pytest.ini` keeps collection to `tests/`.
- **S1 fixed.** The log and journal (`role_starved`) name idle models that `role_models` keeps
  out of a role. The worker waits for the first model that may implement. An opt-in
  `role_fallback: true` lets a model kept out of a role stand in for it. The SkyRing config
  doesn't set it.
- **S2 fixed.** A model rate-limited 3 times in the hour is passed over while a calmer model is
  free (`Ledger.busy_at`). Round lines carry the clock time.
- **S5 fixed.** The repeat check hashes only a gate failure's failing lines, so the stamina
  failures now match. A failure that repeats after a repair, and whose compiler-reported source
  line exists only outside `allowed_paths`, parks as `out_of_scope` ("packet defect") without
  blaming the model. On this run's evidence it names `plugin/src/Bridge.cpp:8` and
  `tools/verify.py:197`. So the stamina packets need **both** files in scope (or both changed on
  `main`), not only `verify.py`.
- **S8 fixed.** A signal stop records `stop_reason: "Stopped (SIGTERM)"`.
- **Stamina packets repaired (wave 1b, 18:30).** `bench/skyring/wave1b.json` adds
  `plugin/src/Bridge.cpp` and `tools/verify.py` to both packets' `allowed_paths`. The new detail
  names all three copies of each size check (ErWorld 36 → 44, ErOutput 224 → 228), and a fifth
  acceptance criterion checks them. `bench/enqueue.py` applied the wave in place through
  `TaskQueue.edit`, which can now change `allowed_paths`. The task ids are unchanged and the hold
  on `t76a32b90af3a` is lifted. The bridge still zero-initialises ErWorld, so stamina_max stays 0
  there until a later packet reads Skyrim's actor value.
- **swarm/wt leak fixed.** `workspace.cleanup` removes a repository's worktree folder once its
  last worktree is gone, and `test_attempt_stages.RepoCase` puts worktrees in its temp dir. The
  157 empty folders and 2 orphaned test worktrees from Oct 10 were removed.
- **N6 is an A/B test now.** `unscored_reward` takes `neutral` (default), `floor`, `impute`, `skip`
  or `ab` (`unscored_ab_arms`, split by task id). Each reward row records `unscored_policy`.
  `bench/unscored_ab.py` replays the policies on all journals: 45 judged and 9 unscored
  landings, with calibration plus a seeded paired Thompson replay.
  - First result: impute is calibrated (bias +0.002 vs −0.143 for neutral). It is never worse
    than neutral in 6 scenarios, and better in 5, with up to 20% less regret.
  - The pre-registered rule (v1) picked skip. Skip's win came from flaky judging landing on
    weaker models; with the rates on the strongest models, it is 18% worse. Rule v2 (written
    after seeing the data, and labelled so) picks impute.
  - Both are in `bench/history/unscored_ab.jsonl`. The owner adopted impute: it is the default
    now. `unscored_reward: "neutral"` restores the old credit.

## Recommended next changes (as originally written)

- Let at least `nvidia/nemotron-3-super-120b-a12b:free` implement (`role_models.implementer`,
  hot-reloadable). Measure it on the next wave before keeping it.
- Fix bugs 1, 2 and 4 in that order. Bug 1 distorts every benchmark number, bug 2 hides stalls, and
  bug 4 is lost work.
- Add one line to the review prompt: "Only defects go in findings; omit observations."
