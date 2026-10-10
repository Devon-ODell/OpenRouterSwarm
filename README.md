# OpenRouterSwarm

A multi-model coding swarm that runs on free OpenRouter models and works on a codebase
until stopped. One daemon per repository, a tested task queue, an adversarial review before
anything lands, and accepted work merged onto its own `swarm/trunk` branch — never into your
working checkout. A Cursor/VS Code extension ("Flint Swarm") gives you a chat panel, a queue
you can edit, and one-click updates on top of the same engine.

It is a *harness*, not magic: free models perform at paid-model level when the task is
tightly specified, and fall apart on broad asks. The system is built around that fact — see
`AGENTS.md` for the full operating manual once you're past the quickstart below.

## Before you start: the $10 that actually matters

OpenRouter's own policy (not something this project invents): an API key with **$0 ever
purchased** is capped at a low daily rate limit for free models. Add **$10 in credits once**
— [openrouter.ai/credits](https://openrouter.ai/credits) — and that cap rises to **1000
free-model requests/day**, which is what every tuned config in this repo assumes.

That $10 is a balance, not a cost. Free models stay free — the swarm's own paid fallback is
off by default (`allow_paid: false`). You're not pre-paying for usage; you're clearing a
one-time account threshold. `./setup.sh` checks your key against OpenRouter's real API at the
end and tells you plainly which side of that line you're on.

## Quickstart (5 minutes)

```sh
git clone https://github.com/Devon-ODell/OpenRouterSwarm
cd OpenRouterSwarm
./setup.sh
```

You'll be asked for an OpenRouter API key (free to create at
[openrouter.ai/keys](https://openrouter.ai/keys)) if one isn't already in `.env`. The script
creates a Python venv, installs dependencies, writes the `flint`/`swarm` commands to your
`PATH`, runs the offline test suite, packages the Cursor extension if Node is installed, and
finishes by checking your OpenRouter account tier against the $10/1000-requests line above.

Then point it at a project:

```sh
swarm init ~/path/to/some/project      # probes the repo, writes a tuned config + install hooks
swarm grind ~/path/to/some/project --goal "what it should become"
swarm status --repo ~/path/to/some/project    # what's running, queue, budget
```

Or install the Cursor extension (done automatically by `setup.sh` if Node is present) and use
the "Flint Swarm" panel in the secondary sidebar instead of the CLI — same engine, a chat/queue
UI on top.

## What it actually does

```
queue a task → claimed by priority → fresh worktree from swarm/trunk
  → implementer model (Thompson-sampled) → tests → adversarial review by a different model
  → tests again → land on trunk → judge scores it → one lesson written for next time
```

Every accepted task feeds the bandit that picks models, planner personas, and task-decomposition
shapes — the system gets better at picking good models for your repo the more it runs. Failures
retry once with notes, then split into smaller subtasks, then park with an honest reason (see
`bridge.py parked`).

## The one real skill: writing a good task

The single biggest predictor of whether a task lands is how it's specified. A title, a detailed
"how" in the repo's own terms, and 1–5 *runnable* acceptance criteria:

```sh
swarm add --repo ~/path/to/project "Add a /health endpoint" \
  --detail "In server.py, add GET /health returning {\"ok\": true}. No new dependencies." \
  --acceptance "curl localhost:8000/health returns 200 and {\"ok\": true}" \
  --acceptance "pytest tests/ still passes"
```

Vague asks ("clean up the code", "make it better") get parked by the harness before a single
model spends a turn on them. See `AGENTS.md` for the full packet contract — it's short and
it's the difference between tasks landing and tasks dying in the queue.

## Repository layout

| Path | What it is |
|---|---|
| `flint.py` | the single-turn model-calling engine everything else is built on |
| `swarm/swarmd.py` | the daemon: queue, worktrees, gates, review, Thompson sampling |
| `swarm/bridge.py` | JSON-in/JSON-out CLI the Cursor extension talks to |
| `cursor-extension/` | the "Flint Swarm" Cursor/VS Code extension |
| `AGENTS.md` | the full operating manual — read this before driving the swarm as an agent |
| `COMMAND.md` | the specific projects this swarm is tuned to run against |
| `NONSTOP.md` | `flint.py --nonstop`, a separate single-agent mode (not the daemon) |

Benchmark/tuning research that shaped the harness's packet doctrine, and a standalone Neovim
livecoding plugin built on top of this engine, have been split into their own repositories —
neither is required to run the swarm.

## Requirements

- Python 3.11+
- Node.js, only if you want the Cursor extension (`brew install node`)
- An OpenRouter account ([openrouter.ai](https://openrouter.ai)) — see the $10 note above
- Cursor or VS Code, only for the extension; the CLI works anywhere
