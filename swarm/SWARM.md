# Flint swarm

A multi-model coding swarm on free OpenRouter models that works on a codebase
until you stop it, compounds its own accepted work, and learns which agents and
ideas to reinforce.

First time in a checkout (creates `.venv`, finds or asks for your OpenRouter
key, installs the `flint` and `swarm` commands, runs the tests and installs the
Cursor extension pointed at this folder):

```sh
./setup.sh
```

`swarm/config.json` is yours: it is created from `swarm/config.example.json`
on first use, rewritten on every run, and not tracked by Git, so pulling never
fights with it. Delete it to start again from the template.

```sh
swarm grind ~/code/project --goal "what it should become"   # start nonstop
swarm grind                                                 # resume the configured target
swarm grind --new ~/code/idea --goal "a problem with no code yet"
swarm report                                                # in the morning
```

`flint /nonstop` / `flint --nonstop` is the separate single-agent mode that
edits the current directory directly. The swarm never edits your checkout.

## One task, end to end

1. **Claim** the highest-priority ready task (breakthrough follow-ups, then
   splits of failed tasks, then planned or hand-added work).
2. **Branch** a fresh worktree from `swarm/trunk`, which already holds every
   change the swarm has accepted, so later work builds on earlier work.
3. **Implement** with a model drawn by Thompson sampling. The prompt carries
   the goal, the task, earlier failure notes and the playbook of lessons and
   pitfalls. On the corpus side of the MIT experiment (below) it also carries
   MIT lecture-card excerpts when a lecture card matches the task, and every
   role in the attempt can call the `study` tool.
4. **Gate**: the test command must pass and no existing assertion may be removed.
5. **Adversary**: a different model tries to break the change and must end
   with exactly `APPROVE: …`. A rejection leaves its failing test on the branch.
6. **Land** on trunk by fast-forward, rebasing and re-testing first if trunk
   moved. Trunk is never moved while it is checked out anywhere.
7. **Judge**: a third model scores impact, creativity and quality (0–10),
   names one lesson and up to two follow-ups.
8. **Reinforce**: the reward updates the implementer model, the planner model
   and the planner persona that proposed the task.

Failures retry once with their notes, then the decomposer splits the task into
2–3 smaller ones (depth 1); if those fail twice they are parked for you.

## Rewards, creativity and breakthroughs

Only work that passed the tests, the reviewer and landing can score above 0.4.
Each outcome also has a weight: how many draws' worth of evidence it adds to
the model's record. Punishment outweighs praise.

| outcome | reward | weight |
|---|---|---|
| weakened existing tests | 0.00 | 6 |
| shipped code that fails the tests | 0.00 | 4 |
| reviewer proved a defect | 0.00 | 4 |
| no change, malformed output | 0.00 | 2 |
| accepted but lost a landing race | 0.30 | 0.5 |
| landed | 0.4 + 0.6 × (0.35 impact + 0.35 creativity + 0.30 quality)/10, − 0.1 per repair round, + up to 0.1 novelty | 2 |
| breakthrough | an extra 1.0 | +3 |

A model with two good landings that then ships faulty code once drops below
an untried model and needs three more clean landings to climb back. Only the
code's author takes the penalty weight; the planner whose idea it was learns
at normal weight. A reviewer that cannot deliver a verdict is penalised as a
malformed output.

**The rafters.** Faulty code that never got fixed is hung up under its
author's name. The reviewer's top finding (or the failing test line) and the
offending lines of the diff are shown in every later implementer's and
planner's prompt ("HUNG FROM THE RAFTERS"). Cheating ranks highest, then the
newest; the three worst of the last twelve are shown. Every exhibit is also
appended to `RAFTERS.md` and announced with a notification; the playbook
section of `swarm report` shows the current wall.

Novelty is the distance from everything accepted before. Creativity counts as
much as impact, and the planner rotates between five personas: builder,
**inventor** (ideas nobody asked for), **scholar** (applies a technique from the
MIT corpus), hardener and refiner. Personas that produce highly scored,
accepted work get drawn more often.

A **breakthrough** is landed work that the judge flags with impact or creativity
≥ 8, or a reward at least 0.85 and two standard deviations above recent landed
work. It is reinforced twice, its lesson is pinned into every later prompt, up
to two follow-ups jump the queue, it is appended to `BREAKTHROUGHS.md`, and a
macOS notification is posted.

The judge only ranks work that external checks already accepted; it cannot
land anything.

## Busy and resting models

Free models are often **busy**: OpenRouter answers 429 "Provider returned
error" because the upstream provider's free capacity is taken ("temporarily
rate-limited upstream"). flint retries three times, honouring `Retry-After`,
then exits 8. The swarm rests that model for 2 minutes (doubling per busy
spell, at most 30) and **hands the turn to another model**, so a busy reviewer
or judge no longer sinks an attempt that already has an implementation. A
role that edits files is handed over only while it has changed nothing;
reviewers and judges are never the implementer. Handoffs are `handoff` rows in
`journal.jsonl`, and the model that actually answered gets the credit.

A model this key cannot use at all (403 "only available on agentic harnesses",
404 no such model; exit 9) is dropped for the run and named in the log: waiting
would not bring it back, so remove it from the pool. A model that is **down**
(repeated 5xx or dropped streams; exit 7) rests 30 minutes, doubling up to 12
hours. Neither lowers its score. Rests are saved
in `learn.json`, so they survive restarts: startup lists resting models,
`swarm status` shows them under `resting`, strikes older than 12 hours are
forgotten, and `swarm wake` ends every rest now. When every model is resting,
the log says which is back first and when.

