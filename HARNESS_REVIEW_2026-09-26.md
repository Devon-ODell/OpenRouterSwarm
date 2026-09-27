# Harness review: why the studio swarm lands 1 attempt in 10, and what to change

Written 2026-09-26 from the openRouter-Studio swarm's own records. It is for the agent working on
this repository (`/Users/devonodell/Desktop/OpenRouterSwarm`): flint, `swarmd`, the bridge and the
Cursor extension. Every claim below cites a record you can re-read, and every change names a file,
a line, a test and a measurable finish line.

## Before you start

- **Someone else's change is in progress.** The working tree has an uncommitted `allowance_recheck`
  feature (`swarm/swarmd.py`, `swarm/SWARM.md`, `swarm/config.example.json`,
  `tests/test_paid_fallback.py`). Finish and commit it first (item 10), or build on top of it. Do
  not discard it. Line numbers in this document are from that working tree.
- **Run the tests like this, and only like this:**
  `FLINT_HOME=$(mktemp -d) .venv/bin/python -m unittest discover -s tests`.
  flint.py loads the real `.env`, so a test that does not patch `swarmd.account()` calls OpenRouter.
- **Never start a real daemon to test a change.** The studio swarm spends real money (paid pool, $20
  a month shared across repos, about $6 left until 2026-10-05).
- **The target repository has an owner file.** `openRouter-Studio/GOAL.md`, section "Current owner
  requirements", overrides older instructions. Only item 14 touches that repository. Coordinate
  before editing it.

## The evidence

Source: `swarm/state/openRouter-Studio-ab0a4a/` (`journal.jsonl`, `attempts/*/attempt.json`,
`done.jsonl`) and `swarm/logs/openRouter-Studio-ab0a4a/`. Window: 2026-09-25 09:39 to
2026-09-26 14:42.

| measure | value |
|---|---|
| attempts (`attempt` rows) | 189 |
| landed | **19 (10%)** |
| `no_change` | **95 (50%)**. Of these: 34 tool calls written as text, 33 hit the step limit, 27 read files and never edited, 1 made edits that cancelled out |
| `model_error` | 34 (18%) |
| `tests_failed` / `rejected` / `review_error` / `weakened_tests` / `agent_timeout` | 21 / 7 / 6 / 4 / 3 |
| finished tasks (`done.jsonl`) | 121: **46 parked, 44 split**, 30 done, 1 superseded |
| implementer turns | 229. Exit codes: 0 = 111, **5 (step limit) = 48**, 1 = 27, **6 (budget pause mid-turn) = 21**, −9 (killed) = 18 |
| daemon starts | **25 in about 26 hours** (17 on 09-25, 8 on 09-26) |
| attempts killed by a restart | **54**, 5.6 hours of attempt time ("attempt interrupted before completion") |
| quota pauses | 22, each a 10-minute sleep (`swarmd.py:1903`) |
| baseline test runs | 177, one per attempt. The studio suite now takes **105 s** (measured 2026-09-26) |
| implementer prompt size | median 16.6 KB, max 25 KB, and **no code from the files the task touches** |
| paid routing rows | 391 `paid_fallback` rows, **0** with `charge_confirmed: true` |
| what landed | 11 of 19 were studio tooling (thumbnails, word lists, WebAudio). GOAL.md's first priority, the four deep games, landed nothing: Shard Stack M1, Rune Garden M1, Cinder Hop M1 and Burrow Rally M1 were all split or parked |

The single most telling log is
`swarm/logs/openRouter-Studio-ab0a4a/w0-implementer-1790428940089493000.log`. qwen3-coder spent
12 of 12 rounds on `list_files`, `read_file` and `bash`. Then flint's tool-free final round
(`FINAL_NUDGE`, `flint.py:1059`) got the model's actual edit, written as
`<tool_call><function=edit_file><parameter=file>…` text. It was discarded: tools were off, and the
markup says `file` where the schema says `path`, so `recover_text_calls` (`flint.py:334`) would not
have matched it anyway. That one log shows the main failure: the swarm fails for **harness**
reasons far more often than for model or task reasons, and then punishes the model and splits the
task as if it had.

To measure before and after, run this. Add `if r['t'] > <deploy epoch>` to compare windows:

