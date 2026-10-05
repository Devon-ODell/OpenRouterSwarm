# VECTOR — recursive benchmark-driven decomposition

This is the recursive benchmark loop behind the "models do better with focused
instructions" finding. It measures the finding instead of assuming it, then
uses the measurement to teach the swarm how to break broad asks into
decomposable, focused packets — on command now, self-reinforcing later.

## The principle

The same model that passes a tightly-specified task falls over when the same
work is described in broad terms. That gap is real but was never measured, so
the harness could not learn from it. VECTOR turns specification intensity into
the independent variable:

| form      | what it is                                           |
|-----------|------------------------------------------------------|
| `broad`   | vague freeform instruction (the failure mode)        |
| `default` | today's saber harness style (the control)            |
| `focused` | scope contract + acceptance criteria + numbered steps |
| `lean`    | acceptance line only — the deployed default (`DEFAULT_SPEC`) |

Every task from `swarm/bench_swarm.py` runs through the **exact flint harness
the swarm uses** (same runner, same verifier, same spend capture) four times,
once per form. Accuracy / quickness / spend become functions of how *focused*
the instruction was — per model, per task.

**focus_gain** = accuracy(best of FOCUSED/LEAN) − accuracy(BROAD) for a task.
That single number says "how much does this task need a sharper contract".
Cross-task it says which *kind* of ask benefits from decomposition.

> **Measured default (Oct 2025, free models):** on `dots-3` across 10 tasks,
> BROAD and DEFAULT pass **10/10**, LEAN ties FOCUSED on accuracy (2/10) but at
> **1/3 the wall-time** (176s vs 570s total). Verbose FOCUSED contracts make
> free models overthink and fail already-passing tasks. So fresh work
> (split children, apply render) defaults to `lean` (`DEFAULT_SPEC`), and the
> loop measures all four arms so the bandit can learn the real ordering.

## Commands

```sh
.venv/bin/python swarm/vector_runner.py run --task 2 \
    --model dots-studio/dots-3-note-preview:free      # one task, three forms, recurse
.venv/bin/python swarm/vector_runner.py trend --n 12  # recent runs, gains, spend
.venv/bin/python swarm/vector_runner.py apply fn-sum-array   # apply a measured lesson to the live PLANNER/DECOMPOSER prompts
.venv/bin/python swarm/vector_runner.py loop --tasks 1,2,3 --gap 60   # self-reinforcing loop
```

After `setup.sh`, `swarm vector <subcommand>` works too (the `swarm` launcher
dispatches `vector` to this runner).

## The recursion

1. **Measure** — run BROAD / DEFAULT / FOCUSED on the task, record ok / secs /
   charges per form (spend captured from `FLINT_CHARGE_FILE`, same as saber).
2. **Split when it pays** — if `focus_gain` is meaningful (above
   `STOP_SATURATION`), a DECOMPOSER prompt produces 2–3 **smaller, more
   focused** subtask contracts; each child is re-measured as its own node.
3. **Stop when it saturates** — children below the saturation threshold stop
   paying for tokens; depth is capped (`--max-depth 2`).
4. **Reinforce** — each measured outcome rewards a new `decomposer` bandit arm
   in `swarm/vector_state/learn.json` (`focus:<form>` / `broad:<form>`) and
   adds playbook lessons/pitfalls, so the *deployment-side* swarm's own
   `decomposer` role inherits the bench's conclusions.
5. **Apply** — `swarm vector apply <slug>` renders the top measured lesson for
   that task as a `VECTOR:<slug>:FOCUS` preamble appended to `swarmd.py`'s
   `PLANNER` and `DECOMPOSER` templates (idempotent; the running daemon picks
   it up on its next prompt cycle).
6. **Loop** — `swarm vector loop` (or `./start-vector-loop.command`) re-runs
   the battery forever, writing plan / journal / trend evidence under
   `swarm/vector_state/`; `./stop-vector-loop.command` stops it.

## State

- `swarm/vector_state/ledger.json` — the decomposition tree: every node, its
  per-form results, `focus_gain`, children, split failures.
- `swarm/vector_state/learn.json` — the bandits + playbook (same `learn.py`
  formats the swarm already uses).
- `swarm/vector_state/loop.log`, `loop.pid` — the self-loop when running.

## Reading the results

`focus_gain` ≈ 0 on an easy, self-contained task means "do not waste tokens
over-specifying this; the broad ask is fine". `focus_gain` near 1 means "this
task belongs decomposed with explicit acceptance criteria". `vector trend`
rolls the last N runs up so the answer is a trend line, not a feeling.
