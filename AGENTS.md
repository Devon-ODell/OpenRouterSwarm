# AGENTS.md — headless driver's manual for OpenRouterSwarm

This file is a **robots.txt-style operating manual for machine agents**: read it
standing, it overwrites your priors. You are allowed to read the three
doctrinal files this references (`COMMAND.md`, `swarm/SWARM.md`,
`swarm/VECTOR.md`) and to call `--help` on any command before you call the
command. What follows is all you need to drive, observe, and not break this
swarm.

## What this system is

A multi-model coding swarm that runs on free OpenRouter models and works on a
codebase until stopped. One daemon per repository ("grind"), a JSON task queue,
acceptance tests as the gate, an adversarial review before anything lands, and
accepted work merged onto a `swarm/trunk` branch in the **target repository** —
never into the working checkout. Every accepted task feeds a Thompson-sampling
bandit that picks models, planner personas and decomposed subtask shapes. This
is a *harness*: free models perform at paid-model level when the task packet is
tightly specified, and fall apart on broad asks. Everything you submit is
evaluated against that fact.

## The one capability you have: file tasks

The swarm's public input surface is deliberately small. The daemon consumes
tasks from a JSONL queue; the person-shaped way in is the CLI. Your decision
loop is:

1. **Inspect:** `swarm status --repo R` (or `bridge.py status`) → what is
   running, budget, queue, recent journal lines.
2. **Enqueue:** `swarm add --repo R <TITLE> ...` with a *packet* (below).
3. **Wait/observe:** `swarm report --repo R`, `bridge.py activity`, journal.
4. **Verify:** acceptance checks live in the repository's test suite; tests
   must pass before anything lands. Without a green suite, the swarm will
   honestly refuse to accept work.

There is no task you can hand to this swarm that is not evaluated this way, and
every task is an expensive, multi-turn, multiply-scored attempt. Treat the
queue as a budget, not a bucket.

## Ground truth and repos to target

Read `COMMAND.md` for the three "fronts" this swarm exists to serve. The repos
you are allowed to drive are exactly the ones with a tuned config in
`swarm/configs/<slug>.json`:

- **openRouter-Studio** — game studio: `/Users/devonodell/Desktop/internetmoney/video-games/openRouter-Studio` (games, arcade, wasm ports)
- **influencers** — desktop.influencers: `/Users/devonodell/Desktop/influencers` (image-chat personas)
- **kraken-bot-trainingGrounds**, **LingAI-Trader**, **hedge-fund**, others — see `swarm/configs/` and `swarm/state/`

The swarm's own repo (`/Users/devonodell/Desktop/OpenRouterSwarm`) is a fourth
target with its own tuned config. Beyond these, any Git repository can be
ground; know what you are doing.

`config_path` in status output tells you which config governs a repo. If it
says `default` while a tuned file exists, that is a warning — the tuned
settings (test command, model pool, budget) are not in effect.

## Rules of engagement (do not break the machine)

- **Never target someone's working checkout.** Swarm work happens in
  worktrees it owns. The daemon, not you, drives git; do not run git commands
  that touch `swarm/trunk` or the state directory.
- **Never edit state files.** `swarm/state/**`, `swarm/*.lock`, and
  `~/.flint/**` are the daemon's. Read-only inspection of queue/done JSONL is
  fine. The queue file is replaced atomically under a lock; writing to it
  directly will corrupt it.
- **Never edit harness config to change behavior as a shortcut.**
  `swarm/configs/*.json` tuning belongs to the owner. If a repo is blocked,
  fix the task packet (below), not the config.
- **Budget is real.** `daily_cap`/`reserve` and per-repo `monthly_usd` are
  enforced. Check `status` before and after large batches. Never queue a
  flood (max_queue is ~20 per repo; the planner may also hold work).
- **Never run two grinders on one repo** and never start a grinder without
  checking the repo is not already grinding (`daemon.running`).
