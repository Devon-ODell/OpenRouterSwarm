# STRUDEL-NVIM HARDENING PACKET — for the OpenRouterSwarm queue

The plugin lives in `strudel-nvim/` in this repo. It's a Neovim plugin
optimizing the Strudel livecoding experience in WezTerm, driven headlessly by
`z-ai/glm-5.3-flash` on OpenRouter.

Everything below is shaped like AGENTS.md packets: small, single-objective,
verifiable by running code (`nvim --headless` is available on this machine at
`/opt/homebrew/bin/nvim`, and the plugin ships `tests/run.sh`).

---

## Task 1 — check command offline split

**Title:** `Add :StrudelCheck --local so the health check runs offline`

**Detail:**
- Open `strudel-nvim/lua/strudel/agent.lua`. `M.check()` currently does a
  network `jget("/models/" .. cfg.model)`.
- Split into `check_local()` (key presence, model string, config sanity — no
  sockets) and the existing network path.
- `:StrudelCheck` keeps doing both; `:StrudelCheck --local` runs only the
  local half (no curl spawned).
- Add `strudel-nvim/tests/check_test.lua` proving it.

**Acceptance:**
1. `cd strudel-nvim && nvim --headless -u NONE --cmd "set rtp+=$PWD" -c "luafile tests/check_test.lua" -c "qa"` exits 0 and prints `CHECK LOCAL PASS`.
2. With `OPENROUTER_API_KEY` unset, `:StrudelCheck --local` reports a missing key and the test asserts that.
3. The local path never invokes curl: assert via `vim.fn.system("pgrep -P $$ | grep curl")` returning empty while running check_local with a 2s fake sleep.

---

## Task 2 — repeatable headless test suite

**Title:** `Make tests/run.sh green end-to-end and wire it into the repo's test story`

**Detail:**
- `strudel-nvim/tests/run.sh` already runs `smoke_test.lua` and
  `wezterm_parity_test.lua`. Add `check_test.lua` to the loop.
- The runner should `set -euo pipefail`, use `${NVIM:-nvim}`.
- No plugin manager, no luarocks — stock nvim 0.10+.

**Acceptance:**
1. `strudel-nvim/tests/run.sh` exits 0.
2. Output contains `ALL SMOKE TESTS PASSED`, `PALETTE PARITY OK`, and
   `CHECK LOCAL PASS`.
3. A fresh git checkout of only `strudel-nvim/` passes all three (the plugin
   must not depend on anything else in this monorepo).