# Flint Swarm for Cursor

Point the flint swarm at the code you are working on without leaving the editor. Version 0.5
adds a Codex-style conversation surface, an inline provider/model picker, responsive queue and
change views, workspace settings, encrypted key storage, and an in-extension setup guide.

In the Queue tab, use **×** to dismiss one split/parked task or **Clear all abandoned**
to dismiss the whole collection, including rows beyond the first 20 displayed.
Logs and patches are preserved; active tasks and accepted work are untouched.
Dismissal does not satisfy dependencies. Restore a dismissed task with
`python swarm/bridge.py queue-requeue --repo /path/to/repo --id TASK_ID`.
After updating the extension, run **Developer: Reload Window** to load the controls.

- **Ask the swarm** (`Ctrl+Alt+A`, or right-click → *Ask the Swarm About This Code*). Several
  free OpenRouter models answer in parallel as read-only agents. Each can open the surrounding
  files and search the MIT OpenCourseWare notes. One more model then checks their claims
  against your code and writes a merged answer, credited per model. With nothing selected, the
  function or class under the cursor is used. 👍/👎 on an answer makes that model more or less
  likely to be picked next time.

  **Picking a provider/model.** The composer has provider and model controls beside the send
  button, so asking does not interrupt you with a second dialog. OpenRouter shows the configured
  free pool; Ollama, LM Studio, MLX and llama.cpp show the models their running server reports
  from `/v1/models`. A custom OpenAI-compatible URL and model id work too. The choice is saved
  for this workspace, so another project can use a different backend.
- **Queue a swarm task** for the selection. The task carries the file, the lines and your
  acceptance criterion; the swarm implements it on `swarm/trunk`, tests it, has another model
  review it, and lands it only if everything passes.
- **The Queue tab** shows everything waiting, whatever is running first and then by priority:
  kind, origin, attempts so far, what is waiting on what, and when a failed task will be retried.
  Click a row to read its detail and acceptance criteria. Hover it for the two controls:
  - **✎** opens the task inline — title, detail, acceptance (one per line), kind and priority.
    Saving keeps the task's id and history, and clears a retry backoff so it is tried again at
    once. A task a worker is running right now cannot be rewritten underneath it.
  - **✕** drops it. If other queued tasks depend on it they can never run without it, so Cursor
    asks first and then removes them together. **Clear all** empties the queue but keeps whatever
    is being worked on.
- **Find MIT lectures** for the selection or a query: lecture cards first, then source pages,
  each with an *open* link to the exact page.
- **Start / Stop / Report** from the sidebar. Start runs `swarm grind` for the repository in a
  terminal. The swarm never touches your checkout, only its own branch. Stop sends it Ctrl-C.
  Report shows what landed, the model leaderboard and the MIT-corpus experiment.

The sidebar header shows whether the swarm is running here, the queue, today's free-request use,
the paid budget you have spent and how many attempts each side of the MIT experiment has.

## When free models are all busy

Free models get rate-limited, gated and retired, and a question where every one of them fails
used to come back empty after minutes of waiting. The extension has its own **paid budget** for
exactly that case — a fixed pot of the credits on your OpenRouter account, **$5 by default**:

- It is spent only when no free model answered, on **one** paid model to rescue the question
  (`flintSwarm.paidFallback`: `auto`). `off` keeps it free-only; `always` answers with paid models
  from the start, which is the fastest and most reliable setting and the one that empties the pot
  soonest.
- Set it with **Flint Swarm: Set the Paid Model Budget**, or the ✎ beside the budget in the
  sidebar header. `0` turns paid models off entirely. The same command offers to reset what has
  been spent.
- Every charge is the amount OpenRouter reports for that request, checked against the pot before
  each request and totalled in `~/.flint/wallet.json`. Paid answers are labelled **paid** in the
  panel and show what they cost; the question's total appears in its footer.
- The swarm daemon is unaffected: it still runs on free models only and cannot reach your credits.
  The budget is the extension's alone.

## Install on a laptop

