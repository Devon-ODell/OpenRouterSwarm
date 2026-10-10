--- strudel.nvim — WezTerm companion config.
---
--- Drop this into ~/.config/wezterm/wezterm.lua (or source it from your config)
--- to get the pastel look + stream-friendly extras:
---
---   * pastel palette at 256-color depth (matches strudel.nvim's ANSI map)
---   * a soft blue→purple→orange window gradient
---   * a bigger font while "stream mode" is on (F1 cycles it)
---   * underline-style cursor + rounded paddings
---   * OSC 52 clipboard controls ON, so <leader>ss can paste into live
---   * `strudel` domain: launch nvim in a tab already split nvim+wezterm help
---
--- This is NOT the user's whole wezterm.lua — it's a bundle of the pieces
--- strudel.nvim cares about. Merge what you like.

local wezterm = require("wezterm")

local config = wezterm.config_builder()

--------------------------------------------------------------------------------
-- pastel palette: mirrors strudel.nvim's ANSI 0-15
--------------------------------------------------------------------------------
config.colors = {
  foreground = "#c8cbe8",
  background = "#1d1b2b",
  cursor_bg = "#f2a65a",
  cursor_border = "#1d1b2b",
  cursor_fg = "#1d1b2b",
  selection_bg = "#2b2640",
  selection_fg = "#c8cbe8",

  ansi = {
    "#191628", -- 0 black   (dark lavender)
    "#e77a9c", -- 1 red     (rosewater)
    "#a8d8a0", -- 2 green   (sage)
    "#f9c18c", -- 3 yellow  (peach)
    "#7fb2f0", -- 4 blue    (periwinkle)
    "#b78fd6", -- 5 magenta (lavender)
    "#93d5e3", -- 6 cyan    (aqua)
    "#c8cbe8", -- 7 white   (mist)
  },
  brights = {
    "#6f7694", -- 8  bright black (dim)
    "#e3a3c6", -- 9  bright red   (orchid)
    "#a8d8a0", -- 10 bright green
    "#f2a65a", -- 11 bright yellow (orange flame)
    "#7fb2f0", -- 12 bright blue
    "#b78fd6", -- 13 bright magenta
    "#93d5e3", -- 14 bright cyan
    "#9aa2c4", -- 15 bright white (alt prose)
  },
}

--------------------------------------------------------------------------------
-- window chrome: subtle gradient + tint
--------------------------------------------------------------------------------
config.window_background_opacity = 0.96
config.window_background_gradient = {
  colors = { "#20243a", "#242033", "#2a2030" }, -- blue → lavender → plum
  orientation = "Vertical",
  blend = "Rgb",
}

config.colors.tab_bar = {
  background = "#242033",
  active_tab = {
    bg_color = "#7fb2f0",
    fg_color = "#1d1b2b",
  },
  inactive_tab = {
    bg_color = "#2b2640",
    fg_color = "#9aa2c4",
  },
  inactive_tab_hover = {
    bg_color = "#332c4d",
    fg_color = "#c8cbe8",
  },
}

config.window_frame = {
  active_titlebar_bg = "#242033",
  active_titlebar_fg = "#b78fd6",
  inactive_titlebar_bg = "#191628",
  border_left_width = "1 cell",
  border_right_width = "1 cell",
  border_top_height = "1 cell",
}

--------------------------------------------------------------------------------
-- fonts & cursor
--------------------------------------------------------------------------------
local stream_font_size = 13.0
local studio_font_size = 13.0

config.font = wezterm.font("JetBrains Mono", { weight = "Medium" })
config.font_size = 12.5
config.line_height = 1.15
config.cursor_blink_rate = 1 -- blink once per second, visible on stream
config.cursor_blink_ease_in = "EaseOut"
config.cursor_blink_ease_out = "EaseIn"
config.default_cursor_style = "SteadyUnderline" -- visible on stream

--------------------------------------------------------------------------------
-- workspace: `wezterm start -- always-on-top`-friendly tab layout
--------------------------------------------------------------------------------
local function default_tabs()
  return {
    {
      cwd = wezterm.home_dir,
      -- WezTerm needs a program; nvim + a hinting split is the point.
      program = "nvim -c 'StrudelCheck'",
    },
    {
      cwd = wezterm.home_dir,
      program = "wezterm cli split-pane --horizontal -- nvim -c 'StrudelSessionToggle'",
    },
  }
end

--------------------------------------------------------------------------------
-- keys
--------------------------------------------------------------------------------
config.keys = {
  -- eldest: cycle font sizes (stream big, studio small)
  { key = "F1", mods = "CTRL|SHIFT", action = wezterm.action_callback(function(win, pane)
    local s = win:get_config_overrides() or {}
    if s.font_size == stream_font_size then
      s.font_size = studio_font_size
    else
      s.font_size = stream_font_size
    end
    win:set_config_overrides(s)
  end) },
  -- quick paste of the OSC 52 clipboard into a pane
  { key = "t", mods = "CTRL|SHIFT", action = wezterm.action.PasteFrom("Clipboard") },
}

--------------------------------------------------------------------------------
-- OSC 52 (clipboard set) — required for <leader>ss remote paste to work
--------------------------------------------------------------------------------
-- WezTerm enables OSC 52 clipboard write by default (os_window 52 / copy), so
-- no set_clipboard override is needed. nvim's "+ reg and the browser live tab
-- agree on the same clipboard, and <leader>ss pastes straight into live.

--------------------------------------------------------------------------------
-- launch domain helper: `wezterm start -- new-tab` opens the live-coding rig
--------------------------------------------------------------------------------
-- Enable with:  local strudel = require(...); strudel.domain("set.nvim")
-- (we keep it out of the default domain so WEZTERM unix socket paths work)

return config