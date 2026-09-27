# OpenRouterSwarm improvement backlog — 2026-09-27

## Scope and evidence

This is an intentionally broad planning document. It contains no product or
infrastructure implementation work. Treat every item as a candidate for a
separate, small, tested change; do not batch this whole document into one swarm
task.

Evidence reviewed on 2026-09-27:

- The supplied OpenRouter Activity screenshots show **$10.80** on one
  `Qwen3 Coder 480B A35B` request, 76.1% of the visible month spend, and about
  30.1M tokens on that model. That is an incident-level outlier, not a normal
  fallback cost.
- The Studio daemon's own shared spend ledger shows **$14.110549** this billing
  period across 2,960 requests. It contains many small `qwen/qwen3-coder`
  charges but does not itself prove which client made the single $10.80 request.
- At review, the eight-hour Studio run was free-only in practice: F02 was in a
  repair turn on `nvidia/nemotron-3-ultra-550b-a55b:free`, roughly twelve minutes
  into its task. A long repair is an efficiency warning even when it is free.
- The daemon had `allow_paid: true`, $20/month, a $5.89 remainder, and a list of
  paid fallbacks. The live config was changed to `allow_paid: false` and
  `paid_models: []`. The active free turn may finish; future task routing will
  reload the config before another task starts.
- The Studio journal confirms prior automatic upgrades after free quota
  exhaustion, including `qwen/qwen3-coder` and `poolside/laguna-s-2.1` fallback
  events. It records cumulative spend at routing time but no provider request
  ID. This is a verified cost-path defect.
- The current Cursor wallet reports $0.00 of $5.00 and no recorded calls. That
  rules out the current wallet as the source of these screenshots, but cannot
  identify an older wallet configuration, another client, or provider-side use.

The source has two independent paid paths. Do not assume a daemon setting
governs the Cursor extension:

| Path | Current behavior | Relevant code |
|---|---|---|
| Daemon | It can replace an incomplete-free-budget turn or an exhausted-free-budget turn with a paid model. | `swarm/swarmd.py:400-425`, `1638-1672` |
| Cursor ask | `auto` runs a paid rescue after every free answer fails; `always` starts paid. | `swarm/bridge.py:353-402`, `cursor-extension/extension.js:172-191` |
| Cursor wallet | Shared machine-level wallet, configured separately from daemon spend. | `swarm/bridge.py:135-146`, `622-644` |

## Non-negotiable guardrails for the next implementation agent

1. **Do not re-enable daemon paid fallback by default.** Preserve
   `allow_paid: false` and `paid_models: []` in every Studio config. A user must
   deliberately enable it for one bounded run.
2. **Never make an unbounded provider request.** Require a maximum output-token
   count, a maximum reasoning budget, an HTTP request timeout, and a per-request
   dollar ceiling before any paid request is sent.
3. **Refuse an expensive model before sending, not after receiving the bill.**
   Query catalog pricing, estimate prompt + maximum completion cost, and reject
   a model whose worst-case request cost exceeds the applicable cap.
4. **Treat missing or stale pricing as unsafe.** A model with unknown pricing,
   a negative/free-looking price, or a stale catalog must not be selected for a
   paid rescue.
5. **Put paid authorization in the command, not only a JSON file.** Require an
   explicit `--allow-paid --paid-run-cap 0.25` pair for a daemon shift. Config
   can define eligible models but cannot silently authorize charges.
6. **Default every Cursor question to free-only.** Change
   `flintSwarm.paidFallback` from `auto` to `off` in
   `cursor-extension/package.json:211-224`; keep a one-question paid override
   visible and deliberate.
7. **Cap a Cursor paid rescue at one model and one round.** The current bridge
   permits two rescue models (`swarm/bridge.py:69`, `394-402`). Never fan out
   paid asks.
8. **Create separate budgets for daemon and editor.** Do not share a vague
   monthly pot across autonomous work and interactive questions. Show remaining
   funds for each, including reservations for in-flight requests.
9. **Add a circuit breaker.** One paid request over a per-request cap, two paid
   requests in an hour, or a spend anomaly should disable paid routing for all
   clients and notify the panel.
10. **Record an immutable cost event before and after each paid call.** Include
    request ID, caller (`daemon`, `cursor`, `CLI`), repo, task/ask ID, model,
    input/output/reasoning tokens, pricing snapshot, requested limit, actual
    charge, elapsed time, and whether the result landed.
