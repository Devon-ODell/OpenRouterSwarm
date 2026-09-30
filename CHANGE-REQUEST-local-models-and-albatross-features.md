# Change Request — Bring OpenRouterSwarm up to parity with Albatross and Cursor

**Date:** 2026-09-30
**Author:** reverse-engineering analysis (see `docs/` footnotes for sources)
**Scope:** Implement the missing features from two reference projects into OpenRouterSwarm:

1. **Albatross** (`morganlinton/tap/albatross` v2.5.0, source: github.com/morganlinton/Albatross, cloned to `/tmp/albatross-src`) — a Rust terminal coding harness.
2. **Cursor** 3.22.7 (`/Applications/Cursor.app`) — specifically its **Chat panel's local-model dropdown** (Ollama / LM Studio / MLX / llama.cpp / any OpenAI-compatible endpoint, plus Anthropic-style endpoints).

---

## 0. Executive summary

OpenRouterSwarm is a **cloud-only** system: `flint.py` hard-codes `OPENROUTER = "https://openrouter.ai/api/v1"` (`flint.py:70`) and builds an `OpenAI(base_url=OPENROUTER, ...)` client (`flint.py:794`). There is no local-model support, no interactive per-turn model picker with a live dropdown, no provider abstraction, no hardware-aware model recommendation, no route scoring, no context-progress accounting, no plan/iterate/auto loops with handoff artifacts, no hooks/extension API, no undo, no session path forking.

Albatross and Cursor both solve these problems in complementary ways. This change request proposes a **phased port of the union of their feature sets**, with the local-model Chat dropdown (Cursor) front-and-center because it directly enables the "pick a local model from a dropdown in your IDE to chat with your project" experience.

**Tiering used below:** `P0` = must-have for parity, `P1` = high value, `P2` = nice-to-have.

---

## 1. What the reference projects actually do (reverse-engineered)

### 1.1 Albatross — feature inventory

Albatross is a terminal-first coding agent harness in Rust. From `README.md` + `src/*.rs`:

**Providers / backends** (`src/backends.rs` — `BackendName` enum, `BackendDescriptor`, `backend()`, `default_model()`):
- Cloud: `openrouter` (default `qwen/qwen-2.5-coder-32b-instruct`), `openai` (`gpt-4o-mini`), `anthropic` (`claude-sonnet-5`, native Messages API), `openai-codex` (ChatGPT/Codex subscription OAuth), `grok` (SuperGrok OAuth).
- Local: `ollama` (`http://localhost:11434/v1`), `lm-studio` (`http://localhost:1234/v1`), `mlx` (`http://localhost:8080/v1`, Apple-Silicon-optimized via `mlx_lm.server`), `llamacpp` (`http://localhost:8080/v1`, GGUF via `llama-server`).
- Endpoint overrides via env: `OLLAMA_BASE_URL`, `LM_STUDIO_BASE_URL`, `MLX_BASE_URL`, `LLAMACPP_BASE_URL`, `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, `OPENAI_CODEX_BASE_URL`.
- **Switch providers mid-session** with `/provider <name>`; persistence via `/provider --default` (surgical merge into `agent.config.json`).
- All local providers + OpenAI share an OpenAI-compatible `/v1/chat/completions` transport; Anthropic uses `/v1/messages`; Codex uses the Responses backend (`src/codex_responses.rs`).

**Routing / model selection** (`src/model_system.rs`, `src/recommend.rs`, `src/route_audit.rs`):
- `ModelSystemConfig` with `planner`, `selector`, `compaction`, `orchestrators{low|medium|high}`, `coders{low|medium|high}`, `reviewers{play|production}`, `securityReviewer`, `policy{RoutingPolicy}`.
- `evaluate_coder_candidates()` scores each candidate by **eligibility (policy exclusions), estimated cost (from `catalog::turn_cost_usd`), selector score, warnings**.
- Effort model: `none|minimal|low|medium|high|xhigh|max`, with `model_supports_effort()` per backend; requested vs effective effort captured in receipts.
- `/route select <task>`, `/route simulate`, `/route why-not <model>`, `/route apply <tier>`, `/route history`, `/route spend`, `/route report`, `/route label`.
- Cost-aware routing: `max_turn_usd` cap, `unknownCost` policy (`allow|warn|deny`), `localOnly` flag, `minConfidence`.
- `/doctor` (in `src/commands/doctor.rs`): `recommend`, `autotune apply`, `--deep`, `bench`, `models` — hardware-aware model recommendation (`src/hardware.rs`: `HardwareSpec`, `hardware_cache_path`, `classify_memory`).
- `catalog.rs`: static per-model context-length + pricing table; OpenRouter has **no** static catalog (dynamic, uses provider-reported usage cost).

**Cost transparency** (`src/budget.rs`, `src/catalog.rs`, `src/route_audit.rs`):
- Per-turn and per-session cost on status line: `$0.013 this turn · $0.094 session`.
- Provider-reported `usage.cost` from OpenRouter when present; `$?` + `≥` prefix when unknown (lower bound, not fiction).
- Anthropic cache-aware pricing (0.1x cache reads, 1.25x 5-min cache writes), promotional pricing expiry.
- **Every model call is journaled as a receipt**: `kind:"modelCall"` rows in `.albatross/routes.jsonl` (the user's own file confirms fields: `requested_backend`, `requested_model`, `actual_model`, `provider`, `requested_effort`, `effective_effort`, `effort_status`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_creation_input_tokens`, `cost_usd`, `cost_source`, `duration_ms`, `status`).

