--- strudel.nvim — the "albatross" pastel theme.
---
--- Three sister palettes with a shared bone structure: warm lavender
--- backgrounds, cool periwinkle foregrounds, and orange for exactly the things
--- that pop (numbers, keywords, the ⚡ glm meter).
---
---   * "blue"   — default: periwinkle/cornflower blues
---   * "purple" — lavender/violet, slightly deeper backgrounds
---   * "orange" — peach/salmon sunlight, friends with terminal transparency
---
--- The base16-like highlightscript is explicit per-group, so the headless
--- agent and the human see the same colors in any terminal that sets
--- termguicolors (we set it ourselves).

local M = {}

--- Paste-friendly cheat sheet (also rendered by :StrudelTheme for a live demo).
M.palette = {
  bg      = "#1d1b2b", -- deep lavender night
  bg_alt  = "#242033", -- panels, floats, diff
  bg_soft = "#2b2640", -- line numbers, folds
  bg_dark = "#191628", -- inactive window background
  fg      = "#c8cbe8", -- periwinkle mist
  fg_alt  = "#9aa2c4", -- muted prose
  fg_dim  = "#6f7694", -- comments, placeholders
  blue    = "#7fb2f0", -- periwinkle / cornflower
  cyan    = "#93d5e3", -- aqua accents
  green   = "#a8d8a0", -- sage (used sparingly: operators, diff-add)
  purple  = "#b78fd6", -- lavender / violet
  pink    = "#e3a3c6", -- orchid, links
  orange  = "#f2a65a", -- number-line flame
  peach   = "#f9c18c", -- soft highlights, warnings
  red     = "#e77a9c", -- rosewater errors, diff-del
  gray    = "#8b90ad",
}

-- One attribute can be tinted per palette copy; everything else inherits.
local VARIANT = {
  blue = {
    bg = "#1d1b2b", bg_alt = "#242033", bg_soft = "#2b2640", bg_dark = "#191628",
    fg = "#c8cbe8", fg_alt = "#9aa2c4", fg_dim = "#6f7694",
    blue = "#7fb2f0", cyan = "#93d5e3", green = "#a8d8a0",
    purple = "#b78fd6", pink = "#e3a3c6", orange = "#f2a65a",
    peach = "#f9c18c", red = "#e77a9c", gray = "#8b90ad",
  },
  purple = {
    bg = "#221e33", bg_alt = "#2b2540", bg_soft = "#332c4d", bg_dark = "#1d1829",
    fg = "#cdc5ea", fg_alt = "#a69ac6", fg_dim = "#7a7094",
    blue = "#8fa8e8", cyan = "#98c8de", green = "#a9d2a4",
    purple = "#c99be0", pink = "#e8aede", orange = "#f4a75c",
    peach = "#fbc38e", red = "#e8829e", gray = "#948aa8",
  },
  orange = {
    bg = "#2a2030", bg_alt = "#342839", bg_soft = "#3e3044", bg_dark = "#241b2a",
    fg = "#dccfb5", fg_alt = "#b9ab92", fg_dim = "#8a7d68",
    blue = "#7faee8", cyan = "#8fc9d5", green = "#a9cf9e",
    purple = "#c19ad4", pink = "#ebb8c8", orange = "#f5a855",
    peach = "#fdd090", red = "#e87a96", gray = "#9a8d76",
  },
}

--- Maps a palette (base or variant) + lighten-factor to a hex color.
local function mix(hex, amount)
  -- very small pure-Lua blend toward white; only called at load time.
  local r = tonumber(hex:sub(2, 3), 16)
  local g = tonumber(hex:sub(4, 5), 16)
  local b = tonumber(hex:sub(6, 7), 16)
  local function f(c)
    return string.format("%02x", math.min(255, math.floor(c + (255 - c) * amount)))
  end
  return "#" .. f(r) .. f(g) .. f(b)
end

local function hl(name, fg, bg, attr, other)
  local attrs = {}
  if attr then
    attrs[attr] = true
  end
  if other then
    for k, v in pairs(other) do
      attrs[k] = v
    end
  end
  local tbl = { fg = fg, bg = bg }
  for k, v in pairs(attrs) do
    tbl[k] = v
  end
  vim.api.nvim_set_hl(0, name, tbl)
end