11. **Correlate OpenRouter request IDs.** Surface the provider ID in the daemon
    handoff, Cursor answer footer, and a searchable local audit file so an
    Activity chart spike can be assigned to a feature or question.
12. **Do not log API keys, source secrets, or entire raw provider responses.**
    Cost telemetry is metadata; redact prompts and paths unless an approved
    debugging capture is explicitly enabled.
13. **Do not let a paid model replace a free model merely because the estimated
    free allowance cannot cover all configured rounds.** This is the current
    behavior in `swarm/swarmd.py:1646-1654`. Trim the turn, defer it, or split
    it; a budget edge must not become a paid escalation.
14. **Do not use a model router or `:online` alias for paid rescue** until its
    maximum unit price and output limit are known and tested.
15. **Make every spend test use a fake provider charge.** No unit/integration
    test may contact OpenRouter or mutate a real wallet.

## Strict instructions: spending and anti-wheel controls

### P0 — implement before another paid run

1. In `swarm/swarmd.py:400-425`, replace `paid_stand_in()` with a policy object
   that receives role, task class, estimated tokens, run cap, per-request cap,
   and a user-granted authorization token. Return a structured denial reason.
2. In `swarm/swarmd.py:1638-1672`, remove the automatic paid route when a full
   free turn does not fit. Set a short-turn limit instead, persist the handoff,
   and resume after free quota resets.
3. In `swarm/swarmd.py:1615-1626`, prevent `route_to_paid()` from running unless
   the policy decision has `approved: true`; log model, ceiling, and reason.
4. Add `max_paid_request_usd`, `max_paid_task_usd`, `max_paid_run_usd`,
   `max_paid_output_tokens`, and `paid_enabled_until` to `KNOWN_KEYS` around
   `swarm/swarmd.py:3507-3517`, validation, status, and the config template.
5. Make `monthly_usd` a hard reservation ledger, not a post-hoc total. Reserve
   worst-case cost atomically before call; settle actual charge after call; do
   not start a call if reserve cannot be made.
6. Add an `inflight_reserved_usd` field to `swarm status` and the Cursor panel.
   A displayed balance without reservations can authorize concurrent overspend.
7. Add explicit deny-list support. Put `qwen/qwen3-coder-480b-a35b-instruct`
   and any model implicated in a spend incident on the deny list until manually
   reviewed. Match canonical slugs, aliases, and provider routing IDs.
8. Add an allow-list for paid models, with fixed versions rather than broad
   family aliases. Keep the list empty by default.
9. Enforce a model-specific output ceiling. A $10.80 one-request outlier is
   consistent with an excessive generation/reasoning allowance; reject before
   streaming when a model cannot honor the ceiling.
10. Set a maximum context payload by role. A reviewer receives changed files and
    test evidence, not a 24 KB diff plus long historical prompts by default.
11. Prune duplicate failure history before prompt construction. The old Qwen
    attempt records contain repeated failure text; cap each failure signature
    once, with a short stable excerpt.
12. Record prompt character count, estimated input tokens, output cap, and
    actual tokens for every model turn. Alert when any exceeds the role median
    by 3x.
13. Add `--dry-run-cost` to daemon and bridge commands. It prints the chosen
    model, token limits, worst-case cost, budget scope, and denial reason,
    without calling a model.
14. Add `--free-only` as an unambiguous override to every daemon, CLI, and
    Cursor bridge call. It must override all config and environment settings.
15. Require an expiration timestamp for paid override. The UI should offer 15
    minutes, one task, or one question—not a persistent switch.
16. Persist a `spend_incidents.jsonl` event when actual cost exceeds 125% of the
    estimate, with an immediate global paid kill switch.
17. Make policy reload immediate for cost controls. `allow_paid: false` must
    take effect in an active `flint()` loop before its next provider request,
    rather than waiting for the worker to finish a task.
18. Make the running process acknowledge policy changes in `now.json` and the
    panel within ten seconds. A changed config file without an
    effective-policy acknowledgement is not a safety control.

### P1 — prevent repetitive free-token waste

1. Add a task-level request budget: implementer, repair, reviewer, judge, and
   retries together must fit a declared maximum. Current roles can each receive
   substantial independent step budgets in `swarm/configs/...json:21-35`.
