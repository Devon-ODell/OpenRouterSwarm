# AGENTS.md — strudel.nvim

Operating manual for machine agents working on **strudel.nvim**, the Neovim
livecoding plugin inside `OpenRouterSwarm/strudel-nvim/`. Read this before you
edit, test, or debug anything in this directory. It records the layout, the
verified install state on the owner's machine, known sharp edges, and every
reproducible test.

> Status of this doc: verified against the current repo on the owner's machine
> (WezTerm 20240203, nvim 0.12.5, macOS arm64). If you change a behaviour,
> update the relevant section and re-run the tests in §Testing.

---

## 1. What this is

A zero-dependency Neovim plugin for **Strudel** livecoding (strudel.cc) sets:
a human plays 2h sets or streams 4–6h while an OpenRouter agent
(`z-ai/glm-5.3-flash`) writes/evolves the set in the editor. The whole thing
shares one pastel blue/purple/orange palette across editor theme, statusline,
and WezTerm.

- No plugin manager: the installer symlinks files into `~/.config/nvim`.
- No external deps at runtime: uses `curl` (via `vim.fn.system`/`jobstart`) for
  OpenRouter, plain Lua for everything else.
- Streams chat into a float window, keeps a per-session ⚡ cost meter, and has
  a `strudel` filetype that sends code to the live browser tab via OSC 52.

### File map

| Path | Role |
|---|---|
| `plugin/strudel.lua` | one-shot entry: `require("strudel").setup()` |
| `lua/strudel/init.lua` | setup(), autocmds, filetype, timers, keymaps, user commands |
| `lua/strudel/config.lua` | defaults + override merge (`vim.g.strudel_*` > setup{} > defaults) |
| `lua/strudel/theme.lua` | 3 palettes (blue/purple/orange), statusline + ANSI map |
| `lua/strudel/statusline.lua` | powerline, ⚡ meter, set clock |
| `lua/strudel/strudel.lua` | `strudel` filetype, OSC 52 send, snippet library (7 snippets) |
| `lua/strudel/agent.lua` | OpenRouter driver: jget/jpost/complete/chat/check/meter |
| `after/ftplugin/strudel.lua` | filetype-local options |
| `syntax/strudel.vim`, `ftdetect/strudel.vim` | pastel syntax + `*.strudel`/`*.ss` detection |
| `wezterm/wezterm.lua` | companion WezTerm palette/keys bundle |
| `tests/run.sh` + `tests/*.lua` | headless test suite (see §Testing) |
| `install.sh` | symlink installer (`--link` default, `--copy`, `--uninstall`) |

---

## 2. Install state (verified)

The plugin is **symlinked** into the owner's `~/.config/nvim` (not copied):

```
~/.config/nvim/
  plugin/strudel.lua        -> <repo>/plugin/strudel.lua
  ftdetect/strudel.vim      -> <repo>/ftdetect/strudel.vim
  syntax/strudel.vim        -> <repo>/syntax/strudel.vim
  lua/strudel               -> <repo>/lua/strudel
  after/ftplugin/strudel.lua-> <repo>/after/ftplugin/strudel.lua
```

### ⚠️ Known installer bug (fixed, but be careful)

`install.sh` previously computed `SRC="$ROOT/strudel-nvim"` — a **nested** path
that does not exist — so every symlink was broken and the plugin silently never
loaded. It now checks `if [ -d "$ROOT/strudel-nvim/plugin" ]` and falls back to
`SRC="$ROOT"`. **If you ever move this repo, re-run `install.sh` and verify the
links resolve** — a fresh agent will otherwise swear the plugin is installed
while `:StrudelCheck` can't even load.

Verify with:

```sh
NVIM_DIR="$HOME/.config/nvim"
for p in plugin/strudel.lua ftdetect/strudel.vim syntax/strudel.vim lua/strudel after/ftplugin/strudel.lua; do
  test -e "$NVIM_DIR/$p" && echo "OK  $p" || echo "BROKEN $p"
done
```

`install.sh` is idempotent; re-running is safe. `--copy` makes real files
instead of links; `--uninstall` removes them.

---

## 3. Environment

- **API key**: `OPENROUTER_API_KEY` is the only required env var. It lives in
  `OpenRouterSwarm/.env` (`OPENROUTER_API_KEY=sk-or-...`) and is exported from
  `~/.zshrc`. `lua/strudel/config.lua` falls back to
  `os.getenv("OPENROUTER_API_KEY")` or the `strudel_api_key` override.
- **Key must be picked up in a fresh shell**: `source ~/.zshrc` or open a new
  terminal. Headless nvim started from a shell that predates the export will
  see no key and `:StrudelCheck` will report MISSING.
- The plugin never hardcodes secrets. Keep it that way.

---

## 4. Commands & keymaps (user-facing)

Commands (defined in `init.lua` / `agent.lua` `setup()`):