**Sessions** (`src/session.rs`, `src/session_turn.rs`, `src/session_paths.rs`):
- JSONL session log; `albatross --continue` resumes most recent session in cwd.
- **Session paths**: `/path fork`, `/path switch`, `/path diff`, `/path pick` — branch the conversation + workspace snapshot, compare, merge; no git worktree required.
- `/reset` writes a **handoff artifact** and starts a clean session seeded with it (better than in-place compaction).
- Checkpoints per turn with `/undo` (reverts the last turn's file mutations including untracked files).

**Autonomous workflows** (`src/planner.rs`, `src/iterate_loop.rs`, `src/fix_loop.rs`, `src/auto_loop.rs`, `src/handoff.rs`, `src/continuation.rs`):
- `/plan` — one-line intent → spec (`spec.md`), low/medium/high task graph → `plan.json`, `/plan execute`.
- `/iterate` — generate→evaluate loop where a **separate critic model** scores each pass against a rubric; generator never grades itself.
- `/auto` — unattended: iterate + auto-`/reset` on context fill (default 0.75), `--budget`, `--deadline`, `--max`, `--yolo`, `--spec` (validates Done Criteria against the diff), stall detection (no score gain / no diff for 3 rounds), morning report at `.albatross/auto-report.md`.
- `/ship`, `/ship pr`, `/shipcheck` — readiness preflight, guarded commit/push, draft PR via gh.
- `/handoff` — commit message, changelog bullets, release post from local context.
- `/fable` — weekly Claude usage tracker (Fable tokens/turns/share/allowance).
- `/scorecard` — global quality PR scorecard (`src/scorecard.rs`).

**Context management** (`src/context_guard.rs`, `src/continuation.rs`, `src/fix_loop.rs`):
- `context.maxMessages`, `modelContextTokens`, `autoCompact`, `compactThreshold` (0.85), `reserveRatio`.
- **Dedicated compaction model** (`modelSystem.compaction`) that summarizes the transcript when context fills.
- Live prompt-budget measurement (`src/budget.rs::measure_prompt_budget` — system/transcript/tool-schema/tool-result bytes, tokens, usage ratio).

**Extensibility**:
- **MCP servers** (`src/mcp.rs`): `mcpServers` block in `agent.config.json`, `/mcp list`, `/mcp trust <name>`; trust stored per canonical workspace + config hash under `~/.config/albatross/`; env allowlist; tools surfaced as `mcp__<server>__<tool>`.
- **Extensions** (`src/extensions.rs`): trusted executables registering model tools, slash commands, lifecycle event listeners over NDJSON JSON-RPC 2.0; tools namespaced `ext__<ext>__<tool>`; same config-hash trust posture.
- **Hooks** (`src/hooks/events.rs`, `src/hooks/config.rs`): `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `PlanUpdated`, `SubagentStart`, `SubagentStop`, `Stop`, `SessionEnd` — command-type hooks defined in `agent.config.json`.
- **Agent Skills** (`src/skills.rs`), **packages** (`src/packages.rs`), **Rust SDK** (`src/sdk.rs`), **prompt library** (`src/prompt_library.rs`), **project memory** (`/index`, `/map`, `/remember` — metadata-only repo map at `.sessions/project-memory/`), **web fetch** (`web_fetch` tool), **image input**, **playground** (`src/playground.rs`).

**UI / QoL** (`src/renderer.rs`, `src/input.rs`, `src/diff_view.rs`):
- TUI status line with per-turn cost; streaming reasoning panel (`/reasoning on`); `/verbose` debug tool view; markdown rendering; diff view; auto-approval policies (`always`, `dangerous-only`, etc.); tool selection (`auto`/`fixed`); `/mode explore|edit|ship|review` presets; project-specific system prompt (`.albatross/prompt.md`, auto-truncated at 8 KB).

### 1.2 Cursor — Chat local-model dropdown, end to end

Cursor is a VS Code fork (Electron). Its Chat/Composer UI lives in the 38MB `out/vs/workbench/workbench.desktop.main.js`; inference for local models runs in bundled extensions:

- **`extensions/cursor-local-agent-runtime`** — OpenAI-compatible + Anthropic-compatible client (`dist/main.js`): builds URLs with `sJe(baseUrl, "/chat/completions")`, probes `GET {baseUrl}/models`, detects Anthropic endpoints (`pJe(e.baseUrl)` → `apiType:"anthropic_messages"`), streams with AbortController-backed signals, supports `getReader()`-style streaming, image generation via local `/images/generations`.
- **`extensions/cursor-agent-host`** — hosts agent orchestration; **`extensions/cursor-always-local`** — feature flags (`glass_local_ollama` etc).
- **`extensions/cursor-agent-exec`** — agent execution with permissions/approvals.

**Data flow (the dropdown):**

1. **User configures local models** in Cursor settings/persistent storage. Keys identified in `workbench.desktop.main.js`:
   - `applicationUserPersistentStorage.openAIBaseUrl` (nullable; `getModelForChallenge()` defaults to `https://api.openai.com/v1` when unset) and the companion `useOpenAIKey` flag.
   - `applicationUserPersistentStorage.localProviderModelIds` and `localProviderAgentModelIds` — the list of user-added local provider models; persisted by `setApplicationUserPersistentStorage("localProviderModelIds", f)` during the available-models refresh.
2. **Discovery**: the workbench calls `fetchLocalProviderModels()` on the agent-exec provider, which hits `GET {baseUrl}/models` and returns a model list — this is what populates the dropdown for the local provider. (Confirmed: runtime code `fetch()(sJe(e.baseUrl,"/models"), n)`.)
3. **Resolution**: `AgentClientService.getLocalAgentProviderConfig(e,t)` builds the request config via `yGd({apiKeyCandidates, baseUrlCandidates})`:
   - candidates come from: model's own `credentials.apiKeyCredentials.{apiKey,baseUrl}`, **persisted `openAIBaseUrl`**, the OpenRouter-provided OpenAI key (`cursorAuthenticationService.openAIKey()`), and `useOpenAIKey` flag;
   - resolves to `{baseUrl, apiKey, apiKeySource, baseUrlSource, normalizationChangedUrl}`;
   - then `MGd(...)` merges shell environment + telemetry/team headers into `customHeaders`;
   - result: `{baseUrl, apiKey, customHeaders}` used by the local-agent-runtime HTTP client.
4. **Fallback model**: `createDefaultLocalModel(e)` returns `modelId = requestedModel?.modelId ?? "gpt-5.5"` (`BGd = "gpt-5.5"`), so the Chat works even with zero configured local models by pointing at the OpenAI-compatible default.
5. **Selections stored**: `applyLocalModeLatestComposerModelSelection(c,f)` sets the Composer's `selectedModels[0]` (model config `modelName` + `selectedModels`) using a filtered/ordered local model set; admin-blocked models (`isModelBlocked`) are filtered out.
6. **Picker UI**: settings panel with title "Models" and description **"Choose which models appear in the model picker"**, plus `onAddCustomModel` → `addUserAddedModel(P)` + `enableModel(P)` + `refreshDefaultModels()`. The picker itself shows "Choose a model or use the cost- and availability-aware [auto-smart]…" options (auto-smart cost migration: `autoSmartModelMigration.js`, `_4t`/`xFi`/`IFi` filtering helpers).
7. **Streaming/abort**: the local runtime supports `/chat/completions` streaming with `AbortController` propagation from the UI; mid-stream provider errors retry, then surface as unavailable.

**Settings mechanism**: user-level config under `~/.cursor/` (this machine has no `settings.json` yet — defaults + persistent application storage hold the values; the durable artifacts are `localProviderModelIds` / `localProviderAgentModelIds` in reactive application-user persistent storage, plus `openAIBaseUrl`).

---

## 2. Feature gap matrix — OpenRouterSwarm vs (Albatross ∪ Cursor)

| # | Feature | Albatross | Cursor Chat | OpenRouterSwarm today | Priority |
|---|---------|-----------|-------------|------------------------|----------|
| G1 | **Local providers: Ollama / LM Studio / MLX / llama.cpp** | ✅ | ✅ (OpenAI-compatible + Anthropic) | ❌ hard-coded OpenRouter | **P0** |
| G2 | **Interactive model picker dropdown (Chat)** with live discovery via `/models` | ✅ `/model`,`/provider` pickers | ✅ dropdown + "add custom model" | ❌ only static config pool | **P0** |
| G3 | **Per-request base URL + API key per model** (custom endpoints) | ✅ env/base-url overrides | ✅ `credentials.apiKeyCredentials{apiKey,baseUrl}` | ❌ | **P0** |
| G4 | **Provider abstraction layer** (`BackendName`, `is_local()`, adapters) | ✅ `backends.rs` | ✅ `apiType` detection | ❌ single OpenAI client | **P0** |
| G5 | **Hardware-aware model recommendation** (RAM tier, Apple Silicon) | ✅ `/doctor`, `hardware.rs` | ⚠️ partial (auto-smart) | ❌ | **P1** |
| G6 | **Cost-per-turn/session display + provider-reported cost** | ✅ status line | ⚠️ (usage tracking) | ⚠️ spend ledgers only, no per-turn UI | **P1** |
| G7 | **Route scoring & cost-aware selection** (`/route select`, exclusions, effort) | ✅ `model_system.rs` | ⚠️ auto-smart cost | ⚠️ Thompson sampling (no cost/effort model) | **P1** |
| G8 | **Model receipts / route audit journal** (`.albatross/routes.jsonl` shape) | ✅ | ⚠️ | ⚠️ has journal.jsonl (different shape) | **P1** |
| G9 | **Plan → spec → task graph → execute; separate critic iterate; auto loop with resets** | ✅ | ⚠️ (composer) | ⚠️ swarm has planner/architect but no spec/iterate/critic loop | **P1** |
| G10 | **Handoff artifacts on reset/stop** | ✅ `/reset`, `handoff.rs` | ⚠️ | ⚠️ partial (parked-task handoff) | **P1** |
| G11 | **Undo of agent file mutations** (incl. untracked) | ✅ `/undo`, checkpoints | ⚠️ | ❌ | **P1** |
| G12 | **Session path fork/diff/pick** | ✅ `/path` | ❌ | ❌ (uses git worktrees) | **P2** |
| G13 | **Hook events** (Pre/PostToolUse, SessionStart, PlanUpdated, Subagent*) | ✅ 12 events | ⚠️ | ❌ | **P1** |
| G14 | **Extension executables** (NDJSON JSON-RPC tools/commands) | ✅ | ✅ (extension host) | ⚠️ has VS Code extension (UI only) | **P2** |
| G15 | **MCP trust + env allowlist** | ✅ | ✅ (MCPs) | ⚠️ no MCP support at all | **P2** |
| G16 | **Session continuation in cwd** (`--continue`) | ✅ | ✅ (recent chats) | ⚠️ resume via sessions dir | **P2** |
| G17 | **Streaming reasoning panel + verbose tool view** | ✅ | ✅ | ⚠️ rich console status | **P2** |
| G18 | **Web fetch tool / image input** | ✅ | ✅ | ❌ | **P2** |
| G19 | **Project memory map** (`/index`, `/map`, `/remember`) | ✅ | ✅ (retrieval) | ⚠️ corpus only (MIT) | **P2** |
| G20 | **Ship preflight + guarded git ops + PR** | ✅ `/ship` | ⚠️ | ⚠️ swarm commits to trunk | **P2** |
| G21 | **Model enable/disable + "add custom model" admin UI** | ⚠️ | ✅ picker settings | ❌ | **P1** |
| G22 | **Auto-smart cost migration ("default" → cost-aware model)** | ⚠️ | ✅ | ❌ | **P2** |

**Features OpenRouterSwarm has that the references lack** (keep, do not regress): multi-agent swarm daemon with roles/Thompson sampling, free-model rest/busy classification, MIT corpus A/B experiment, paid-rescue wallet, launchd service management, roadmap coordinator gating, weakened-test protection, sandboxed worktrees, retained.patch crash recovery, config hot-reload, 24h baseline test caching.

---

## 3. Architecture directions

### 3.1 New module: `swarm/providers.py` (P0) — provider abstraction

Mirror `src/backends.rs`:

```python
class BackendName(str, Enum):
    OPENROUTER, OPENAI, ANTHROPIC, OLLAMA, LM_STUDIO, MLX, LLAMACPP = ...

BACKENDS = {
    "ollama":      Backend(url="http://localhost:11434/v1", default_model="qwen2.5-coder:7b", api_type="openai-compatible"),
    "lm-studio":   Backend(url="http://localhost:1234/v1",   default_model="qwen2.5-coder-7b-instruct", api_type="openai-compatible"),
    "mlx":         Backend(url="http://localhost:8080/v1",   default_model="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit", api_type="openai-compatible"),
    "llamacpp":    Backend(url="http://localhost:8080/v1",   default_model="gpt-3.5-turbo", api_type="openai-compatible"),
    "openrouter":  Backend(url="https://openrouter.ai/api/v1", default_model="qwen/qwen-2.5-coder-32b-instruct", api_type="openai-compatible"),
    "openai":      Backend(url="https://api.openai.com/v1",  default_model="gpt-4o-mini", api_type="openai-compatible"),
    "anthropic":   Backend(url="https://api.anthropic.com/v1", default_model="claude-sonnet-5", api_type="anthropic-messages"),
}
# env overrides: OLLAMA_BASE_URL, LM_STUDIO_BASE_URL, MLX_BASE_URL, LLAMACPP_BASE_URL, OPENAI_BASE_URL, ANTHROPIC_BASE_URL, OPENROUTER_BASE_URL
```

- `resolve(model_ref) -> (url, api_key, api_type)` where `model_ref` is `"backend/model"` or a dict `{backend, model, base_url?, api_key?}`.
- `is_local(backend)` — True for the four local ones (0-cost, no key needed).
- `detect_api_type(base_url)` — if the URL looks like an Anthropic host/path, use `anthropic-messages`, else probe `GET {base_url}/models` and inspect `api_types`/capability headers (Cursor's `uJe`/`oJe` approach).
- Keep `Agent.client` construction in `flint.py` but route through this module: `OpenAI(base_url=url, api_key=key, ...)` for openai-compatible, and a thin Anthropic Messages client (streaming, cache-control headers) for anthropic.
- **Default model per provider** and `--default` persistence exactly like albatross (`/provider --default` clears `modelOverride`; `/model --default` sets it).

### 3.2 New UI feature: local-model dropdown in the Cursor extension (P0)

This is the headline ask — "Chat on Cursor lets you pick local models from a drop down to interact with your project."

**Backend (bridge):**
- `swarm/bridge.py` new subcommands:
  - `local-models --repo R --backend ollama` → calls `GET {base_url}/models` (with graceful failure like Cursor's `if(!s.ok) return` → empty list), returns `[{id, contextLength?, apiTypes?}]`.
  - `ask --model ollama/qwen2.5-coder:7b --base-url http://localhost:11434/v1` → run an ask round against that provider. `run_flint` gains `backend`/`base_url`/`api_key` kwargs passed through to `Agent`.
- New bridge events: `local_models`, `provider_resolved` (reports `{baseUrl, apiKeySource, baseUrlSource, normalizationChangedUrl}` like Cursor's `yGd` log), and per-request `usage` passthrough.

**Extension (`cursor-extension/extension.js`):**
- Add a **provider/model QuickPick** in the Ask-the-Swarm flow: first pick backend (`OpenRouter / Ollama / LM Studio / MLX / llama.cpp / Custom…`), then for local backends fetch `/models` and show the dropdown; "Custom OpenAI-compatible endpoint…" prompts for base URL + optional API key (stored per-project in `.cursor/settings` style — see §3.3).
- Remember last selection (vscode `context.workspaceState`), show current provider+model in the panel header, allow switching mid-conversation (albatross `/provider` behavior).
- When a local model is chosen, **omit** the OpenRouter-only fallback/rescue paths and the synthesis merge can still run locally (cheap) — but keep `--no-synthesis` respect.

**Why this maps to Cursor exactly:** `localProviderModelIds` = your clicked models; `fetchLocalProviderModels` = `local-models` bridge subcommand; `getLocalAgentProviderConfig` = `resolve()` in §3.1 + persisted `openAIBaseUrl`; `createDefaultLocalModel` default (`gpt-5.5`) = our `backend default model` fallback; `onAddCustomModel` = the "Custom endpoint…" QuickPick item.

### 3.3 Settings & persistence (P0)

Add to `flint.py` / a new `swarm/settings.py`:
- `~/.flint/providers.json` (0600) — user-scope: `{openAIBaseUrl: null, useOpenAIKey: bool, localProviderModelIds: [...], localProviderAgentModelIds: [...]}` (same keys as Cursor's persistent storage, for familiarity).
- Per-repo overrides in `agent.config.json`: `"backend"`, `"modelOverride"`, `"mcpServers"`, `"hooks"`, `"modelSystem"` (mirror albatross `agent.config.json` shape from README §Configuration).
- Env always wins at lookup time (albatross behavior).

### 3.4 Cost display + receipts (P1)

- Per-turn footer in flint REPL: `$0.003 this turn · $0.41 session`; local turns show `$0.00` + token counts; unknown cost shows `$?` and `≥` prefix.
- Extend the existing journal with albatross-shaped receipts (`.albatross/routes.jsonl`): add `kind:"modelCall"` rows with `requested_backend`, `actual_model`, `provider`, `effort_status`, `cached_input_tokens`, `cache_creation_input_tokens`, `cost_source: provider-reported|catalog-estimated`, `duration_ms`, `status`. Keep writing `journal.jsonl` rows as-is for the swarm (backward compatible), and write `.albatross/routes.jsonl` per project.
- Use provider-reported `usage.cost` when present (OpenRouter already returns it; flint currently uses `usage` include — surface it), else catalog estimate.

### 3.5 Hardware recommendation (`/doctor` equivalent) (P1)

`swarm/hardware.py`:
- Detect RAM tier + Apple Silicon (`platform`/`sysctl`), classify like `HardwareSpec.tier()` / `classify_memory()`.
- `recommend()` ranks installed local models (from each local provider's `/models`) + defaults by estimated tokens/sec feasibility for the box; `autotune apply` persists the winner as project default.
- Surface in the Cursor panel's model dropdown footer ("recommended for this Mac: qwen2.5-coder:7b (8 GB unified memory)").

### 3.6 Route scoring (P1)

- Add `swarm/routing.py`: `evaluate_candidates(stack, policy, estimated_input_tokens)` port of `model_system.rs::evaluate_coder_candidates` — exclusions (localOnly, maxTurnUsd, unknownCost deny, effort support), warnings, `estimated_cost_usd` (0.0 for local), selector call to pick, candidate scoreboard.
- Add bridge subcommands: `route select`, `route simulate`, `route why-not`, `route status`, `route template`, `route spend`, `route report`, `route label`. Wire into the panel's model picker as an "auto (cost-aware)" entry.

### 3.7 Plan / iterate / auto loops + handoff (P1)

- `swarm/workflow.py` (or new `swarm/specs.py`):
  - `plan(goal)` → `.albatross/plan.json` task graph (planner model), `/plan execute`.
  - `iterate(goal, rubric, evaluator_model)` → generate→evaluate with a **separate critic**; `liveVerify` optional test-gate.
  - `auto(goal, max_rounds, budget_usd, deadline, reset_ratio, yolo)` → iterate + auto-reset with handoff artifact + `.albatross/auto-report.md` (morning report: verdict, per-round scores, Done-Criteria checklist, cost, elapsed, reset count, stall detection).
- Reuse existing swarm pieces: budget.py (spend), sandbox.py (mutation safety), learn.py (model warm/cool).

### 3.8 Undo (P1)

- `swarm/undo.py`: snapshot touched files before each agent turn (including untracked), `/undo` restores the last turn's mutations. Persist per-session JSONL of snapshots; honor `checkpoints.maxTurns`.

### 3.9 Hooks (P1)

- `swarm/hooks.py`: NDJSON event bus with the 12 albatross events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `PlanUpdated`, `SubagentStart`, `SubagentStop`, `Stop`, `SessionEnd`); command-type hooks from `agent.config.json`; fire at the existing chokepoints in `flint.py` (`turn()`, `run_tool()`, `complete()`, checkpoint code, swarmd dispatch for Subagent*).

### 3.10 MCP + extensions (P2)

- `swarm/mcp.py`: stdio JSON-RPC client, `mcpServers` config, `/mcp list|trust`, config-hash trust store in `~/.flint/trust.json`, tools surfaced as `mcp__<server>__<tool>` with approval gating.
- `swarm/extensions.py`: NDJSON JSON-RPC 2.0 extension executables registering tools/commands/lifecycle listeners, namespaced `ext__<ext>__<tool>`.

### 3.11 Sessions/paths/remainder (P2)

- `albatross --continue`-style `flint --continue` in cwd.
- `/path fork|switch|diff|pick` (skip if git worktrees remain preferred — makes this P2).
- `/reasoning` reasoning-panel toggle, `/verbose` debug tool view.
- `web_fetch` tool, image input (base64 user message).
- `/index`,`/map`,`/remember` project memory.
- `/ship`,`/ship pr`,`/shipcheck` guarded git ops.
- Model enable/disable + add-custom-model admin UI in the panel (P1 actually — small).

---

## 4. Suggested implementation order (phases)

**Phase 1 (P0 — local models + dropdown, the headline):**
1. `swarm/providers.py` — provider registry, `resolve()`, `is_local()`, env overrides, `detect_api_type()`.
2. `flint.py` — route `Agent` through `providers.resolve`; add Anthropic Messages streaming client; default-model-per-provider.
3. `swarm/bridge.py` — `local-models` subcommand (GET `/models`), `ask --backend/--base-url`, per-model credentials, provider_resolved event.
4. `cursor-extension/extension.js` — backend QuickPick → local `/models` dropdown → custom endpoint flow; persist selection; panel header shows current provider/model; local-only ask skips rescue/wallet.
5. `~/.flint/providers.json` persistence (Cursor-shaped keys).
6. Tests: `tests/test_providers.py` (resolve/overrides), `tests/test_local_models.py` (fake `/models` server via `http.server`), extension panel unit tests for the picker state machine.

**Phase 2 (P1 — cost, doctor, routing, loops):**
7. Per-turn cost footer + receipts (`.albatross/routes.jsonl`).
8. `swarm/hardware.py` + `/doctor` bridge subcommand.
9. `swarm/routing.py` + route subcommands + auto-smart picker entry.
10. `swarm/specs.py` plan/iterate/auto + handoff + auto-report.
11. `swarm/undo.py`, `swarm/hooks.py`, admin model enable/disable in panel.

**Phase 3 (P2 — ecosystem):**
12. MCP client, extensions runtime, `--continue`, reasoning/verbose, web_fetch, project memory, ship commands, session paths.

Every phase must keep the existing test suite green (`python3 -m pytest -q tests/`) and the daemon's hot-reload contract (config keys re-read between tasks).

---

## 5. Where to put new config (schema sketch)

Extend `swarm/config.json` (per-repo) and `agent.config.json` (project-level, albatross-compatible):

```jsonc
// agent.config.json (new keys)
{
  "backend": "ollama",                       // default provider
  "modelOverride": "qwen2.5-coder:14b",      // per-project model
  "modelSystem": {
    "enabled": true,
    "coders": { "low":  {"backend": "ollama", "model": "qwen2.5-coder:7b"},
                "high": {"backend": "openrouter", "model": "anthropic/claude-sonnet-4.5"} },
    "policy": { "maxTurnUsd": null, "localOnly": false, "unknownCost": "warn" }
  },
  "mcpServers": { "fs": { "command": "/usr/local/bin/some-mcp-server", "args": [] } },
  "hooks": { "PostToolUse": [ { "hooks": [{ "type": "command", "command": "$HOME/bin/notify" }] } ] }
}
```

```jsonc
// ~/.flint/providers.json (user scope; 0600; Cursor-compatible keys)
{
  "openAIBaseUrl": null,                    // "" -> null on read (Cursor migration behavior)
  "useOpenAIKey": false,
  "localProviderModelIds": ["qwen2.5-coder:7b", "llama3.1:8b"],
  "localProviderAgentModelIds": ["qwen2.5-coder:7b"]
}
```

---

## 6. Risks & mitigations

- **Local latency/free-capacity**: local models are not rate-limited like OpenRouter free tiers, but are slow; reuse the existing status/progress streaming and per-turn timeouts; recommend by hardware (§3.5).
- **Approval gating must not change**: local models get the same mutating-tool approval gates (`NEEDS_APPROVAL`), same `AUTO_APPROVE_TOOLS` env.
- **Don't break the swarm's free-request accounting**: local turns must not consume `daily_cap` free-request counters (they're OpenRouter counters). Mark them `provider:"local"`, cost `$0.00`, and exclude from OpenRouter RPM throttle.
- **Anthropic-compatible local servers** (e.g., some LM Studio/llama.cpp builds expose `/v1/messages`): gate on `detect_api_type` probing; don't assume openai-compatible (Cursor distinguishes `anthropic_messages` vs `openai-compatible`).
- **Trust posture**: MCP/extensions run arbitrary executables — adopt albatross's config-hash trust + env allowlist from day one, never spawn changed servers automatically.
- **Secrets**: per-model API keys stored in `~/.flint/providers.json` mode 0600; never committed; env wins.

---

## 7. Acceptance criteria for the headline feature

1. With Ollama running (`ollama serve` + at least one pulled model), the Cursor extension's Ask-the-Swarm flow offers **Ollama** in a provider dropdown, lists pulled models fetched from `http://localhost:11434/v1/models`, and can answer a question entirely locally with **no OpenRouter request made** (verify via logs + zero `daily_cap` consumption).
2. A custom OpenAI-compatible endpoint (e.g., LM Studio on :1234) can be added once and appears in subsequent sessions (persisted in `~/.flint/providers.json`).
3. The model picker shows per-entry context/cost when known, marks the live choice `(selected)` and disk default `(default)` (albatross parity).
4. A local ask streams progress like the free-model path does today, honors `flintSwarm.timeoutSeconds`, and the panel header shows the active provider/model.
5. New tests cover: provider resolution & env overrides, `/models` discovery with a fake server, custom-endpoint persistence, and "no OpenRouter call on a local ask".

---

## Appendix A — source evidence (paths)

- Albatross source: `/tmp/albatross-src` — `src/backends.rs`, `src/model_system.rs`, `src/hardware.rs`, `src/catalog.rs`, `src/budget.rs`, `src/hooks/events.rs`, `src/hooks/config.rs`, `README.md` (§Providers, §Tools and commands, §Cost and credentials, §Going further, §Configuration).
- User's real albatross receipts: `/Users/devonodell/.albatross/routes.jsonl`, `~/Desktop/OpenRouterSwarm/.albatross/routes.jsonl`.
- Cursor app: `/Applications/Cursor.app/Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js` (symbols: `getLocalAgentProviderConfig`, `createDefaultLocalModel`, `fetchLocalProviderModels`, `localProviderModelIds`, `openAIBaseUrl`, `_4t`/`xFi`/`IFi`, `BGd="gpt-5.5"`), `extensions/cursor-local-agent-runtime/dist/main.js` (symbols: `sJe`, `pJe`, `oJe`, `uJe`, `nVe`, `apiType`, `/models`, `/chat/completions`, `/images/generations`), `extensions/cursor-always-local/dist/main.js`.
- OpenRouterSwarm: `flint.py` (`OPENROUTER`, `Agent.__init__`, `_request_kwargs`, `_stream_once`, `complete`), `swarm/bridge.py` (`cmd_ask`, `pick_models`, `run_flint`), `swarm/config.json`, `cursor-extension/extension.js`, `cursor-extension/README.md`.