--- Terminal palette (ANSI 0-15). 8 pastel colors the terminal never has trouble
--- distinguishing, all tuned to warm up as the sun comes up on stream.
local function set_terminal(p)
  vim.g.terminal_color_0 = p.bg_dark
  vim.g.terminal_color_1 = p.red
  vim.g.terminal_color_2 = p.green
  vim.g.terminal_color_3 = p.peach
  vim.g.terminal_color_4 = p.blue
  vim.g.terminal_color_5 = p.purple
  vim.g.terminal_color_6 = p.cyan
  vim.g.terminal_color_7 = p.fg
  vim.g.terminal_color_8 = p.fg_dim
  vim.g.terminal_color_9 = p.pink
  vim.g.terminal_color_10 = p.green
  vim.g.terminal_color_11 = p.orange
  vim.g.terminal_color_12 = p.blue
  vim.g.terminal_color_13 = p.purple
  vim.g.terminal_color_14 = p.cyan
  vim.g.terminal_color_15 = p.fg_alt
end

--- The meat. Called once per setup/reload with the resolved config.
function M.apply(cfg)
  local v = VARIANT[cfg.theme] or VARIANT.blue
  local p = vim.tbl_extend("force", M.palette, v)
  M.palette = p
  M.variant = cfg.theme

  vim.o.background = "dark"

  -- Core editor -------------------------------------------------------
  hl("Normal", p.fg, p.bg)
  hl("NormalFloat", p.fg, p.bg_alt)
  hl("FloatBorder", p.purple, p.bg_alt)
  hl("StatusLine", p.bg_alt, p.bg, "bold")
  hl("StatusLineNC", p.fg_dim, p.bg_alt)
  hl("StatusLineTerm", p.bg_alt, p.bg)
  hl("WinBar", p.purple, p.bg_alt)
  hl("WinBarNC", p.gray, p.bg_alt)
  hl("Cursor", p.bg, p.peach) -- visible cursor for the stream
  hl("CursorLine", nil, p.bg_soft)
  hl("CursorLineNr", p.orange, p.bg_soft, "bold")
  hl("LineNr", p.fg_dim, nil)
  hl("EndOfBuffer", p.fg_dim, nil)
  hl("Visual", p.bg_soft, nil)
  hl("Search", p.bg, p.peach)
  hl("IncSearch", p.bg, p.peach, "bold")
  hl("CurSearch", p.bg, p.peach, "bold")
  hl("MatchParen", p.orange, p.bg_soft, "bold")
  hl("Pmenu", p.fg, p.bg_alt)
  hl("PmenuSel", p.bg, p.blue)
  hl("PmenuThumb", nil, p.gray)
  hl("SnippetTabstop", p.orange, p.bg_soft, "underline")
  hl("Selection", p.bg, p.blue)

  -- Split / window chrome
  hl("WinSeparator", p.gray, p.bg)
  hl("VertSplit", p.gray, p.bg)
  hl("TabLine", p.fg_dim, p.bg_alt)
  hl("TabLineFill", nil, p.bg)
  hl("TabLineSel", p.bg, p.purple, "bold")
  hl("Title", p.peach, nil, "bold")
  hl("Bold", nil, nil, "bold")
  hl("Italic", p.pink, nil, "italic")
  hl("Underlined", p.cyan, nil, "underline")

  -- Syntax ------------------------------------------------------------
  hl("Comment", p.fg_dim, nil, "italic")
  hl("String", p.cyan, nil)
  hl("Character", p.peach, nil)
  hl("Number", p.orange, nil)
  hl("Float", p.orange, nil)
  hl("Boolean", p.orange, nil)
  hl("Constant", p.orange, nil)
  hl("Identifier", p.fg, nil)
  hl("Function", p.blue, nil)
  hl("Method", p.blue, nil)
  hl("Statement", p.purple, nil)
  hl("Conditional", p.purple, nil, "bold")
  hl("Repeat", p.purple, nil, "bold")
  hl("Label", p.pink, nil)
  hl("Operator", p.green, nil)
  hl("Keyword", p.purple, nil, "bold")
  hl("Exception", p.red, nil)
  hl("PreProc", p.pink, nil)
  hl("Include", p.pink, nil)
  hl("Define", p.pink, nil)
  hl("Macro", p.orange, nil)
  hl("PreCondit", p.pink, nil)
  hl("Type", p.blue, nil)
  hl("StorageClass", p.purple, nil)
  hl("Structure", p.blue, nil)
  hl("Typedef", p.blue, nil)
  hl("Special", p.peach, nil)
  hl("SpecialChar", p.pink, nil)
  hl("Tag", p.orange, nil)
  hl("Delimiter", p.gray, nil)
  hl("SpecialComment", p.fg_dim, nil, "italic")
  hl("Debug", p.peach, nil)
  hl("Error", p.red, p.bg, "bold")
  hl("Todo", p.bg, p.peach, "bold")

  -- Diff / diagnostics -------------------------------------------------
  hl("DiffAdd", p.green, p.bg_alt)
  hl("DiffChange", p.orange, p.bg_alt)
  hl("DiffDelete", p.red, p.bg_alt, "strikethrough")
  hl("DiffText", p.bg, p.blue, "bold")
  hl("DiffAddText", p.bg, p.green, "bold")
  hl("DiagnosticError", p.red, nil)
  hl("DiagnosticWarn", p.peach, nil)
  hl("DiagnosticInfo", p.blue, nil)
  hl("DiagnosticHint", p.purple, nil)
  hl("DiagnosticOk", p.green, nil)
  hl("DiagnosticUnderlineError", nil, nil, "undercurl", { sp = p.red })
  hl("DiagnosticUnderlineWarn", nil, nil, "undercurl", { sp = p.peach })
  hl("DiagnosticUnderlineInfo", nil, nil, "undercurl", { sp = p.blue })
  hl("DiagnosticUnderlineHint", nil, nil, "undercurl", { sp = p.purple })

  -- Strudel-specific ---------------------------------------------------
  hl("StrudelPulse", p.cyan, nil, "bold") -- combinator literals like 0xdeadbeef
  hl("StrudelCombinator", p.peach, nil, "bold") -- chain combinators: |> <-> =
  hl("StrudelChord", p.blue, nil, "bold")
  hl("StrudelRoot", p.orange, nil, "bold")
  hl("StrudelCommentTag", p.pink, nil, "bold")-- "#flip", "#wid" hints
  hl("StrudelPattern", p.purple, nil, "bold")
  hl("StrudelSymbol", p.green, nil)
  hl("StrudelEval", p.green, nil, "bold")
  hl("StrudelPlay", p.cyan, nil, "bold")

  -- Statusline uses <Plug>Strudel* highlight groups too.
  hl("StrudelModeGenerate", p.bg, p.green)
  hl("StrudelModeDrift", p.bg, p.blue)
  hl("StrudelModeChill", p.bg, p.purple)
  hl("StrudelModeFix", p.bg, p.orange)
  hl("StrudelMeter", p.bg, p.orange, "bold")
  hl("StrudelMeterWarn", p.bg, p.peach, "bold")
  hl("StrudelClock", p.bg, p.cyan, "bold")
  hl("StrudelAgent", p.bg, p.purple, "bold")

  -- Misc treesitter-like groups that other plugins like to reuse
  hl("TSConditional", p.purple)
  hl("TSKeyword", p.purple)
  hl("TSOperator", p.green)
  hl("TSString", p.cyan)
  hl("TSNumber", p.orange)
  hl("TSFunction", p.blue)
  hl("TSMethod", p.blue)
  hl("TSConstructor", p.purple)
  hl("TSConstant", p.orange)
  hl("TSNamespace", p.pink)
  hl("TSStruct", p.blue)
  hl("TSType", p.blue)
  hl("TSField", p.fg)
  hl("TSParameter", p.fg_alt)
  hl("TSProperty", p.fg)
  hl("TSTag", p.peach)
  hl("TSEmphasis", nil, nil, "italic")
  hl("TSStrong", nil, nil, "bold")
  hl("TSUnderline", p.cyan, nil, "underline")
  hl("TSTitle", p.blue, nil, "bold")
  hl("TSLiteral", p.cyan)

  -- Telescope / fzf-lua / Noice share these
  hl("TelescopeBorder", p.purple, p.bg_alt)
  hl("TelescopePromptBorder", p.blue, p.bg_alt)
  hl("TelescopeSelection", p.bg, p.blue, "bold")
  hl("TelescopePromptPrefix", p.peach, p.bg_alt)
  hl("TelescopeNormal", p.fg, p.bg_alt)
  hl("TelescopeMatching", p.orange, nil, "bold")

  -- LSP / treesitter-heavy plugins
  hl("@lsp.type.namespace", p.pink)
  hl("@lsp.type.class", p.blue)
  hl("@lsp.type.struct", p.blue)
  hl("@lsp.type.enum", p.purple)

  set_terminal(p)
end

--- Give the fiddling agent a direct screenshot of what's on screen right now.
function M.cheatsheet()
  return {
    theme = M.variant or "blue",
    palette = M.palette,
    screenshot = {
      "transparent off / termguicolors on",
      "base:   " .. M.palette.bg .. "  periwinkle prose on lavender night",
      "hot:    " .. M.palette.orange .. "  numbers + keywords + the glm meter",
      "cool:   " .. M.palette.purple .. "/" .. M.palette.blue .. "  structure, functions, floats",
      "status:  " .. M.variant .. " · ⚡ meter · set clock · mode chip",
    },
  }
end

return M