2. Add a wall-clock budget per task, separate from each role timeout. At review,
   F02 spent about twelve minutes in repair. After a task budget expires, stop,
   save evidence, and queue a smaller explicitly scoped follow-up.
3. Detect repeated tool-call sequences. If three consecutive rounds read the
   same files or rerun unchanged tests, stop the turn and hand off the evidence.
4. Detect no-diff loops. If a candidate has no changed tracked files after two
   implementer rounds, end the turn as `no_progress`; do not spend repair or
   adversary calls on it.
5. Detect no-new-evidence loops. If a repair sees the same failure signature and
   same diff hash, stop repair immediately rather than using `max_repairs`.
6. Gate repair on a machine-readable failure classification. Only code/test
   regressions should trigger repair; provider error, test infrastructure error,
   missing browser, and quota error should not.
7. Cache targeted test results by tree hash and command. Do not rerun an
   unchanged full suite after an adversarial text review.
8. Use a cheap syntax/lint/import check before a full suite. Fail fast on broken
   JavaScript/Python syntax, then run focused tests, then whole-suite only when
   focused gates pass.
9. Make every task declare focused validation commands and a maximum full-suite
   invocation count. Treat a generic full suite as an integration gate, not a
   tool loop.
10. Add a preflight estimate: predicted requests, time, test time, and unique
    paths. Reject or split work that cannot fit the shift/task budget.
11. Snapshot the working tree once per attempt; hash the allowed write paths and
    reject edits outside them before any reviewer call.
12. Preserve the existing deterministic gate as authority. Model review should
    never cause another repair pass unless it provides a reproducible command or
    a concrete changed-file finding.
13. Mark model/provider failures as infrastructure events in the bandit ledger.
    Do not punish a model for timeout, 429, malformed streaming, or sandbox
    failure; do cool it temporarily.
14. Add adaptive step caps per model and role from observed median success. A
    fast model that lands after four rounds should not receive twelve by default.
15. Add a hard cap on context refresh and corpus excerpts. Read only task-owned
    files until a test identifies a dependency.
16. Make prompt budgets visible in task records: context bytes, historical bytes,
    diff bytes, and token estimate.
17. Add a two-strike model quarantine for repeated empty/truncated responses,
    then retry another free model before calling anything paid.
18. Give every long test a watchdog line with elapsed time, last output, and a
    cancellation point. Panel users need to distinguish a real build from a
    quiet hang.

## Strict instructions: response quickness and uptime

1. Add per-stage latency histograms in `swarm/state/<repo>/`: queue wait,
   first-token, tool-call, tests, review, merge, and total task time.
2. Rank free models by verified acceptance-per-minute and requests-per-accepted
   commit, not only reward. A cheap model that loops is expensive in throughput.
3. Select model by role: a fast model for classification/planning, a code model
   for edits, and a different cheap verifier only when deterministic tests pass.
4. Implement speculative first-pass triage: one short free model determines
   whether the task is code, test, docs, config, or blocked before assigning a
   long implementer turn.
5. Reduce default reviewer/judge traffic when the change is tiny and all focused
   gates pass; retain an independent review for runtime, security, billing, and
   shared-shell changes.
6. Make model cooldowns role-specific. A model that cannot review JSON may still
   be useful for small implementation work.
7. Use provider health probes with strict rate limits and cache results. Avoid
   sending a full coding prompt to discover a model is unavailable.
8. Set connection/read deadlines separately from total role deadlines. Capture
   provider status, retry-after, request ID, and stream finish reason.
9. Resume interrupted attempts from their saved diff, test results, and compact
   handoff; do not replay the whole history and start from zero.
10. Keep a durable supervisor heartbeat with task ID, phase, last model event,
    last filesystem change, test PID, and elapsed seconds. The existing `now`
    record is a good base, but it needs progress-quality fields.
11. Add a stale-worker remediation ladder: inspect, request compact summary,
    cancel only the stuck role, preserve worktree, rotate model, then split.
12. Do not self-restart the daemon for source changes during a task. Preserve the
    current finish/drain behavior; add a version banner telling the panel when a
    restart is pending.
13. Validate configuration atomically: write temp file, parse/validate, then
    replace. Log effective settings and reload version, never partial JSON.
14. Add a launch lock with PID start time and repository UUID. A stale PID must
    not prevent restart; a different live daemon must not be overwritten.
