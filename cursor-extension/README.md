# Flint Swarm for Cursor

Point the flint swarm at the code you are working on without leaving the editor.

- **Ask the swarm** (`Ctrl+Alt+A`, or right-click → *Ask the Swarm About This Code*). Several
  free OpenRouter models answer in parallel as read-only agents. Each can open the surrounding
  files and search the MIT OpenCourseWare notes. One more model then checks their claims
  against your code and writes a merged answer, credited per model. With nothing selected, the
  function or class under the cursor is used. 👍/👎 on an answer makes that model more or less
  likely to be picked next time.
- **Queue a swarm task** for the selection. The task carries the file, the lines and your
  acceptance criterion; the swarm implements it on `swarm/trunk`, tests it, has another model
  review it, and lands it only if everything passes.
- **Find MIT lectures** for the selection or a query: lecture cards first, then source pages,
  each with an *open* link to the exact page.
- **Start / Stop / Report** from the sidebar. Start runs `swarm grind` for the repository in a
  terminal. The swarm never touches your checkout, only its own branch. Stop sends it Ctrl-C.
  Report shows what landed, the model leaderboard and the MIT-corpus experiment.

The sidebar header shows whether the swarm is running here, the queue, today's free-request use and how many attempts each side of the MIT experiment has.

## Install

```sh
cursor-extension/install.sh     # runs the tests, packages flint-swarm.vsix, installs it into Cursor
```

Then reload Cursor with *Developer: Reload Window*. The extension talks to `swarm/bridge.py` in
the checkout it was installed from. To use another checkout, set **Flint Swarm: Flint Path**.

## Settings

| setting | default | meaning |
|---|---|---|
| `flintSwarm.models` | 3 | models that answer each question in parallel |
| `flintSwarm.stepsPerModel` | 8 | tool rounds (free requests) each model may use |
| `flintSwarm.synthesize` | true | one more model checks and merges the answers |
| `flintSwarm.useStudy` | true | answering models may search the MIT corpus |
| `flintSwarm.flintPath` | install folder | the flint checkout |
| `flintSwarm.python` | `.venv/bin/python` | interpreter for the bridge |

## Cost and speed

Only `:free` models are used, and the swarm refuses paid ones unless `allow_paid` is set in
`swarm/config.json`. A question with three models and a merge costs about 10 to 35 of the 1,000
free requests a day. Free models are slow: expect 1 to 7 minutes per answer. The panel streams
each model's progress while it works. Questions do not count against the swarm's 100-request
reserve, which is kept for you.