- **Headless discipline:** never mix interactive flint prompts into swarm
  operations; use non-interactive invocation everywhere, and keep `--repo R`
  on every swarm command that accepts it. Follow the nonstop notes in
  `NONSTOP.md` if you use the single-agent mode (separate from the swarm
  daemon, edits the checkout directly — you should rarely need it and its
  behavior is not the swarm's).
- **Secrets:** never print `.env`, API keys, or secret env vars; sandbox rules
  block agent shell access to them anyway (agents run in a macOS sandbox;
  network stays open, so scope is containment, not security).
- **Merging:** accepted work accumulates on `swarm/trunk`; the owner merges
  into `main` by hand. You may inspect with `git log main..swarm/trunk` and
  report it; do not merge on the owner's behalf unless explicitly told.

## Forming tasks — the packet contract (best practices)

The swarm is a *scope* machine. The single biggest predictor of a landed task
is how the packet is specified. Follow the VECTOR finding: `focused` beats
`default` beats `broad`.

### Shape the packet (title + detail + acceptance)

- **Title:** the behavior to achieve, imperative, one sentence, no jargon no
  one in the repo uses. Example: `Add arcade-four: a playable Rust port of
  Connect Four compiling to wasm`.
- **Detail:** *how* to make the change in the repo's own terms — the modules
  to touch, the entry points, naming conventions, and any non-negotiable
  constraints (public API shape, no new deps, keep scope). This becomes the
  implementer's core instruction. Vague detail is the top cause of parked
  tasks. Confine the change to specific paths when practical.
- **Acceptance criteria (1–5, required, observable):** numbered checks the
  adversary must be able to verify *by running code*, not by reading intent.
  Good: `The test suite passes: <test_cmd>` / `A new test covers the win
  condition` / `arcade-four/index.html renders a 7x6 grid`. Bad:
  `improve the game` / `make it feel better` / `clean up the codebase as
  needed`.
- **The golden rule:** whether the change is accepted must be decidable by
  (a) the test command passing and (b) the acceptance criteria being
  checkable in a fresh worktree. A criterion the swarm cannot check (e.g.
  browser behavior with no browser harness in the repo) will be reported
  honestly and cannot close — see the "browser acceptance criteria" gap in
  `swarm/SWARM.md`. Put verification in the repo's own suite, invoked through
  `test_cmd`.
- **Packets must be small and single-objective.** One observable outcome per
  task. If the goal has two independent parts, split it into two tasks with
  `--depends-on`. Depth is capped (`max_depth`); decomposer-produced
  subtasks inherit the root.
- **Priority:** `--priority N` (hand-added tasks jump the queue; default 1;
  higher = first). Use priority sparingly — you are displacing real work.
  `must_run_first` floats a task above cost scoring, and is for rare
  dependencies.
- **`--depends-on TITLE_OR_ID`**: chain tasks. If a dependency dies (parked/
  split), dependents are parked with it — check `blocked` reasons in `status`
  and expect to repair the root cause, not the leaves.
- **`--allow-test-changes`**: only humans may set this (origin gate). As an
  agent you cannot and should not; a task that needs to change a test suite is
  two tasks (fix code; then adjust tests) and must be approved by the owner.
- **`--execution-class fast`**: a fully-specified mechanical edit (explicit
  path, explicit change, explicit test) skips study material/history and caps
  implementer rounds at 4 (300s). Use it for rename/refactor/format-level
  packets; it is cheaper and faster. Do not use it for anything exploratory.

### Workflow walk-through (so you know what you are paying for)

Queue → claimed by priority → fresh worktree from `swarm/trunk` → implementer
model (Thompson-sampled, prompt includes goal, task, failure notes, playbook,
and a numbered context pack of the files it names) → test gate (`test_cmd`
runs in a sandbox; existing assertions must not be lost) → adversarial review
by a different model that must end `APPROVE: …` → re-test → land on trunk →
judge scores impact/creativity/quality and writes one lesson + follow-ups →
reward updates the bandits. Failures retry once with notes, then split into
2–3 smaller subtasks (depth 1), then parked. Harness failures (timeouts,
provider issues, sandbox) cost nothing and come back in ~2 minutes. `scope_gate`
parks open-ended packets *before* any model spends a turn on them — that is the
harness agreeing with this manual.

### Time and spend budget

- `swarm run --repo R --hours N` — bounded shift; `grind` runs until stopped.
- `swarm add` tasks are capped by `task_request_cap`; a run by
  `run_request_cap` (config, default 45/300).
- Free-tier quota pauses make the daemon wait (see `pacing` in status:
  `allowed_now`, `wait_s`, `reason`). `wake` ends model rests manually — do
  not use it to bypass genuine 429/quiet periods; it just clears local cooldown
  state.
- `reserve` is the buffer the owner keeps untouched. If `budget.remaining`
  approaches zero, stop adding tasks; the daemon will gate anyway.

## CLI master reference

All paths assume the harness root is
`/Users/devonodell/Desktop/OpenRouterSwarm` (use the repo-relative forms
below). `.venv/bin/python` is the interpreter. `swarm`/`flint` may also be on
`PATH` after `setup.sh`; use the full python invocation when in doubt.
`--repo R` selects the tuned config and state; omitting it uses the default
`config.json` and emits a `config_mismatch` warning — always pass it.

### Daemon lifecycle (`swarm/swarmd.py`)

```sh
# Start / stop / status
.venv/bin/python swarm/swarmd.py init   <repo>          # write tuned config + prov hooks (idempotent)
.venv/bin/python swarm/swarmd.py init   --check <repo>  # surroundings map only; writes nothing
.venv/bin/python swarm/swarmd.py grind  --repo R [--goal "…"] [--test-cmd "…"] [--hours H] [--workers N] [--max-tasks N]
.venv/bin/python swarm/swarmd.py run    --repo R [--hours 24] [--workers N] [--max-tasks N]   # bounded
.venv/bin/python swarm/swarmd.py stop   [--repo R] [--drain]                                   # drain = finish in-flight task
.venv/bin/python swarm/swarmd.py status --repo R        # daemon, queue, budget, pacing, journal tail — start here
.venv/bin/python swarm/swarmd.py wake                   # clear model cooldown rests (this repo's daemon)
.venv/bin/python swarm/swarmd.py service install --repo R [--hours H] [--autostart] | status | uninstall   # launchd; autostart is OFF unless asked
```

### Provisioning (`swarm init` and the `prov` block)

A fresh git worktree contains only tracked files — no `node_modules/`, no
`.venv/`, no vendored or nested-repo content. A test gate that needs any of
those used to fail on a bare checkout before a single model request (the
`Cannot find package '@strudel/core'` / "every task would be rejected"
baseline failures). `swarm init <repo>` fixes that permanently:

- **Probes the surroundings** — nested git repos, runtime markers
  (`package.json`, `requirements.txt`, …) searched one level deep too, `.venv`,
  `node_modules/`, `.gitignore` — and prints a human-readable map with
  `init --check <repo>`.
- **Writes `swarm/configs/<slug>.json`** with a `prov` block: install hooks the
  harness runs inside every fresh worktree (and the `_view`) before the test
  gate. Existing tuned configs are never overwritten (idempotent).
- **Exact dj-bot fix this encodes:** `spike/package.json` installs as
  `cd spike && npm ci …` (subdir manifests run in their subdir), a gate that
  *references* `.venv/bin/python -m pytest` still gets a `python3 -m venv`
  hook (referencing is consuming, not creating), and a gate that already runs
  `python -m venv` skips it. Nested repos get a bootstrap hook.
- **Fails loud, not silent:** if provisioning fails or the gate stays red
  after hooks run, the daemon exits with the hook output attached — no
  mid-shift guessing.

Hand-editing `prov` hooks is allowed (like any tuned config); `prov` accepts
`hooks` (shell, run in order in the worktree), `env`, and `timeout`. The
`prov` key is in `KNOWN_KEYS`.

### Queueing (`swarm add`)

```sh
.venv/bin/python swarm/swarmd.py add --repo R "TITLE" \
  --detail "Repo-specific how-to: modules, entry points, constraints" \
  --kind feature|bugfix|test|refactor \
  --priority 1 \
  [--acceptance "criterion one" --acceptance "criterion two" ...] \
  [--depends-on "TITLE_OR_ID" ...] \
  [--execution-class standard|fast]
```

Rules to remember: 1–5 acceptance criteria; auto-generated if omitted (so
supply them when the detail is not checkable text); duplicate titles refuse;
queue max ~20 (add fails with `not queued`); tickets the swarm already tried
refuse re-add.

### Observation (`swarm report` / bridge)

```sh
.venv/bin/python swarm/swarmd.py plan  --repo R [-n 5]     # what the planner would do next
.venv/bin/python swarm/swarmd.py report --repo R [--hours 24]  # landed, breakthroughs, leaderboard, playbook, rafter wall
.venv/bin/python swarm/bridge.py activity --repo R [--offset N] [--day YYYY-MM-DD]  # per-day log tail
.venv/bin/python swarm/bridge.py status --repo R           # same shape as swarmd status (machine-readable JSON)
.venv/bin/python swarm/bridge.py landed --repo R [--limit 20]
.venv/bin/python swarm/bridge.py parked --repo R [--limit 20]   # failed/parked with reasons
.venv/bin/python swarm/bridge.py evidence --repo R --id TASK   # retained attempt bundle: contracts, reviews, gate logs
.venv/bin/python swarm/bridge.py parked-dismiss --repo R --id T | --all   # clear dead tickets (keep evidence)
.venv/bin/python swarm/bridge.py import-bugs --repo R [--limit 5] [--file F]  # feed <repo>/reports/bugs.jsonl
```

### Space / cost / models

```sh
.venv/bin/python swarm/bridge.py info                          # config, model pool, api key present, corpus
.venv/bin/python swarm/bridge.py wallet [--cap 5.0] [--reset] [--enable|--disable] [--account]  # editor wallet (paid asks only)
.venv/bin/python swarm/bridge.py receipts [--hours 24] [--limit 20] [--days N]  # lasting cost ledger
.venv/bin/python swarm/bridge.py route --repo R --task "…" [--select]           # score the pool for a task; pick best
.venv/bin/python swarm/bridge.py models? use: swarmd models
.venv/bin/python swarm/swarmd.py models [--write]              # list free tool-capable models; --write sets pool
.venv/bin/python swarm/bridge.py set-key [--key sk-or-...] [--check]   # write OPENROUTER_API_KEY to checkout .env (0600)
```

### Measurement loop (`saber`) and recursive decomposition bench (`vector`)

This tooling — and the consensus-of-agents benchmark — has been split out into its own
repository (`swarm-measurement-lab`, a sibling checkout) since it is research/tuning
infrastructure, not something the swarm itself needs to run. It is not present in this
checkout. The packet doctrine it already produced is what the rest of this document teaches:
`focused` beats `default` beats `broad`, every acceptance criterion must be runnable, and a
packet is small and single-objective. You do not need the bench itself to apply that — just
the rule.

If you are working in `swarm-measurement-lab` directly, its own README covers `vector.py`,
`vector_runner.py`, `saber.py`, and `bench_swarm.py`; `vector_runner.py apply <lesson-slug>`
still writes its result as a file you copy into this checkout's live planner/decomposer
prompts by hand — the two repos do not share state automatically.

### Single-agent mode (`flint.py` — not the swarm)

```sh
.venv/bin/python flint.py -C /path/to/project -p "goal" --yolo          # one session
.venv/bin/python flint.py -C /path/to/project --nonstop --hours 8 -p "goal" [--test-command "…"]
.venv/bin/python flint.py -C /path/to/project --read-only -p "question" # inspection only
```

`--nonstop` enables edits/shell without prompts, defaults to 8h, cannot
combine with `--read-only`, works directly in the checkout (no worktrees, no
trunk, no queue), and needs `GOAL.md` or `-p`. It is a different beast from
the daemon: use it only when the owner asks for a supervised single-agent run
on a specific directory, and never on the swarm's own checkout.

## Acceptance testing — how to write the checks the swarm can close

The reviewer (adversary) must be able to end with `APPROVE: …` by running
things. In practice that means:

1. **Add tests to the repo's suite** and make them part of `test_cmd`
   (configured per repo). The swarm will not accept "I verified manually".
2. **One check per acceptance criterion**, ideally a real assertion.
3. For **wasm/browser/game behavior**: the check must run in the game repo's
   own test harness (headless render or logic-level assertions). The swarm
   repo cannot drive a browser for you; a criterion that requires one stays
   open forever and blocks its dependents.
4. **Never lose existing assertions** — the gate fails work that removes or
   weakens tests (that is a scored fault, and it hangs from the rafters).
5. When acceptance cannot be fully expressed in code (visual design, tone of
   an influencer persona), still write *something* the adversary can verify —
   e.g. a persona output contains X, a file follows a template — and leave the
   human-in-the-loop criterion out of the packet.

## Diagnostics you will actually hit

- `daemon.running: false` → nothing is grinding; start with `run`/`grind`.
- `pacing.allowed_now: false` + `reason` → quota/allowance pause; wait
  (`resets_local`).
- `blocked` list with `reason: dependency` → a prerequisite parked/split; fix
  or dismiss the root, or the leaves stay dead.
- `reason: every implementer in the pool is excluded on this task` → the pool
  cannot touch this task; inspect the retained attempt, tighten the packet
  (scope/verifier), then `parked-dismiss` and re-add a better packet.
- `exploration_exhausted` → models twice found no entry point; you overspecified
  the wrong thing or underspecified the repo layout. Read the attempt evidence
  (`bridge.py evidence`), give the implementer a map (exact files/entry
  points), and re-queue.
- `scope_gate` park → your packet read open-ended ("clean up as needed"); add
  concrete acceptance and a bounded detail.
- `spend.remaining_usd` low → stop queueing on that repo until reset.

## strudel.nvim (the editor/workstation plugin)

Split out into its own repository (`strudel-nvim`, a sibling checkout) — it is a standalone
Neovim livecoding plugin with no Python dependency on this harness, built once as a one-off
task. It is not present in this checkout and is **not** part of the swarm. Its own `AGENTS.md`
(now that repo's root doc) still covers install state, headless test commands, and known
OpenRouter/WezTerm sharp edges, unchanged by the move.

## Golden rules (tldr)

1. Everything lands through tests + adversarial `APPROVE`, on `swarm/trunk`.
2. The acceptance criteria decide the packet's fate — make them runnable,
   numbered, 1–5, each one checkable in a fresh worktree.
3. One narrow objective per task; chain with `--depends-on`; split big asks
   yourself before the decomposer does it for you.
4. Always pass `--repo R`; respect per-repo budgets and max_queue; check
   `status` before and after you act.
5. Never touch state files, never start a second daemon on a repo, never set
   `allow_test_changes` — that gate is human-only on purpose.
6. When a task fails twice, fix the packet (map + sharper contract), not the
   config; the harness already measured why broad asks fail.