15. Start a local status HTTP/socket service only if it can be authenticated and
    bound to localhost. Otherwise keep bridge JSON over subprocess.
16. Use an exponential backoff with jitter for provider 429/5xx and a maximum
    retry age. Make "waiting for provider" visible rather than looking hung.
17. Cache model capabilities/pricing with TTL and version. Revalidate on a paid
    authorization event.
18. Test daemon recovery under sleep/wake, network loss, config rewrite, stale
    lock, SIGINT, SIGTERM, deadline expiry, and a process killed mid-test.
19. Add a deadline-aware scheduler. Do not begin a task whose predicted runtime
    plus its gate exceeds time remaining in the eight-hour shift.
20. Keep the Mac awake only while an active task or bounded wait exists; kill the
    child `caffeinate` when the daemon exits and expose this state to Cursor.

## Strict instructions: response accuracy and landing quality

1. Require every task to name accepted paths, focused tests, an invariant, and
   an observable before model dispatch. Reject vague tasks at queue entry.
2. Build task-specific contract snippets from the named files; do not give each
   turn a large generic manifesto plus unrelated historical failures.
3. Require an implementation plan in structured JSON with paths, functions,
   tests, and risk—not prose—before a multi-file edit.
4. Parse tool actions against declared write paths in real time. Block illegal
   writes at the tool boundary, not during review.
5. Require a minimal patch before a broad refactor. If one acceptance criterion
   fails, fix that criterion first and rerun its focused test.
6. Make test weakening detection structural: compare removed assertions, skip
   decorators, changed fixtures, command flags, and coverage of edited symbols.
7. Add mutation tests for the spending policy, queue dependencies, guide shell,
   and other high-consequence control logic.
8. Use differential tests for generated shell copies and generated configs.
9. Make output claims cite command output, commit SHA, paths, and a timestamp.
   Do not accept "should work" or "probably fixed" as evidence.
10. Give reviewers a compact evidence packet: exact diff, allowed paths,
    deterministic test output, previous known failure, and user acceptance
    criteria. Do not ask them to reconstruct the task from conversation.
11. Add an independent static check for credential reads, network calls, new
    dependencies, subprocesses, and file writes outside the worktree.
12. Use browser tests only for explicit browser acceptance criteria; record the
    browser binary/version and skip reason. Do not turn optional availability
    into a false green result.
13. Persist test artifacts on a failed task and make the next agent read only a
    summary plus links; preserve full logs for humans.
14. Separate "accepted", "tests passed", "reviewed", "merged", and "deployed"
    in UI and ledger. These are different facts.
15. Add a counterfactual quality metric: how many reviewer findings changed the
    patch or caught a deterministic failure. Drop review calls with zero yield.
16. Measure first-attempt landing rate by task family, model, role, and prompt
    size. Optimize the worst bucket first.
17. Use holdout tasks and fixed seeds for policy changes; do not declare a new
    routing strategy better from one successful task.
18. Add human spot-check sampling for accepted visual/gameplay changes, with
    screenshots and reproducible seeds.

## Cursor extension: strict capability and UX backlog

### Cost clarity and control

1. Change the paid fallback default from `auto` to `off` in
   `cursor-extension/package.json:211-224` and migrate existing users with a
   one-time notice, not a silent behavior change.
2. Before any paid request, show a confirmation card containing model, maximum
   output tokens, estimated worst-case cost, wallet remaining, and reason free
   models failed. Require **Spend up to $X once**.
3. Remove the `always` setting or gate it behind an explicit temporary session
   mode. It is too easy to create a $10.80 surprise from a normal question.
4. Add a global **Paid requests: OFF** toggle to the sidebar, separate from the
   wallet edit button. A $0 cap should be shown as an explicit lock, not merely
   absence of a meter.
5. Show provider request ID, task/ask ID, model, input/output/reasoning tokens,
   actual charge, and duration in each answer footer.
6. Add a clickable cost ledger filtered by repository, day, caller, model, and
   request ID. Export redacted CSV/JSON for reconciling OpenRouter Activity.
7. Warn when account-level spend differs materially from local daemon + editor
   ledgers. Label it **unattributed provider spend**, never guess the source.
8. Add per-question, daily editor, daily daemon, and shared account advisory
   limits. Each should have a different color and reset clock.