| Command | What it does |
|---|---|
| `:StrudelCheck [--local]` | health: key, model, reachability, pricing |
| `:StrudelChat` | streaming chat float, history kept between turns |
| `:StrudelAsk <prompt>` | one-shot; answer pasted into buffer |
| `:StrudelMode [generate\|drift\|chill\|fix]` | change agent mood live |
| `:StrudelMeter` | toggle ⚡ cost meter in statusline |
| `:StrudelStop` | kill in-flight streams |
| `:StrudelTheme [blue\|purple\|orange]` | swap palette |
| `:StrudelSend <code>` | raw one-liner to live |
| `:StrudelSessionToggle` | session clock / mksession helper |
| `:StrudelReload` | reload plugin modules |

Default keymaps (configurable via `vim.g.strudel_keys.<k>`):

| Key | Action |
|---|---|
| `<leader>ss` | send selection / line / buffer to live |
| `<leader>sa` | send whole set |
| `<leader>sk` | snippet picker (7 snippets) |
| `<leader>ac` | open chat float |
| `<leader>aq` | ask about cursor line |
| `<leader>at` | toggle ⚡ meter |
| `<leader>ax` | stop streams |

---

## 5. Config model

`require("strudel").setup{...}` merges, in ascending precedence:
**defaults** → **setup{} opts** → **`vim.g.strudel_<key>`**. See
`lua/strudel/config.lua` for the full list. Highlights:

```lua
require("strudel").setup {
  model = "z-ai/glm-5.3-flash",  -- on OpenRouter
  temperature = 0.65,
  max_tokens = 2200,
  timeout = 90,
  budget_usd = nil,              -- hard session budget in USD
  api_key = os.getenv("OPENROUTER_API_KEY"),
  base_url = "https://openrouter.ai/api/v1",
  max_history_chars = 200000,    -- history trim for the 1M ctx window
  mode = "generate",             -- generate | drift | chill | fix
}
```

Changing the model: update `model`, and ideally the hardcoded price constants
in `agent.lua` `acc(usage)` (they're glm-5.3-flash's list prices). **If the
meter shows wrong costs, that's where to look.**

---

## 6. Testing (headless, no GUI)

The whole suite runs with plain `nvim --headless`; no display or wezterm needed.

```sh
cd OpenRouterSwarm/strudel-nvim && bash tests/run.sh
```

What it covers:

- **`tests/smoke_test.lua`** — plugin loads, keymaps exist, snippets = 7,
  theme cycles, meter state, mode switch.
- **`tests/check_test.lua`** — offline health check never touches network
  (`agent.check_local()` must return <1s even with a dead network), key present
  vs MISSING, model row mentions `z-ai/glm-5.3-flash`.
- **`tests/wezterm_parity_test.lua`** — the 16 ANSI colors in
  `wezterm/wezterm.lua` must exactly match `theme.lua`'s `terminal_color_*`
  for the blue variant.

Manual headless probes (reusable for debugging):

```sh
# offline health
nvim --headless -u NONE --cmd "set rtp+=<repo>" \
  -c "lua require('strudel').setup(); local a=require('strudel.agent'); for _,r in ipairs(a.check(true)) do print(r[1], r[2]) end" -c qa

# network health (needs OPENROUTER_API_KEY)
nvim --headless -u NONE --cmd "set rtp+=<repo>" \
  -c "lua require('strudel').setup(); local a=require('strudel.agent'); for _,r in ipairs(a.check(false)) do print(r[1], r[2]) end" -c qa

# one-shot completion straight through the plugin
nvim --headless -u NONE --cmd "set rtp+=<repo>" \
  -c "lua require('strudel').setup(); local a=require('strudel.agent'); print(a.complete('say pong'))" -c qa
```

WezTerm config validation (the real binary from the app bundle):

```sh
/Applications/WezTerm.app/Contents/MacOS/wezterm-gui \
  --config-file "$HOME/.config/wezterm/wezterm.lua" ls-fonts
```

A clean font listing = config valid. Any `ERROR ... Config::from_dynamic`
line = the config breaks this WezTerm version.

---

## 7. Known issues & how to troubleshoot (read before touching)

### 7.1 `:StrudelCheck` network half — was a false `http 22`, now falls back

