# Unattended work for a workday

From LingAI-Trader, start a ten-hour run:

```sh
.venv/bin/python flint.py \
  -C /path/to/project \
  --nonstop --hours 10 \
  -p "Improve collision handling. Add regression tests, fix failures, and check edge cases without expanding the game's scope." \
  --test-command "npm test"
```

Replace the goal and test command with those for your project. `--test-command`
is optional; when supplied, the supervisor runs it after every completed or
round-limited worker cycle and passes the real output to the next cycle.

`--nonstop` explicitly enables edits and shell commands without approval prompts;
you do not also need `--yolo`. It defaults to eight hours. `--read-only` cannot
be combined with nonstop mode. Work happens directly in the `-C` directory;
use a separate checkout if you want to review changes away from your main one.
This mode does not use the swarm queue or automatically create review branches.

You can also give the command as your prompt:

```sh
.venv/bin/python flint.py -C /path/to/project \
  -p "run this nonstop for 10 hours: Improve collision handling and verify the tests."
```

Inside Flint's interactive prompt, either of these works:

```text
run this nonstop for 10 hours: Improve collision handling and verify the tests.
/nonstop for 8h: Improve collision handling and verify the tests.
```

If you already described the task, just type `run this nonstop`; it uses your
most recent request for eight hours. With no prior request or explicit goal,
it reads `GOAL.md` in the target directory. An absent or empty goal is an error.
Only a command at the start of your message triggers this mode; mentioning the
phrase in an explanation does not enable unattended edits.

## What runs

Each worker gets the original goal, a bounded excerpt of recent tool results,
the previous handoff, and any supervisor test results. It tackles a focused
piece of the goal. Reaching Flint's normal round limit starts another cycle
instead of ending the whole run. Context is refreshed between cycles, while
files on disk carry the implementation forward. Repeated identical summaries
cause a five-minute pause and an instruction to reassess the approach.

The supervisor enforces the overall deadline, including active subprocesses,
tests, retry delays, and quota waits. Individual workers have a 30-minute
maximum; verification commands have a 15-minute maximum. Provider outages
back off and retry. Daily quota pauses wait for the recorded reset or the run
deadline, whichever comes first. Account/credit errors and three consecutive
unclassified worker errors stop the run for inspection. No model is changed
or account credits purchased automatically.

Eight or ten hours is a maximum wall-clock duration, not guaranteed inference
time or a guarantee that the goal will be finished. Exit zero means the allotted
time ended normally; inspect the recorded tests and changes to judge success.
Edits remain on disk even when a test fails or the run is interrupted.

## Progress and stopping

Startup prints the log directory under `~/.flint/nonstop/` (or `FLINT_HOME`).
It contains `status.json`, `events.jsonl`, per-cycle stdout/stderr, test logs,
and bounded progress snapshots. You can watch the current `.err.log` with
`tail -f`. Ctrl-C or SIGTERM stops the supervisor and its active process group;
the interactive interface then returns to its prompt. A second nonstop run
using the same state directory cannot own the same project at the same time.

Keep the terminal open and the computer awake during a normal run. To survive
closing the terminal, launch from the shell with `nohup`:

```sh
nohup .venv/bin/python flint.py -C /path/to/project \
  --nonstop --hours 10 -p "Your concrete goal" \
  > /tmp/flint-nonstop.log 2>&1 &
echo $!  # save this PID; stop later with: kill <PID>
```

`nohup` does not prevent system sleep. Snapshots provide cycle continuity and
an audit trail; they are not automatic resumption of a terminated run.

## Offline checks

```sh
.venv/bin/python -m unittest discover -s tests -v
```
