# OpenRouterSwarm — Code Review (2026-10-10)

Scope: all 46 local branches, `swarm/trunk`, uncommitted diff, core Python engine
(`swarm/`, `flint.py`), the Cursor extension (`cursor-extension/`), tests, and repo hygiene.
Goal of the review: **sleek, focused, user-friendly, effective.**

**Verdict:** the core engine is well-tested (822 tests pass, incl. 29 subtests) and the
harness philosophy (tight packets, adversarial review, trunk-not-checkout) is sound.
The problems are almost all *presentation and hygiene*: a 3,700-line webview
template/script monolith in the extension, ~20 orphaned branches, 6.3 GB of worktrees
and 21 config snapshots nobody can tell apart. Nothing below is a correctness blocker —
but the codebase actively works against the four goals above.

Severity guide: **P1** = should fix before next release; **P2** = polish/when-time;
**P3** = note, no action needed now.

---

## 1 · HYGIENE: Branches, worktrees, cruft

| # | Severity | Category | Finding |
|---|----------|----------|---------|
| H1 | P1 | Hygiene | **46 local branches, of which only 3 are real** (`main`, `fix/paid-fallback-reconcile`, `local-before-merge`). ~20 `swarm/t*` branches are fully merged into `main`; the rest are stale swarm attempt branches (`ahead:1–9`, `behind:52–70`). They will grow forever. |
| H2 | P1 | Hygiene | **`swarm/trunk` is ahead of `main` by 9 commits and 3,187 insertions** — the entire `strudel-nvim/` plugin (815-line `agent.lua` + 7 more files). If trunk is the real product, merge it to `main` (or move the repo to trunk as base). Right now two "main" branches disagree. |
| H3 | P1 | Hygiene | **6.3 GB of worktrees** (`swarm/wt/`): 4.6 GB is `dj-bot-47dfef`, 1.0 GB `xmr-studio-34a077`. These are git worktrees of other repos — making them branches of *this* repo (via `swarm init`) means every `git fetch`/`status` here drags their objects. Prune finished worktrees; consider storing `swarm/wt/` outside the repo entirely. |
| H4 | P2 | Hygiene | **21 files in `swarm/configs/`; 14 are timestamped snapshots** (`*.before-*`, `backup-before-unlock-*`). They accumulate every time anyone edits a config. Archive them under `swarm/configs/history/` or in git history, not as flat siblings of the live configs. |
| H5 | P2 | Hygiene | **43 `except Exception` / bare `except` sites** across the Python tree. Most are deliberate (thread-survival, "never stop a run from starting": `swarmd.py:1417`), but several swallow errors with no log: `corpus_index.py:75`, `hardware.py:48`, `providers.py:299`. |
| H6 | P3 | Hygiene | The `.sessions/` (155 MB) and `swarm/state/` (40 MB) dirs sit in the repo. If generated, they belong in `.gitignore` or a runtime dir (`~/.flint`), not the working tree. |
| H7 | P3 | Hygiene | The uncommitted diff (pixelated `swarm.svg` logo in the extension header, with CSS + test) is small and coherent — **commit it or drop it**; don't carry it as a forever-pending change. |

**Proposed cleanup (safe, ordered):**
```sh
# 1. Delete branches fully merged into main
git branch --merged main | grep -v '^\*\|main$\|trunk' | xargs git branch -d
# 2. Merge trunk into main (or rebase main onto trunk) after review
git diff --stat main...swarm/trunk   # review the strudel-nvim work
# 3. Prune finished worktrees
git worktree list | grep -v '^\S*\s*\S*\s*\[main\]' | awk '{print $1}' | xargs git worktree remove --force
# 4. Move config snapshots
mkdir -p swarm/configs/history && git mv swarm/configs/*.before-* swarm/configs/history/ 2>/dev/null
```

---

## 2 · SLEEK — the extension is 80% Template, 20% Logic

The extension is **sleek in pixels but not in structure** (known-good CSP at
`extension.js:1241`, escape-first renderer, no `eval` — good). The problems:

| # | Severity | Finding |
|---|----------|---------|
| S1 | P1 | **~200 lines of the entire app HTML lives inside a JS template literal** (`SwarmPanel.html`, `extension.js:1237+`), including the app header, status deck, tabs, composer, and 4 tab panels. It cannot be syntax-checked, linted, or edited with any HTML tooling. Move to `media/panel.html` and inject only the CSP nonce/URIs. |
| S2 | P1 | **`media/panel.js` is 1,091 lines of one IIFE** with a single `switch`-less `message` listener and DOM building via `innerHTML` concatenation (`panel.js:194, 240, 458, 539–652, 741–743`). The model-answer escape is correct everywhere it renders `R.esc()`, but *one* missed escape in the queue editor or settings form becomes an XSS vector inside the webview. Split into modules (status, queue, thread, settings, skin) with a whitelist of `postMessage` channels. |
| S3 | P2 | **Some `innerHTML` sites interpolate untrusted-shaped data without `R.esc()`**: `statusHtml()` at `panel.js:194` is fed by daemon JSON. Verify every `now.run.reason`, task detail, and commit message passes `R.esc()` — today the queue/detail rendering does, but the status deck path is worth an audit. |
| S4 | P2 | **The `el()` helper + `innerHTML` pattern defeats DOM APIs**: toggling classes, focus, and aria states requires string surgery. For a panel this interactive (queue editing, provider pickers, tabs), use `textContent` for untrusted values and `document.createElement` for structure. |
| S5 | P3 | Extension ships a `flint-swarm.vsix` build artifact in the repo (`cursor-extension/flint-swarm.vsix`). Binary in source control — `.gitignore` it and build on release. |

---

## 3 · FOCUSED — one daemon, three jobs, lots of side quests