OpenRouter **404s `GET /models/z-ai/glm-5.3-flash`** (the slug exists in
`/models` and works for chat completions, but isn't individually addressable).
The old `check()` treated that 404 as an auth failure ("http 22 — check
OPENROUTER_API_KEY") even though everything worked.

**Fixed**: `check()` now tries the per-model GET; on failure it sends a
1-token chat completion probe. Output when healthy prints
`auth ok — chat/completions reachable (per-model GET 404'd)`. The probe costs
~nothing and charges the meter — minor and intentional.

Troubleshoot auth with curl directly:

```sh
KEY=$(grep '^OPENROUTER_API_KEY=' OpenRouterSwarm/.env | cut -d= -f2-)
curl -sS -m 30 -H "Authorization: Bearer $KEY" -d '{"model":"z-ai/glm-5.3-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' \
  https://openrouter.ai/api/v1/chat/completions
```

- HTTP 401 → key wrong/revoked. HTTP 402 → budget limit. HTTP 404 on
  `/models/<slug>` → normal for this slug, ignore. Anything else → network.

### 7.2 glm-5.3-flash streams `content:""` + `reasoning` — was silent chat

Live wire format (confirmed against OpenRouter while polishing this repo): the model emits
`delta.content == ""` with `delta.reasoning` chunks, then the actual
`content`. The old stream handler required truthy `delta.content`, so the chat
float sat on an empty "glm:" line and timeouts made it look broken.

**Fixed** in `chat_send`'s `on_stdout`:
- `content == ""` (or nil) + `reasoning` present → append reasoning to the
  float, prefixed once with `⸙ ` at the reasoning→content transition
  (`reasoning_phase` local tracks the boundary so you don't get `⸙ ⸙ ⸙`).
- `M.complete()` also falls back to `message.reasoning` when `content` is nil,
  so `:StrudelAsk` returns text instead of "empty completion".

If responses still look empty: check `:StrudelMeter` cost went up (proves the
call happened) and dump the raw curl stream to see the delta shape.

### 7.3 WezTerm version drift — the companion config broke on 20240203

`wezterm/wezterm.lua` was written against newer APIs. On the owner's install
(WezTerm **20240203-110809-5046fc22**) it failed validation on four keys.
Both `~/.config/wezterm/wezterm.lua` and the repo copy are now fixed:

| Old (broke) | New (works on 20240203) |
|---|---|
| `window_background_gradient.start = "top"` | `orientation = "Vertical"` |
| `window_background_gradient.blend = "screen"` | `blend = "Rgb"` (enum: Hsv\|LinearRgb\|Oklab\|Rgb) |
| `cursor_blink_interval = 0.5` | `cursor_blink_rate = 1` (u64, no decimals) |
| `cursor_style = "SteadyUnderline"` | `default_cursor_style = "SteadyUnderline"` |
| `action.PasteFrom("clipboard")` | `action.PasteFrom("Clipboard")` |
| `config.set_clipboard = function(...)` | removed — OSC 52 write is on by default in this WezTerm |

Rule discovered by probing: this WezTerm uses **capitalized enum strings**
(`"Vertical"`, `"Rgb"`, `"Clipboard"`), and any unknown field produces a
`Config::from_dynamic` error naming the valid alternatives. When validating a
config against a *newer* wezterm, expect these enum names to be stable but
fields like `set_clipboard` to be re-supported — test before reverting.

### 7.4 Installer path bug (see §2)

### 7.5 OSC 52 send-to-live requires WezTerm (or a terminal with OSC 52 write)

`<leader>ss` pastes into the browser live tab through the terminal's clipboard
bridge. Without WezTerm, the OSC 52 write won't reach the browser. The owner's
machine has WezTerm at `/Applications/WezTerm.app`, CLI symlinked to
`/opt/homebrew/bin/wezterm` (note: `/Applications/.../wezterm` needs
`wezterm-gui` next to it; the symlink CLI can't find it, so use
`/Applications/WezTerm.app/Contents/MacOS/wezterm-gui` for config validation —
see §6).

---

## 8. Architecture notes for agents (the important mental model)

- **`plugin/strudel.lua`** is a 1-line loader. Real setup lives in
  `init.lua:setup()` which requires `theme`, `strudel`, `agent` lazily.
- **`agent.lua` is the heart.** It keeps module-level `cfg` (set by
  `M.setup()`) and `U` (usage meter). **Never call `agent.check()` /
  `complete()` before `require("strudel").setup()` — `cfg` is nil and you'll
  get a cryptic "attempt to index upvalue 'cfg' (a nil value)" at
  `agent.lua:check_local()`. This is the #1 headless-test footgun.**
- HTTP is plain `curl` (no plenary/vim.curl):
  - `jget(path)` → `vim.fn.system` (blocking, `--fail-with-body`).
  - `jpost(path, body)` → writes JSON to a temp file, `-d @tmp`, blocking.
  - `chat_send()` streams via `vim.fn.jobstart` + `curl -N`, appending to the
    float buffer's last line.
- The meter (`U`) accumulates from **response `usage`**, using hardcoded list
  prices in `acc()`. Panel reads `vim.g.strudel_agent` (see `statusline.lua`).
- `ftdetect/` only maps `*.strudel`/`*.ss`; a `FileType` autocmd re-maps
  js/ts buffers whose *filename* ends in those extensions.
- Snippets are plain Lua data in `strudel.lua` `M.snippets` (7 entries,
  asserting count is part of the smoke test — add/remove updates the test).

---

## 9. Rule of thumb for this repo

1. **Symlinked install** — edits to the repo are live in nvim instantly; there
   is no build step. But broken links look like a missing plugin.
2. **Headless nvim is the only test rig** — never claim a change works without
   `tests/run.sh` passing, and run a network `:StrudelCheck` probe when you
   touch `agent.lua`.
3. **Keep `wezterm/wezterm.lua` in sync with `~/.config/wezterm/wezterm.lua`**
   (the installed copy); the parity test only checks colors, not the whole
   config.
4. Don't hardcode secrets; don't add dependencies — the "zero-dep, curl + Lua"
   constraint is the plugin's identity and its testable property.