## MIT corpus experiment

Does the MIT OpenCourseWare material make these models better? Each attempt is
randomly assigned, before its implementer runs, to one of two arms
(`mit_experiment.share_on`, default 0.5). The **with** arm gets excerpts in its
prompt and the `study` tool for every role. The **without** arm gets neither:
the tool is hidden from flint. Everything else is shared, so the difference
between arms estimates the corpus's effect. `swarm report` (and the Cursor
panel) shows landed rate with 95% Wilson intervals, the difference with its
interval and a Fisher exact p-value, mean reward, judge scores, study calls per
attempt, and a per-model table. Below 30 attempts per arm it says the numbers
are anecdotes. The raw rows are the `attempt` events in `journal.jsonl`;
`python3 swarm/experiment.py <journal>` prints the table. Set
`"enabled": false` to give every attempt the corpus.

Retrieval (`swarm/mit_corpus.py`) puts lecture cards first. A card condenses
one lecture and links the exact source pages. Source pages follow, labelled
with course, lecture and page. Every hit carries an absolute path, so an agent
can `read_file` the page a card cites. Raw OCW site pages fill in only when
nothing better matched. Excerpts are injected into a prompt unasked only when a
lecture card matches the task. The `study` tool returns whatever matches.

## Review and merge

```sh
git -C REPO log --oneline main..swarm/trunk
git -C REPO diff main...swarm/trunk
git -C REPO merge swarm/trunk        # from your main checkout, when you like it
```

New commits on `main` are merged into trunk automatically when they merge
cleanly and keep the tests green. Rejected attempts stay on `swarm/<task>-<id>`
branches, committed as "not accepted"; worktrees are removed after each attempt
(`keep_worktrees: true` keeps them).

## Cursor / VS Code extension

`cursor-extension/install.sh` tests, packages and installs the Flint Swarm
extension into Cursor. From the editor you can:

- ask several free models about the selection, or the function under the cursor, in parallel, with a merged and checked answer;
- queue a swarm task for the selection, with file and lines attached;
- look up MIT lectures;
- start, stop and read reports on the swarm for the current repository.

It talks to `swarm/bridge.py`, a JSON CLI you can also use directly (see its
docstring). Asking uses read-only agents in the sandbox. It spends the day's
free requests but not the swarm's reserve. 👍/👎 on an answer trains a
separate bandit (`swarm/state/consult/learn.json`) that picks which models
answer next time.

## Budget

With at least $10 of credits OpenRouter allows 1000 free-model requests per UTC
day (8 pm Eastern). The swarm reads the real limit and usage from OpenRouter at
startup and every 15 minutes, keeps `reserve` (100) for you, and waits for the
reset when the rest is spent. Only `:free` models are used; a paid model in the
pool is refused unless `allow_paid` is true, so credits are never spent.
`swarm models` lists free tool-capable models; `swarm models --write` resets the
pool. One worker usually spends the day's allowance, so more workers finish
sooner, not more; they also land in parallel and conflict more.

`owner_window` (disabled by default) pauses the swarm during set local hours.

## Safety

Agents and the test command run under a macOS `sandbox-exec` profile: writes
only to the task's worktree, its Git metadata, `~/.flint`, temp and package
caches; `.env` files, `~/.ssh`, `~/.aws` and similar are unreadable; secret-like
environment variables are removed from test runs and agent shell commands. The
supervisor's Git commands ignore hooks. Network access remains open, so this
contains accidents, not a determined attacker. `grind` runs `caffeinate`: the
Mac stays awake unless the lid is closed on battery.

## State and files

Per target repository: `swarm/state/<repo>-<hash>/` holds the queue, journal,
`learn.json` (bandits, playbook, history), `BREAKTHROUGHS.md` and `REPORT.md`;
`swarm/logs/<repo>-<hash>/` holds per-role logs. The study corpus is
`~/.flint/corpus.db` (`FLINT_CORPUS_ROOT` if it was built from another folder); rebuild it with
`.venv/bin/python swarm/corpus_index.py build --root ~/Documents/flint-training --prune`
(add `--budget 150` and rerun if it stops early). The MIT OpenCourseWare index lives in
`~/Documents/flint-training/MIT OCW Courses/agent-skills/`: start at `FLINT-INDEX.md`
(domain -> topic -> lecture card -> source page). It is a copy of
`~/Desktop/college/MIT OCW Courses/agent-skills/`; refresh it with that folder's
`scripts/sync_to_flint.sh` after rebuilding the course skills.

Exit codes from flint turns: 1 error, 3 provider daily cap, 4 credit/account
limit, 5 step limit, 6 local budget pause, 7 provider unavailable, 8 model busy
(rate-limited upstream), 9 model not available to this key, 130 Ctrl-C.

The target must be the repository that actually holds the code. A folder that
merely contains projects is searched one level down for a single testable
project; a project with its own `.git` is refused, because Git keeps an
embedded repository's files out of its parent, so the swarm's worktrees would
be empty where that code should be.

```sh
.venv/bin/python -m unittest discover -s tests -v
```
