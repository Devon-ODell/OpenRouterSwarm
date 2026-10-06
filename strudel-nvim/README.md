# strudel.nvim

Neovim, tuned for the **Strudel** livecoding language (strudel.cc), running in
**WezTerm**, driven by an **OpenRouter agent** — `z-ai/glm-5.3-flash` — while a
human plays 2-hour sets or livestreams for 4–6 hours.

The whole thing is a pastel **blue / purple / orange** system: the editor
theme, the statusline meter, the WezTerm palette and window gradient all speak
the same color language, so on stream it reads as one coherent piece of stage
lighting.

---

## What's inside

| Piece | File | What it does |
|---|---|---|
| Theme (3 variants) | `lua/strudel/theme.lua` | lavender-night bg, periwinkle prose, orange flame for numbers + the ⚡ meter. `blue` / `purple` / `orange`, hot-swappable with `:StrudelTheme`. Also paints ANSI 0–15 for the terminal. |
| Statusline + winbar | `lua/strudel/statusline.lua` | zero-dependency powerline: mode chip (`GEN`/`DRIFT`/`CHILL`/`FIX`), file + branch, then right-aligned **⚡ glm meter** and the **set clock** (HH:MM:SS counting up through hour 6). |
| Strudel mode | `lua/strudel/strudel.lua` + `ftdetect/` + `syntax/` | real `strudel` filetype for `*.strudel`/`*.ss`, pastel syntax, OSC 52 "send to live" with an optional `strudel_pad` so the music keeps playing mid-edit, snippet picker. |
| Agent driver | `lua/strudel/agent.lua` | OpenRouter chat with glm-5.3-flash: streaming float chat, one-shot asks, health check, and a live **session spend** tracker (list prices per token → cents in the statusline). |
| WezTerm config | `wezterm/wezterm.lua` | the same pastel palette at 256 depth, blue→plum window gradient, stream font toggle, steady underline cursor, OSC 52 enabled. |
| Installer | `install.sh` | symlinks everything into `~/.config/nvim`, no plugin manager required. |

## The agent driver

The plugin is built around the idea that **glm-5.3-flash is the touring
member of the band** — it has a 1M context window, costs ~$0.15/M prompt and
~$0.50/M completion, and can keep a livecoding narrative straight for hours.

```lua
require("strudel").setup {
  model = "z-ai/glm-5.3-flash",   -- on OpenRouter
  temperature = 0.65,             -- < .7 keeps sets coherent, > .9 gets spicy
  budget_usd = 2.0,               -- hard session budget, shown as a % in the meter
  mode = "generate",              -- generate | drift | chill | fix
  api_key = os.getenv("OPENROUTER_API_KEY"), -- or set strudel_api_key
}
```

Commands:

- `:StrudelCheck` — key present? model reachable? what does it cost?
- `:StrudelChat` — streaming chat float, history kept between turns
- `:StrudelAsk <prompt>` / `<leader>aq` — one-shot, answer pasted into the buffer on the cursor line
- `:StrudelMode [generate|drift|chill|fix]` — change the system-prompt mood live
- `:StrudelMeter` / `<leader>at` — toggle the ⚡ cost meter
- `:StrudelStop` / `<leader>ax` — kill in-flight streams

The **mood modes** rewrite the driving instructions:

| mode | atmosphere |
|---|---|
| `generate` | keep the crowd moving: new phrases, smart dissonance, resolve. DRIVE. |
| `drift` | textural pads, sparse percussion, let the loop breathe. |
| `chill` | minimal, warm, slow. The DJ dropped it low. |
| `fix` | surgical: find what's broken and repair it, one small change at a time. |

## Sending code to live

Strudel live runs in a browser tab. On WezTerm, OSC 52 paste is enabled, so:

- `<leader>ss` — send visual selection (or current line) into the focused live tab
- `<leader>sa` — send the whole set
- `<leader>sk` — snippet picker (`hero`, `arp`, `beat_algo`, `lfo wobble`, `palindrome`, …)
- `StrudelSend <code>` — raw one-liner

`strudel_pad = " "` (configurable) pastes a tiny breather after your code so
the loop doesn't stutter while you type the next phrase.

## Colors — the three palettes

All three share the same "bone structure"; the accent colors shift.

| token | blue (default) | purple | orange |
|---|---|---|---|
| background | `#1d1b2b` | `#221e33` | `#2a2030` |
| prose | `#c8cbe8` | `#cdc5ea` | `#dccfb5` |
| hot accent (numbers, meter) | `#f2a65a` | `#f4a75c` | `#f5a855` |
| cool accent (structure) | `#7fb2f0` | `#8fa8e8` | `#7faee8` |
| lavender (keywords) | `#b78fd6` | `#c99be0` | `#c19ad4` |
| aqua (strings) | `#93d5e3` | `#98c8de` | `#8fc9d5` |

`transparent = true` option drops the `Normal` background and lets the WezTerm
gradient show through — a favorite for 6-hour streams.

## Streaming extras

- **Set clock**: starts on `VimEnter`, `StrudelSessionToggle` (or `<leader>st`) to restart. Counts HH:MM:SS past hour 2 without rolling over to days.
- **Big cursor**: `Cursor` is a peach block, `CursorLineNr` orange — the stream viewer always knows where the agent is typing.
- **`:StrudelTheme`** cycles the palette live mid-stream.

## Install

```bash
cd strudel-nvim
./install.sh          # symlinks into ~/.config/nvim
# or for lazy.nvim:
#   { dir = "~/Desktop/OpenRouterSwarm/strudel-nvim", name = "strudel" }
```

Then open a set:

```bash
nvim set.strudel      # done — pastel, meter on, clock running
:StrudelCheck         # confirm the API key + model reachability
:StrudelChat          # "write me a 4-bar heartbeat for a 92bpm techno set"
<leader>ss            # drop it into the live tab
```

## Notes for agent operators

- The statusline guard is `vim.g.strudel_agent` — the meter reads
  `vim.g.strudel_agent.cost / budget / visible`.
- The current mode is always in `vim.g.strudel_mode`.
- `:StrudelAsk` lands one-shot answers *in the buffer* — perfect for a
  headless driver that wants outputs it can diff, and for the human who wants
  prompts the crowd can read along with.
- Everything is reloadable: `:StrudelReload` re-reads config and re-applies.

## License

Unlicense — do anything.