9. Display in-flight reservations. Do not report wallet remaining as spendable
   while another request could consume it.
10. Show a clear "paid fallback disabled by policy" outcome instead of silently
    returning no answer after free models fail.

### Operations and observability

1. Add a dedicated **Run** panel: shift start/end, deadline, current task,
   phase, model, request/round budget, elapsed time, no-progress counter,
   free quota, paid policy, and last filesystem/test event.
2. Add one-click **Drain after task**, **Pause dispatch**, **Free-only now**, and
   **Disable paid globally** actions. Each must name the target repository.
3. Add task controls: expected paths, test plan, time budget, request budget,
   current diff size, last test result, and a link to handoff artifacts.
4. Show queue dependency graph and blocked reason. For roadmap mode, label
   coordinator gates and ownership gates distinctly from model failures.
5. Add an **Incident** button that captures redacted current state, cost events,
   tail logs, config fingerprint, and request IDs without sharing source code.
6. Add a model health table: availability, first-token latency, success rate,
   429 rate, empty-response rate, median requests/task, landing rate, and cost.
7. Show a confidence badge for model answers based on citations to files/tests,
   not a model self-rating.
8. Show test command status and duration live. Let users stop a hung test while
   retaining its output and task worktree.
9. Mark generated/derived files and offer a safe review of canonical source
   versus copies, especially for the shared game shell.
10. Give the panel a visible config-reload confirmation with changed keys and
    effective policy version. A file edit is not proof the daemon adopted it.

### Accuracy, speed, and interaction quality

1. Add request presets: explain, diagnose, design, patch review, test failure,
   bounded implementation, and cost audit. Each preset has a small context and
   tool budget.
2. Estimate question cost/time before dispatch based on selected context and
   model count; warn when selection is huge.
3. Trim code selection at syntax/function boundaries, not raw character count.
4. Deduplicate identical asks within a short window and offer the existing
   answer instead of launching parallel work.
5. Stream tool progress as meaningful stages: reading files, reproducing,
   editing, focused tests, full gate, review—not opaque rounds.
6. Let the user select one free model for a fast first answer, then request a
   second opinion only if needed.
7. Delay synthesis until there are genuinely conflicting or complementary free
   answers. One answer does not need a merger call.
8. Add answer citations that open exact file/line locations in Cursor.
9. Add a **turn this answer into a bounded task** action that prepopulates
   write paths, acceptance criteria, focused tests, and a request/time budget.
10. Add a **why did this fail?** view built from deterministic evidence and
    compact handoffs, not raw model transcript alone.
11. Provide an accessible keyboard-first command palette for cost controls,
    queue operations, model health, and incident capture.
12. Add panel tests for every destructive/cost action: wrong repository, stale
    status, no wallet, cap exhausted, declined confirmation, provider mismatch,
    and concurrent click handling.

## Suggested small implementation sequence

1. Add tests that demonstrate automatic paid fallback and the partial-free-turn
   escalation. Make them fail under the intended free-only policy.
2. Implement daemon `--free-only` / explicit paid-run authorization and remove
   the partial-budget paid route. Test it with fake provider charges.
3. Add fixed per-request/task/run reservations and provider-request correlation.
4. Change Cursor’s default to free-only, then add paid confirmation + one-rescue
   ceiling + cost event footer.
5. Add no-progress/task request/time budgets and compact handoffs.
6. Add the Cursor Run/Cost/Incident views and integration tests.
7. Run a controlled free-only shift on a fixed task set. Compare landing rate,
   elapsed time, requests/landing, context size, and zero paid charges against
   the historical baseline before considering any paid exception.

## Definition of done for the cost incident

The incident is not closed merely because the current config says false. Close
it only when all of the following are demonstrable:

- No daemon or Cursor path can issue a paid request without explicit, expiring,
  bounded approval.
- A request with the Qwen 480B slug or an unknown/wildcard price is rejected
  before network dispatch.
- A $10.80 simulated provider response trips the circuit breaker and records a
  correlated incident without exceeding the reserved cap.
- Status and Cursor show actual spend, reserved spend, policy state, and
  unattributed provider spend separately.
- Free-only runs do not auto-upgrade when quota is low or a free provider fails.
- Cost, uptime, and accuracy metrics are measured per task and used to stop
  repeated no-progress loops.