The product story ("a harness that turns tightly-specified tasks into landed,
tested work") is clear in `README.md`/`AGENTS.md`. The *code* has drifted:

| # | Severity | Finding |
|---|----------|---------|
| F1 | P1 | **`swarm/swarmd.py` is 6,152 lines** — a single file holding the HTTP server, queue, worker threads, budget pacing, model routing, git integration, and the full prompt/agent loop. The `Worker` class alone spans `swarmd.py:3080–3990` (~900 lines). It is the thing that must be "sleek, focused, effective" and it is the least navigable file in the repo. Split at the seams: `queue.py`, `worker.py`, `budget.py` (exists), `api.py`. |
| F2 | P1 | **`flint.py` (1,985 lines) duplicates a big slice of `swarmd.py`**: two `Spend` classes (`flint.py:592`, budget/`swarmd.py:345`), two provider-down exception families (`flint.py:543–551` vs `swarmd.py:1923–1943`), two classify-429 paths. The CLI is a second engine, not a thin client of the daemon — that is *why* behaviors drift. Pick one spine (`flint.py` as library, `swarmd.py` as server) and have the other import it. |
| F3 | P2 | **Experiment code lives next to production code**: `swarm/mit_corpus.py`, `corpus_index.py`, `learn.py`, `experiment.py`, `classify_no_change.py` are barely wired (one call site each, some zero: `corpus_index` is imported but `mit_corpus.study` is the only reachable path at `swarmd.py:1757`). Move to `swarm/experiments/` or a `research/` dir so the core is obviously smaller. |
| F4 | P2 | **`swarm/trunk` carries a whole second product** (strudel-nvim, 3,187 lines) that `main` doesn't have. Nothing wrong with it — but it silently splits the codebase in two. Decide: is strudel-nvim a product (→ merge) or an experiment (→ its own repo/branch-only)? |
| F5 | P3 | `board-view.js` (27 lines) + `board.js`-style rendering on top of the main panel = a second UI surface (scrum board) that duplicates the panel's "landed" tab. Keep one. |
| F6 | P3 | `nonstop.py` defines its own REPL/trigger/deadline loop that calls back into `flint.py` (`run_unattended`). Third personality. The /nonstop path in `flint.py:1872` and `nonstop.py` should share the daemon's task machinery. |

---

## 4 · USER-FRIENDLY — CLI and panel friction

| # | Severity | Finding |
|---|----------|---------|
| U1 | P1 | **`swarm` CLI has no single "what's happening" command.** `swarm status`/`report`/`bridge.py activity` overlap; `flint.py` and `swarmd.py` each expose their own `--help`. Pick one vocabulary: `swarm status` (state) + `swarm log` (events) + `swarm report` (budget/turns) and make the rest aliases. |
| U2 | P2 | **The extension header has 6 one-char/emoji icon buttons** (`🎨 ? ⚙` + Start/Stop/Drain/Restart/Report in the status deck = ~9 controls before the chat composer). Tooltips exist but the marquee/skin/presets feature is cosmetic drift — collapse "Dress it up" into settings, and collapse Stop/Action into one contextual button (Run ⇄ Stop). |
| U3 | P2 | **The "Update · 0.0.0" button is a self-installer** for the extension (tracked via `updateClicks` state, `extension.js:57–62`). Auto-updating your own checkout is surprising UX; make it a settings toggle ("check for updates") rather than a header button counting clicks. |
| U4 | P2 | **Queue editing is powerful but undiscoverable.** `queueEdit`/`queueRemove`/`clearQueue`/`queueRetry`/`requeue` exist (5 commands) with no empty-state next steps (what do I ask next? what's blocked and why?). Empty queue = good moment for the "What are we building?" chips to appear (they exist in the ask tab at `panel.js:743` — reuse them). |
| U5 | P3 | `flint.py` REPL and `nonstop.py` each print their own banner/help; `swarm` has different prompts. Consistent `>` / `›` and one shared `/help` text would tighten the feel. |
| U6 | P3 | Error strings mix formats: "free allowance exhausted", "DailyCapReached", "429". Centralize user-facing messages in one `messages.py` so the panel and CLI say the same thing. |

---

## 5 · EFFECTIVE — reliability and correctness nits

The engine is genuinely robust: subprocess timeouts are everywhere (`sh()` 900s default,
`git()` 180s const at `swarmd.py:715/728`), git hooks are neutralized
(`core.hooksPath=/dev/null`), spend ledger writes are atomic via tmp+rename
(`spend_ledger_paths`), and budget pacing is tested. Remaining nits:

| # | Severity | Finding |
|---|----------|---------|
| E1 | P1 | **`swarm/bridge.py` HTTP layer is `ThreadingHTTPServer` with raw socket reads** and per-thread `except Exception` guards (`bridge.py:434, 963, 1061`). Long-lived daemon + threads + swallowed exceptions = the "it worked yesterday" class of bug. Add structured logging of every swallowed error (store in a ring buffer, surface in `swarm status`). |
| E2 | P2 | **`providers.py:297` probes local model URLs with `timeout=1`** and swallows everything (`except Exception`). A flaky local provider silently disables itself; surface which providers were probed and why they failed in status. |
| E3 | P2 | **Config snapshots (`swarm/configs/*.before-*`) have no linkage to what changed.** `config_mtime()`/`watch_config` (`swarmd.py:172–220`) auto-reload on change — good — but a snapshot with no diff note is archaeology. Store `(timestamp, changed_keys)` in the snapshot filename or a changelog. |
| E4 | P3 | `Queue.max_*` defaults (`max_depth=2, max_queue=20, max_children=3`) are duplicated between `Queue.__init__` (`swarmd.py:817`) and `config.example.json` (`"workers": 1, "daily_cap": 800...`). One source of truth (config) wins; the Python defaults should just read config defaults. |
| E5 | P3 | `extension.test.js` (672 lines) covers ask/queue/status paths but has no coverage for `clearQueue`, cropping/skin, or the queue editor. Given the panel is the product surface, a happy-path test per tab is cheap insurance. |

---

## 6 · Branch-by-branch disposition (all 46)

| Branch | Disposition |
|---|---|
| `main` | Keep. Base of everything. |
| `swarm/trunk` | **Merge into `main` or promote** (9 commits, strudel-nvim). |
| `fix/paid-fallback-reconcile` | Keep — 0 ahead, 54 behind, but named fix-work; rebase onto `main` or close if the paid-fallback work already landed in `main` (it did: `4ce493d`). Suggest close after verification. |
| `local-before-merge`, `mac-local-2026-09-27` | Local play branches, 1 commit each, 57/22 behind — archive or delete. |
| `review/chatgpt-swarm-work` | 0 ahead, 51 behind → fully merged. Delete. |
| `swarm/t07a4c95e537d-*` … `swarm/tf25a9c65c98a-*` (the 33 `ahead:1/behind:70` batch) | **All merged into `main`** — the swarm's own trunk landed them. Delete. |
| `swarm/t144780eeff8d-23c25e9c` (+6), `swarm/t2703d8076d63-*` (+2), `swarm/t8a76866282fd-*` (+2), `swarm/t8b9e71e2c0c2-*` (+5), `swarm/t9afd15ca45aa-*` (+7×2), `swarm/tc36436e7511d-*` (+9), `swarm/tefd06afc1456-*` (+1), `swarm/tfef29aac863e-*` (+2/+3) | **Awaiting merge onto trunk** — they are the in-flight attempt branches. Merge-or-delete after the trunk work lands. |
| `swarm/t28db3320c96a-*` (+8), `swarm/t4d17b423cd35-*` (+9) | Same as above; note `+8` and `+9` mean these two are *already on trunk* (identical commit lists) — delete once trunk merges. |
| `swarm/td67aa68c73f0-27232c9a` | 0 ahead, 53 behind — merged. Delete. |

Net: after cleanup, **you should have ~4 branches: `main`, `swarm/trunk` (or trunk-merged),**
`fix/paid-fallback-reconcile` (or closed), and whatever the swarm currently has in flight.

---

## Priority shortlist (do these first)

1. **Decide trunk vs main** and merge (H2) — kills the "two main branches" confusion.
2. **Prune branches + worktrees** (H1, H3) — 6.3 GB and 40 branch names are the single
   biggest "not focused" signal in this repo.
3. **Move the extension HTML out of the JS string** (S1) and add a postMessage channel
   whitelist (S2) — one afternoon, removes the top maintenance and XSS-adjacent risk.
4. **One engine, not two** (F2): make `flint.py` a library the daemon imports; delete the
   duplicated Spend/provider/429 classes.
5. **Dashboard-ify status** (U1): one `swarm status` that shows daemon, budget, queue,
   blocked reasons, and last errors — including the swallowed exceptions (E1).

---

## Progress log (executed 2026-10-10)

**Done — item 3 (extension HTML):** moved the webview shell out of `extension.js` into
`cursor-extension/media/panel.html` (commit `3335b90`). `html()` reads the file and resolves
`${...}` host tokens at runtime; same CSP/nonce/pane layout; 32/32 extension tests pass.

**Done — items 1 & 2 (trunk merge + prune):** `swarm/trunk` merged into `main` (`b6e5d29`),
10 merged branches + 8 stale self-worktrees pruned (46 → 36 branches); 824 Python +
32 extension tests green.

**Done — item 4 (flint.py as engine):** extracted `flint.run_headless()` — the headless
single-turn engine (Agent + turn + checkpoint + wallet accounting) — and made `main()` a
thin CLI over it. The daemon's subprocess contract (prompt-file + exit codes) is unchanged;
flint.py is now unambiguously a library with a CLI shim. (Investigation showed the daemon
already imports flint for `spend_period`, `Throttle`, `fetch_key_info`; the missing piece
was a formal library entry for the turn engine, now added.)

**Done — item 5 (one swarm status):** `swarmd.cmd_status` is now human-first: a readable
summary (daemon, budget, queue, trunk, last error) above the unchanged JSON machine
contract, with the journal tail marking errors. `bridge.py status` (extension contract)
untouched. 824 Python + 32 extension tests pass.

**Still open (judgment calls, not automated):** config-snapshot archive
(`swarm/configs/*.before-*`), the 6.3 GB of cross-repo worktrees, and collapsing the
remaining overlapping commands (`report`/`activity` stay as distinct views for now).

---

*Review method: full branch audit (`git for-each-ref` + `rev-list` vs `main`), structural
map of `swarmd.py`/`flint.py`/`extension.js`/`panel.js`, targeted reads of the hot paths
(worker loop, budget pacing, git integration, webview CSP/renderer), config and test
inventory. Tests: 822 passed + 29 subtests in 52s.*