You need Cursor, Git, Python 3, and the current Node.js LTS. On Windows, use WSL with Ubuntu for
the terminal steps. Then run:

```sh
git clone https://github.com/Devon-ODell/OpenRouterSwarm.git
cd OpenRouterSwarm
./setup.sh                      # venv, tests, commands, then the extension
cursor-extension/install.sh     # later: re-package and reinstall only the extension
```

Then reload Cursor with *Developer: Reload Window*. The extension talks to `swarm/bridge.py` in
the checkout it was installed from. To use another checkout, set **Flint Swarm: Flint Path**.
The complete plain-English walkthrough lives in the sidebar's **Help** page, including what each
button does, how to move to a second laptop, and where cloud requests and Git changes go.

## Settings

Open the sidebar's **Settings** page to edit these values for the current workspace. This uses
Cursor's workspace configuration rather than changing another project's defaults.

| setting | default | meaning |
|---|---|---|
| `flintSwarm.models` | 3 | models that answer each question in parallel |
| `flintSwarm.stepsPerModel` | 8 | tool rounds (free requests) each model may use |
| `flintSwarm.synthesize` | true | one more model checks and merges the answers |
| `flintSwarm.useStudy` | true | answering models may search the MIT corpus |
| `flintSwarm.timeoutSeconds` | 300 | how long one model may take before it is given up on |
| `flintSwarm.paidFallback` | auto | `off`, `auto` (only when no free model answered) or `always` |
| `flintSwarm.flintPath` | install folder | the flint checkout |
| `flintSwarm.python` | `.venv/bin/python` | interpreter for the bridge |

## The OpenRouter API key

On a fresh Cursor start, if no key is configured and the last-chosen backend is the cloud one,
the extension asks for your OpenRouter key once (`sk-or-…`). Keys saved from the extension are
kept in Cursor/VS Code SecretStorage (the operating system's encrypted credential store) and are
passed only to the local bridge child process. They do not travel through workspace settings or
Git. You can replace or forget the saved key from **Settings**, or use **Flint Swarm: Set the
OpenRouter API Key…**. Existing installations with a key in the checkout `.env` remain compatible.
Local-only setups (Ollama, LM Studio, …) do not need an OpenRouter key.

## Phase 2/3 additions (cost, doctor, routing, undo, extensions)

The bridge gained Phase 2/3 subcommands:

- `bridge.py receipts [--hours N] [--days N]` — the lasting cost ledger
  (`~/.albatross/routes.jsonl`, same file the real albatross harness writes; flint records
  every charged request there and reads albatross's own rows too).
- `bridge.py doctor` — hardware-aware local-model recommendation for this machine
  (albatross `/doctor` parity).
- `bridge.py route [--select] [--task ...]` — scores the configured model pool by cost and
  policy and says which model to use.
- `bridge.py undo [--list] [--session ID] [--dry-run]` — restores the file states a flint
  turn snapshot before editing (guards against clobbering edits you made since).
- `bridge.py extension [--list] [--trust NAME] [--run --event E]` — the trust-first
  extensions runtime under `~/.flint/extensions/`.
- `flint --continue` (and `bridge.py continue`) — resumes the most recent nonstop run's
  goal for more hours.
- `flint.py` gained a `web_fetch` tool and PostToolUse hooks from `agent.config.json`
  (`hooks.PostToolUse[].hooks[].command`, run with the event as `$1` and a JSON payload on
  stdin, 10s cap).

## Cost and speed

Free models answer first and cost nothing: a question with three models and a merge costs about
10 to 35 of the 1,000 free requests a day, and does not touch the swarm's 100-request reserve.
Free models are slow — expect 1 to 7 minutes per answer — so the panel streams each model's
progress while it works, and gives up on one after `flintSwarm.timeoutSeconds`.

Credits are spent only from the paid budget above, only by this extension, and only on a question
no free model answered. A rescue on a cheap model is typically fractions of a cent, so $5 lasts a
long time; the sidebar shows what is left. `allow_paid` in `swarm/config.json` is a separate,
unrelated switch — it would let the *swarm daemon* run on paid models, and is off.
