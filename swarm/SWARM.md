# Flint swarm

A multi-model coding swarm on free OpenRouter models that works on a codebase
until you stop it, compounds its own accepted work, and learns which agents and
ideas to reinforce.

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
   the goal, the task, earlier failure notes, the playbook of lessons and
   pitfalls, and study-corpus excerpts; agents can also call the `study` tool.
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
land anything. Model outages rest the model (30 min, doubling) without
lowering its score.

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
`~/.flint/corpus.db`; rebuild it with
`.venv/bin/python swarm/corpus_index.py build --root ~/Documents/flint-training --prune`
(add `--budget 150` and rerun if it stops early). The MIT OpenCourseWare index lives in
`~/Documents/flint-training/MIT OCW Courses/agent-skills/`: start at `FLINT-INDEX.md`
(domain -> topic -> lecture card -> source page). It is a copy of
`~/Desktop/college/MIT OCW Courses/agent-skills/`; refresh it with that folder's
`scripts/sync_to_flint.sh` after rebuilding the course skills.

Exit codes from flint turns: 1 error, 3 provider daily cap, 4 credit/account
limit, 5 step limit, 6 local budget pause, 7 provider unavailable, 130 Ctrl-C.

```sh
.venv/bin/python -m unittest discover -s tests -v
```
