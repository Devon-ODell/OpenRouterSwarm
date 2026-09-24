# Claude handoff: Flint reliability review

## Observed failure

The user ran `flint.py -C <icebowl> -p <large browser-port task>` without
`--yolo`. Output shows upstream provider 429 retries and then KeyboardInterrupt
inside an SSL read after Ctrl-C. It does not show an application exception or
an active-checkout conflict. Retry counters restart for each model round.
The command could inspect files, but mutation/shell calls required `--yolo`.
Asking for 24 hours in the prompt did not override MAX_STEPS=40.

## Changes made

- Explicit headless read-only mode, restricted tools, stderr round/tool progress.
- 90-second configurable network timeout; handle HTTP and SSE API errors;
  close streams; reject truncated/empty replies; exit 130 on Ctrl-C and 5 on
  exhausted steps instead of pretending the task completed successfully.
- Fixed empty API-key injection that suppressed `.env` in swarm subprocesses.
- Removed automatic merge/reset of the user's active checkout, live harness
  self-edits, smoke-test ratchet, automatic corpus indexing, and launchd setup.
- One worker by default; architect optional; implementer plus adversary with
  baseline/final gates and explicit APPROVE required. Unique retained worktrees
  per attempt, including failures. Git failures are checked.
- Shared per-request reserve enforcement, atomic counters, conservative 50/10
  default budget; removed hourly pacing and unused inferred-cap detection.
- Cross-process queue lock, atomic queue replacement, singleton daemon lock,
  restart claim recovery, quota pauses without retry penalties, process-group
  termination and a real `run --hours 24` maximum duration.
- Provider quota display uses free_model_daily_requests instead of guessing
  from is_free_tier. Startup banner no longer makes a redundant network call.
- Corpus incremental completion check no longer rereads all indexed paths for
  every source file; empty queries do not open a database connection.

## Validation and next experiment

Run `.venv/bin/python -m unittest discover -s tests -v`. Coverage includes real
Git worktrees with dirty user files, baseline failures, strict review verdicts,
stream failures/retries, read-only tools, queue concurrency, quota deferral,
and cancellation. No real model requests or edits to Icebowl were made.

The local config still has installation placeholders. Before an actual run,
configure a committed target and a test command that works in a fresh worktree.
Use a small deterministic game subsystem as the first task, then inspect the
resulting branch. Add real browser gameplay/rendering checks in the target
project; a green unit suite does not prove a Three.js port is playable.
Measure accepted tasks, requests per accepted task, failed gates and runtime
before increasing workers. Do not claim consensus increases model accuracy
without comparing outcomes against the same external checks.

## Remaining limits

No live provider validation was performed; free endpoint availability is outside
this code's control. Local quota accounting excludes other clients/machines.
Worker shell access is not sandboxed. Failed work is retained but not resumed
from saved conversations. Review assertions are heuristic, not a semantic proof.
Manual review/merge is intentional while the target is being edited concurrently.
A 24-hour duration means the supervisor may wait for quota; it is not 24 hours
of guaranteed inference. Baseline-red projects require human triage first.

# Update 2026-09-24: self-reinforcing nonstop swarm

Owner asked for a nonstop mode that compounds work, rewards creativity and
reinforces agents on breakthroughs. The owner is separately building
`nonstop.py` / `tests/test_nonstop.py` and flint's `/nonstop` CLI handling:
leave those and flint's CLI/interactive command handling to them.

- Account: $50 credit → `free_model_daily_requests.limit` is 1000/day.
  Config: daily_cap 1000, reserve 100, owner_window disabled, 10 free models.
  `flint.fetch_key_info` was silently failing (urllib lacked a CA bundle);
  it now uses truststore, like the openai client.
- Accepted work lands on `swarm/trunk` (fast-forward, rebase + re-test when
  trunk moved, never while trunk is checked out); tasks branch from trunk.
  `main` is merged into trunk only when clean and green. Nothing touches main.
- `swarm/learn.py`: Thompson-sampling bandits (implementer model, planner
  model, planner persona), reward = gates first then judge scores + novelty,
  lessons/pitfalls playbook with reward credit, breakthrough detection.
- Recursion: retry once with failure notes → decomposer splits (depth 1) →
  park. Strong accepted work queues the judge's follow-ups.
- `swarm/sandbox.py`: sandbox-exec profile for agent turns and test gates.
  Verified: writes outside worktree/caches denied, .env and ~/.ssh unreadable,
  TLS to OpenRouter works inside. It cannot nest inside another sandbox.
- flint (library only): `study` corpus tool, exit 7 = provider unavailable
  (swarm rests the model, no penalty), headless bash strips secret env vars.
- Corpus built: 2760 files / 37160 chunks, skipping OCW static_shared JS.
- Tests: tests/test_swarm_learning.py (24) plus existing suites.
