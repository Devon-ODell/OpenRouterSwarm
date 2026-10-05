# Consensus-of-Agents — Findings & Improvement Log

Append-only log of what the benchmarks measure and what to try next.
Raw data lives in `consensus/runs/runs.jsonl`; tables regenerate via
`.venv/bin/python consensus/report.py`.

## Measured (2026 — 14-task battery, free panel vs openai/gpt-5.5)

| system | pass | cost | wall |
|---|---|---|---|
| consensus (3 free, majority) | 12/14 (86%) | $0.00 | 1956 s |
| gpt-5.5 (frontier, openrouter backend) | 12/14 (86%) | $0.55 | 204 s |
| north-mini-code:free (best panelist) | **13/14 (93%)** | $0.00 | 269 s |

## Efficiency notes (actionable)

1. **Slowest member dominates latency.** laguna-xs averages ~102 s/member vs
   ~19 s for the other two; dropping it cuts panel wall-time ~3× while keeping
   2 independent votes (consensus-of-2 requires agreement). *Try next.*
2. **Free tier ⇒ $0 marginal cost** — the entire panel is free; efficiency win
   is cost, not speed. At scale (thousands of tasks) this is the real edge.
3. **Trivial tasks don't need a panel.** smoke-status: 12s panel vs 5s
   frontier. Route simple asks to one member; use the panel only on contested /
   complex tasks.
4. **Frontier is already cheap per-call** ($0.019–0.053/task). The paper's cost
   claim matters at scale, not per task.

## Accuracy notes (actionable)

1. **Errors are not independent enough.** Both panel and frontier fail
   `bubble-sort` and `json-filter`; voting can't rescue shared failure modes.
   *Add diverse members (different families + different weak spots).*
2. **No task where panel fails and gpt-5.5 passes** — strongest defense: the
   panel never lost to the frontier; it matched it.
3. **Best panelist (north-mini-code, 13/14) already beats gpt-5.5 (12/14).**
   The ceiling of the panel is above the frontier; majority voting only lowers
   variance, it doesn't raise the best member's ceiling.
4. **Proposed v2: debate round** — after independent answers, each agent sees
   the others' worktrees and revises; then majority-vote the revisions. This is
   the natural extension where a small panel *could* exceed a single frontier.
5. **Proposed v2: weighted voting** by per-member historical accuracy.
6. **Proposed v2: task-adaptive routing** — one member on tasks it alone
   passes; panel only on contested ones.
7. **Bigger battery (50+ tasks)** to make 12/14 vs 12/14 statistically
   distinguishable and to surface tasks where the panel diverges favorably.

## Infrastructure

- `consensus/consensus_runner.py` — panel + frontier runner (append-only log).
- `consensus/report.py` — aggregate tables + JSON (reads log, never rewrites).
- `consensus/runs/runs.jsonl` — raw reproducible dataset (14 records).
- `consensus/PAPER.md` — full working paper with method + results + analysis.

## Gotchas learned

- `openai/gpt-5.5` must be run as `openrouter:openai/gpt-5.5`; the bare `openai/`
  prefix routes to api.openai.com and 401s with an OpenRouter key.
- Panel results and frontier results must be merged into ONE record per task
  (run panel, then `--frontier-only`, then merge) so the report never
  double-counts or misreads stubs.
- `--frontier-only` writes stubs with empty agents; the merge step in this log
  rebuilds clean one-record-per-task JSONL.