```sh
cd /Users/devonodell/Desktop/OpenRouterSwarm && .venv/bin/python - <<'EOF'
import json, collections
J = 'swarm/state/openRouter-Studio-ab0a4a/journal.jsonl'
rows = [json.loads(l) for l in open(J) if l.strip()]
att = [r for r in rows if r['event'] == 'attempt']
c = collections.Counter(r['stage'] for r in att)
print(len(att), 'attempts; landed', c['accepted'], f"({c['accepted']/max(1,len(att)):.0%})"); print(c.most_common())
t = [r for r in rows if r['event'] == 'turn' and r['role'] == 'implementer']
print('implementer rc', collections.Counter(r['rc'] for r in t).most_common())
EOF
```

---

## Work items, in order

P0 items change the landed rate or stop active damage. P1 items cut wall-clock time and downtime.
P2 items are UI. Each item says where to change the code, what to change, how to test it and when
it is done.

### P0-1 · Load the repository's tuned config everywhere

**Why.** The studio's tuned settings live in `swarm/configs/openRouter-Studio-ab0a4a.json`, but only
`start-studio-swarm.command` selects them, through `FLINT_SWARM_CONFIG`. Everything else reads
`swarm/config.json`: `swarmd.py:39`, `bridge.py:89` (`config_for`, which only swaps the repo path),
`bridge.py:649` (`grind-cmd`, which is how the Cursor panel's Start button launches the daemon) and
the panel's status view. The 11:32 run on 09-26 was a bare restart on the default config. It used
the pytest gate, a 600 s plan cooldown and laguna as paid fallback, and landed 0 of about 25 paid
attempts. Nothing warned.

**Change.**
1. Pull the slug computation out of `use_repo` (`swarmd.py:134`) into `repo_slug(repo)`.
2. Add `config_path_for(repo)`. Order: `FLINT_SWARM_CONFIG` if set, then
   `HERE/"configs"/f"{repo_slug(repo)}.json"` if it exists, then `HERE/"config.json"`.
   `load_cfg(repo=None)` (`swarmd.py:95`) uses it, and `save_cfg` writes back to the path it loaded
   from. Keep the path in a module global, not in the file.
3. `cmd_grind` (`swarmd.py:2536`) resolves the config from its repo argument before anything else.
4. `bridge.config_for(repo)` calls `swarmd.load_cfg(repo)`. `cmd_grind_cmd` prefixes
   `FLINT_SWARM_CONFIG=<path> ` when a per-repo file exists.
5. `run_daemon` logs `config: <path> (per-repo | default)` at startup. `swarm status` and
   `bridge.py status` return `config_path` and `config_kind`.
6. If a per-repo file exists but a different file was selected, log a warning and journal
   `config_mismatch`.

**Test.** Add `tests/test_config_resolution.py` with a temporary `configs/` directory. Check that the
environment variable wins; that the per-repo file is chosen by repo path; that the default is the
fallback; that `bridge.config_for(studio)` returns the per-repo `test_cmd`; and that `grind-cmd`
output starts with `FLINT_SWARM_CONFIG=`.

**Done when** `.venv/bin/python swarm/bridge.py status --repo /Users/devonodell/Desktop/internetmoney/video-games/openRouter-Studio`
reports `config_kind: per-repo` with no environment variable set.

### P0-2 · Make "Stop" stop one swarm, not all of them

**Why.** `cmd_stop` (`bridge.py:697`) sends SIGINT to every process whose arguments match
`swarmd.py (grind|run)`, machine-wide. The panel's Stop button on the studio also stops the
hedge-fund, kraken and other swarms.

**Change.** Require `--repo`. After `use_repo`, read `STATE/daemon.pid` (written by `swarmd.start`,
`swarmd.py:2364`). Check that the pid is alive and that its arguments contain `swarmd.py` and the
repo path, then signal that pid only. Keep the old behaviour behind `--all`. `extension.js`
`stopGrind` passes the repository.

**Test.** Start two dummy processes:
`Popen([sys.executable, "-c", "import time; time.sleep(60)", "swarmd.py", "grind", repoA])`, and the
same with `repoB`. Write the repo A pid file, stop A, and assert that B is still alive.

### P0-3 · Give the implementer the code, and a round budget it can see

**Why.** 60 of the 95 `no_change` attempts ran out of rounds before editing (33 step limits, 27
read-only turns that said, for example, "I exceeded my tool call limit before making any changes").
The prompt contains no code, so the model spends 6 to 11 of its 12 rounds reading. Nothing tells it
how many rounds are left. The config comment records that raising the limit to 26 rounds did not
help: at about 47 s a round, the 900 s timeout SIGKILLed those turns.

**Change.**
1. **Context pack.** Add `context_pack(task, wd, limit=20_000)` to `swarmd.py` and call it in
   `_attempt` after `swarmd.py:1576`. It collects:
   - every path in the title, detail or acceptance matching `[\w./-]+\.(py|js|json|md|html|css)`
     that exists in the worktree;
   - for any `games/<slug>/` it names: `manifest.json`, the first 60 lines of `NOTES.md`, and
     `game.js` in full when it is under 400 lines, otherwise an outline of its function and `const`
     definitions with line numbers;
   - the test files that `git grep -l <slug or basename> -- tests` finds (first two), with their
     `def test_` lines;
   - `git ls-files <dir>` for each directory it mentions.

   Print the content in `read_file`'s exact format (`{i:>5}\t{line}`), so `edit_file`'s `old_str`
   can be copied from it. Add a `{context}` placeholder to `IMPLEMENTER` (`swarmd.py:1021`) and
   `REPAIR` (`swarm/workflow.py:192`, filled at `swarmd.py:1634`).
2. **Round counter.** In `flint.py` `turn()` (`flint.py:1035`), when headless, append
   `[round k of N; M left]` to the last tool result of each round. Track `self.edits`: increment it
   in `run_tool` when `write_file` or `edit_file` returns `Created`, `Overwrote` or `Edited`. When
   3 rounds are left and `self.edits == 0`, add a user message: *"Three rounds left and no file has
   changed. Stop reading. Make the edit now with edit_file or write_file, then run the single test
   module that covers it."*
3. **Do not throw away the final edit.** In `_final_answer` (`flint.py:1062`), when the role edits
   files (swarmd sets `FLINT_FINAL_EDIT=1` for implementer and repair) and `self.edits == 0`, give
   one last round with only `edit_file` and `write_file` offered, before the tool-free answer. Also
   run any `edit_file` or `write_file` calls that `recover_text_calls` finds in the final answer.
4. **Parameter aliases in `recover_text_calls`.** Map `file`, `filename` and `filepath` to `path`,
   and `old` and `new` to `old_str` and `new_str`, when the tool's schema lacks the alias. Use
   `w0-implementer-1790428940089493000.log` as the fixture.
5. **A `read_files(paths)` tool.** Weak models make one call per round. Cap its output at
   `MAX_TOOL_OUTPUT`, register it next to `read_file`, and say in the system prompt that it takes
   several paths at once.

**Test.** flint unit tests with a scripted `complete()`: the round note appears; the nudge fires at 3
left only when nothing was edited; the final round offers exactly the two edit tools; and the
fixture's text markup becomes an applied edit.

**Done when**, over the next 40 implementer turns, exit code 5 is ≤ 10% (now 21%) and "read-only,
never edited" is ≤ 5% of attempts (now 14%).

### P0-4 · Stop filling the prompt with unrelated material

**Why.** Attempt `te418b5570dd3-4d356a96`, "Add onFoot walk state … vehicle for Crosstown", has
these excerpts in its prompt: an MIT IDS.333 lecture on parking-garage real options, a Python
`Vehicle` class homework with answers, and an MIT 14.41 public-finance transcript about a police
vehicle. They matched on the word "vehicle". SWARM.md says excerpts are injected "only when a
lecture card matches the task", but one weak card match unlocks source pages from other courses.
The studio config sets `mit_experiment.enabled: false`, and `mit_arm` (`swarmd.py:738`) returns
"on" in that case, so 187 of 189 attempts got the corpus. The "HUNG FROM THE RAFTERS" wall adds
4.1 KB of old diffs from unrelated tasks, and "EARLIER ATTEMPTS" is
`json.dumps(sorted(prior)[-3:])[-6000:]` (`swarmd.py:1576`): a JSON dump cut mid-string, made of
paths the model cannot use.

**Change.**
1. Add config `inject_corpus` (default `true`, for other repos). When false, `mit_arm` returns
   `"off"`. Set `"inject_corpus": false` in `configs/openRouter-Studio-ab0a4a.json`.
2. In `mit_corpus.study(..., require_card=True)`: a card must match at least 2 distinct non-stopword
   query terms. Source pages must come from the matched card's course and lecture. Never inject
   pages under `references/transcripts/` unasked; the `study` tool can still return them.
3. `format_playbook` (`swarm/learn.py:440`): at most one rafters exhibit, only when its files
   overlap the task's paths, and at most 600 characters of diff.
4. Replace the EARLIER ATTEMPTS dump with up to 3 lines of the form
   `- <date> <stage> on <model>: <classified reason> (<first failing test line>)`, 1,200 characters
   at most.
5. Order the prompt so recent text is the task: one-line task summary at the top, then goal, context
   pack, playbook, earlier attempts, and finally the full task contract, rules and acceptance.

**Test.** Use the query built from `te418b5570dd3-4d356a96`'s title and detail: it must inject
nothing. Check that `mit_arm` returns `"off"` with `inject_corpus: false`, and that the
earlier-attempts text is at most 1,200 characters and is not JSON.

**Done when** the median implementer prompt, excluding the context pack, is ≤ 7 KB. Measure with
`wc -c swarm/state/openRouter-Studio-ab0a4a/attempts/*/prompt-01-implementer.txt`.

### P0-5 · Classify a failure before punishing a model or splitting a task

**Why.** `Queue.release` (`swarmd.py:561`) treats every failure the same way. It adds a note,
increments `attempts`, and after `MAX_ATTEMPTS = 2` splits or parks the task. So harness failures
(text calls, step limits, sandbox timeouts, restarts) cost tasks their lives: 90 of 121 finished
tasks were split or parked, including every deep-game milestone. The bandit also scores those
failures against the model with weight 2. Some decompositions return zero subtasks, which strands
the task and everything that depends on it.

**Change.**
1. Add a `FAILURE_CLASS` function in `swarmd.py` that maps each stage and note to one of three
   classes:
   - `task`: `tests_failed`, `rejected`, a real `weakened_tests` (see P0-6);
   - `model`: malformed output, `review_error`, text markup that could not be recovered,
     `no_change` after at least one edit call;
   - `harness`: step limit or timeout with no diff, sandbox or process errors, interrupted or
     deferred attempts, provider `model_error` (5xx, empty response).
2. `release(tid, ok, note, failure_class=...)`: a `harness` failure does not increment `attempts`
   or append a note. Increment `harness_failures`, defer 120 s, and park after 3 with the note
   `needs harness look: <reason>`. Only `task` failures count toward `MAX_ATTEMPTS` and splitting.
3. `split_now=self.agent_timed_out` (`swarmd.py:1891`) applies only when a diff existed.
4. A decomposition with zero subtasks retries once on a different model, then parks. It does not
   strand dependents on the first try.
5. `_reinforce` (`swarmd.py:1717`) skips the implementer update for `harness` failures.

**Test.** Three `harness` releases leave `attempts` unchanged and park on the third; `decompose` is
not called; the ledger is not updated for the implementer.

**Done when** split plus parked is < 40% of finished tasks (now 74%).

### P0-6 · Fix the `weakened_tests` false positives (4 of 4)

**Why.** `weakened_tests` (`swarmd.py:1187`) counts removed lines containing `assert` or `expect` in
any path containing "test". It carries the heaviest penalty (weight 6) and hangs the model on the
rafters. All four hits in this window were legitimate:

| studio branch | task | what the diff did |
|---|---|---|
| `swarm/t2788f961b026-e1659a98` | Remove star-catcher and reversi games | deleted the games and their tests, as the owner asked |
| `swarm/t2788f961b026-f6e6d0ad` | same | same |
| `swarm/t57ad75a4a71d-ee5a3053` | "I want you to remove star catcher and reversi, add Chess…" | the owner's own request |
| `swarm/t1bf36c30ecd7-120eeee2` | Verify shard-stack test assertions match 8x15 well | changed `b[112]` to `b[80]`: an edited assertion, not a removed one |

The owner's removal request failed three times in the swarm and was then done by hand.

**Change.**
1. Count per test file. A removed assertion paired, in the same hunk, with an added line calling
   the same assertion function is an edit, not a removal. Flag only a net loss of assertions in a
   test file that still exists.
2. Do not count removed assertions whose test file is deleted along with the source it tests (for
   example, the diff deletes `games/reversi/`).
3. Add `allow_test_changes: true` on a task, settable only by human or cursor origin
   (`swarm add --allow-test-changes`, and the queue edit form). The planner and decomposer cannot
   set it. The adversary prompt then says: "The owner authorised test changes; check they match
   the request."

**Test.** Save the four diffs as fixtures:
`git -C /Users/devonodell/Desktop/internetmoney/video-games/openRouter-Studio diff <branch>~1 <branch> > tests/fixtures/weakened/<name>.diff`.
All four must pass, the third one with the flag. Add a synthetic diff that deletes one assertion
with no replacement; it must still flag.

### P1-7 · Restart without losing work

**Why.** There were 25 daemon starts in about 26 hours, most to apply a config change ("These
settings … take effect when the daemon next starts", SWARM.md). Each start killed the attempt in
flight: 54 of them, 5.6 hours of work.

**Change.**
1. **Hot reload.** Before each `claim` in `Worker.run` (`swarmd.py:1883`), check the config file's
   modification time. If it changed, reload these keys: `models`, `paid_models`, `steps`,
   `role_timeouts`, `turn_timeout`, `test_timeout`, `max_repairs`, `reviewer_exclude`,
   `plan_cooldown`, `allowance_recheck`, `monthly_usd`, `owner_window`, `max_queue`, `corpus_*`
   and `inject_corpus`. Log `config reloaded: <keys>` and journal `config_reload`. For `repo`,
   `trunk`, `workers` and `sandbox_write`, log `restart needed for <key>` instead.
2. **Drain.** `Tally.drain` already stops workers between tasks (`swarmd.py:1306`). Near
   `swarmd.py:2376`, register SIGUSR1 to set it, then add `swarm stop --drain` and
   `bridge.py stop --repo P --drain`.
3. **Resume (optional).** At startup, re-enter `_attempt` at the candidate gate for an interrupted
   attempt whose branch still has a diff ("cleanup deferred" in the log), instead of implementing
   it again.

**Done when** a config edit takes effect without a restart, and a drained stop leaves no
"attempt interrupted" rows.

### P1-8 · Survive a reboot (opt-in)

**Why.** After today's reboot nothing came back until a person asked, and the untuned config is the
easy way to bring it back (see P0-1).

**Change.** Add `swarm service install --repo P [--hours H] [--autostart]`. It writes
`~/Library/LaunchAgents/com.flint.swarm.<slug>.plist` with:
- `ProgramArguments`: `.venv/bin/python swarm/swarmd.py grind <repo> --goal GOAL.md`
- `EnvironmentVariables.FLINT_SWARM_CONFIG`: the resolved per-repo path
- `WorkingDirectory`: this repository
- `StandardOutPath` and `StandardErrorPath`: `swarm/logs/<slug>.out`
- `KeepAlive`: `{SuccessfulExit: false}`
- `RunAtLoad`: only with `--autostart`

Load it with `launchctl bootstrap gui/$(id -u) <plist>`, and add `swarm service status|uninstall`.
Autostart must stay opt-in: this daemon spends money.

### P1-9 · Cut the wait per attempt

**Why.** Every attempt runs the full suite before the model starts (`swarmd.py:1542`), and the
studio suite now takes 105 s. The implementer prompt says "Run it yourself and keep working until
it does", so the model also runs the whole suite inside its turn. flint's `bash` tool times out at
120 s (`flint.py:191`), so that run is 15 s from being cut off. 21 implementer turns stopped
mid-turn on a local budget pause (exit code 6).

**Change.**
1. **Baseline cache.** Key: `sha1(base_commit + test_cmd + validation_commands)`. Store
   `{passed, at, seconds}` in `STATE/baseline.json`. Skip the gate after a pass within 24 hours,
   and record `baseline: cached pass for <sha>` in the attempt evidence.
2. **Prompt.** Tell the implementer to run the narrowest covering test, such as
   `python3 -m unittest tests.test_table_games -q`, taken from the context pack's test list, and
   that the supervisor runs `{test_cmd}` afterwards.
3. **`bash` timeout.** Make it `int(os.environ.get("FLINT_BASH_TIMEOUT", 120))`, and have swarmd
   set it to `test_timeout` for implementer and repair turns.
4. **Budget check before the turn.** Check the budget for the whole turn (`steps` requests) before
   it starts. If it cannot fit, route to paid at the start rather than pausing mid-turn.

**Done when** median wall time of accepted attempts drops by ≥ 100 s, and no implementer log
contains "command timed out after 120s".

### P1-10 · Stop sleeping 10 minutes at a time, and land `allowance_recheck`

**Why.** `CapReached` sleeps 600 s (`swarmd.py:1903`), which happened 22 times. The local counter
can read 1025/1000 while OpenRouter reports fewer requests used.

**Change.** Finish, test and commit the in-progress `allowance_recheck` diff. Then call
`recheck_allowance(c, budget)` before that sleep, and sleep `min(600, allowance_recheck or 600)`.

### P1-11 · Reward the goal, not whatever is easiest

**Why.** The judge gave 0.94 to "Add validation for thumbnail data URL format" and 0.97 to "Add
Configurable Content Word Lists", while the milestones GOAL.md puts first died. Those rewards train
the planner toward tooling: the hardener persona landed 10 of 52, the scholar 0 of 10. The judge
(`swarmd.py:1083`) is asked about "impact" but never sees the ordered priorities.

**Change.**
1. Add `goal_items(goal_text)`. It returns the numbered list under the first heading matching
   `(?i)work on now|priorities`, or `[]` if none.
2. **Judge.** Show the list and require `"goal_item": <n or 0>`. In `_reinforce`, multiply the
   landed component of the reward by `{1: 1.0, 2: 0.85, 3: 0.6, 4: 0.5, 0: 0.35}`, and store
   `goal_item` on the `attempt` row.
3. **Planner** (`swarmd.py:1117`). Each task must carry `"serves": n`. `plan()` drops tasks that
   serve no listed item and logs how many it dropped. At least half of each batch must serve
   item 1.

**Done when** at least 60% of commits landed in a 24-hour window serve item 1 or 2.

### P1-12 · Records that say what actually happened

1. **Errors.** At `swarmd.py:1924`, `err=str(e)[:500]` stores the start of the sandbox command. All
   12 "sandbox-exec" errors were really "timed out after 420 seconds", hidden past character 500.
   For `TimeoutExpired` and `CalledProcessError`, record `kind`, `timeout`, `returncode` and the
   last 1,000 characters of stderr. Never record argv.
2. **Prompt by file.** `swarmd.py:901-902` passes the 10–25 KB prompt as `-p <prompt>`, which puts
   it in argv, in `ps` and in every exception. Add `--prompt-file` to flint's argument parser and
   pass the `prompt-NN-<role>.txt` the attempt already writes.
3. **Charges.** flint `_charge` (`flint.py:765`) knows `usage.cost`. Write it to `FLINT_CHARGE_FILE`,
   journal a `charge` event (role, model, attempt, usd), and put the sum on the `attempt` row as
   `usd`. The report should show cost per landed commit and per model. Today that cannot be
   computed.
4. **Report** (`build_report`, `swarmd.py:2472`). Hide the MIT section when no experiment is running.
   Under "Split or parked", show the P0-5 class plus one line, not raw log fragments such as
   `_file tool: read_file round 3/26…`.

### P2-13 · Cursor panel: make its first line say whether the swarm is working

Today `statusHtml` (`cursor-extension/media/panel.js:21`) is a list of counters: free requests,
spend meter, wallet, OpenRouter usage, MIT chunk count, experiment arms. None of them says what is
happening or whether it is going well.

1. **Now line.** The daemon writes `STATE/now.json` on every phase change: task id and title,
   role, the model actually running, round k of N, start time and waiting reason. `bridge status`
   includes it. The panel's top line reads, for example:
   `Implementing "Add onFoot walk state…" · qwen3-coder · round 7/12 · 4m10s`, or
   `Waiting for the free allowance · back 20:00`, or `All models resting · first back 14:32`.
2. **Health row, last 24 hours:** attempts, landed, landed %, the top three failure classes, $
   spent and $ per landed commit. Red when landed % is below 15 over at least 10 attempts.
3. **Config badge:** the loaded config path. Red when it is the default while a per-repo file
   exists.
4. **Buttons:** Stop now; Stop after this task (drain); Restart (drain, then start with the same
   config).
5. **Queue tab:** attempts, last failure class, `not_before` countdown and **Retry now** per task.
   A Parked section with **Requeue** and **Why** (opens the attempt's `HANDOFF.md`).
6. **Landed tab:** the last 20 trunk commits with model, reward and goal item. Clicking one opens
   `git show <sha>` in a diff editor.
7. **Remove:** the MIT chunk count and experiment arms unless an experiment is enabled. Rename
   "locally recorded response costs" to "$ spent this month (OpenRouter-reported)".
8. **Refresh:** every 10 s while a daemon runs (`bridge status` takes 0.3–0.5 s), 60 s otherwise.
   Today it is always 60 s (`extension.js:720`).

### P2-14 · The arcade on port 8000 (studio repository; coordinate before editing)

1. **Player bug reports should reach the swarm.** `studio/arcade.py:14` says `reports/bugs.jsonl`
   is there "so a person or the swarm can read them", but nothing in `swarm/` reads it. Add
   `bridge.py import-bugs --repo P`: new rows past a cursor in `STATE/bugs.cursor` become tasks with
   `origin: "player"`, priority 1, title `Bug in <game>: <summary>`, and the seed, build revision
   and steps in the detail. Call it from the daemon's tick.
2. **The preview bar names the wrong commit.** `site/arcade.js:296` prints `status.swarm_commit` as
   the "New tested build", even when the change came from `main`. Add
   `changes: [{sha, subject, source: "main" | "swarm"}]` to `/api/preview` (from `git log old..new`
   on both refs in `studio/preview.py`). The bar should list the subjects, show elapsed time while
   `state == "testing"`, and show the first failing test line on failure.
3. **Swarm chip.** Add a swarm chip to the top bar, fed by `/api/swarm` (arcade.py calls
   `bridge.py status --repo`, cached 10 s), for example:
   `Swarm: implementing Crosstown G2b · 3 landed today · 6 queued`.

### P2-15 · Config hygiene, plus one decision for the owner

1. `configs/openRouter-Studio-ab0a4a.json` has a 2,000-character `_comment` changelog that no longer
   matches its values. It says the pool was "cut to the two models that have paid counterparts"
   and that "qwen3-coder-30b-a3b-instruct leads paid_models". The file actually lists three paid
   models, and `paid_models[0]` is `qwen/qwen3-coder`. Move that history to
   `swarm/configs/openRouter-Studio-ab0a4a.CHANGELOG.md` and keep the JSON as values.
2. **Validate at startup.** Warn on unknown keys. Warn when `steps × recent seconds per round >
   role timeout`. Warn when `models` contains no `:free` model while `allow_paid` is true.
3. **Owner decision (ask; do not change silently).** The studio's `models` list is
   `qwen/qwen3-coder`, `moonshotai/kimi-k2` and `deepseek/deepseek-chat`: all paid. With
   `allow_paid: true`, `pool()` (`swarmd.py:150`) makes **every** studio turn paid, even with the
   free allowance untouched. Studio GOAL.md says the studio runs "on free OpenRouter models". Show
   this in `swarm status` and ask the owner which is intended.

---

## What not to do

- **Do not raise `steps.implementer` to fix step limits.** 26 rounds at about 47 s each overran the
  900 s timeout and got turns SIGKILLed (see the config comment). Give the model the code and a
  visible budget instead (P0-3).
- **Do not add models to fix the landed rate.** qwen3-coder has the best record (12 landed of 64
  attempts), and 34 of its attempts still ended `no_change`. The pool is not the constraint.
- **Do not loosen the test gate or the adversary** to raise the landed rate. P0-6 makes the
  weakened-tests check more accurate, not more lenient.
- **Do not change the studio repository** outside P2-14, and not without coordinating.

## Finish line

Deploy P0-1 through P0-6, let the swarm run 40 attempts, and rerun the measurement snippet above.
Targets:

| measure | now | target |
|---|---:|---:|
| landed | 10% | ≥ 25% |
| `no_change` | 50% | ≤ 20% |
| implementer exit 5 | 21% of turns | ≤ 10% |
| split + parked | 74% of finished tasks | < 40% |
| attempts killed by restarts | 54 in 26 h | 0 (drain and hot reload) |
| cost per landed commit | unknown | reported per commit and per model |
