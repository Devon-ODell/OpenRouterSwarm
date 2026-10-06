-- wezterm palette parity test — the terminal and the editor must speak the
-- same 16 paint chips when the theme is "blue".
-- Run: nvim --headless -u NONE --cmd "set rtp+=<repo>" -c "luafile tests/wezterm_parity_test.lua" -c "qa"

local function slurp(path)
  local f, err = io.open(path, "r")
  if not f then
    error("cannot open " .. path .. ": " .. tostring(err))
  end
  local s = f:read("*a")
  f:close()
  return s
end

-- Within a "{ ... }" block, collect every "#rrggbb" literal in order.
local function hexes_in(block)
  local out = {}
  for hex in block:gmatch('#[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]') do
    out[#out + 1] = hex:lower()
  end
  return out
end

-- ANSI 0-7 from `ansi = {...}`, 8-15 from `brights = {...}`.
local function wezterm_ansi()
  local src = slurp("wezterm/wezterm.lua")
  local ansi = src:match('ansi%s*=%s*(%b{})')
  local brights = src:match('brights%s*=%s*(%b{})')
  assert(ansi, "wezterm config must declare an `ansi` block")
  assert(brights, "wezterm config must declare a `brights` block")
  local out = hexes_in(ansi)
  for _, v in ipairs(hexes_in(brights)) do
    out[#out + 1] = v
  end
  return out
end

-- ANSI as set by lua/strudel/theme.lua (blue variant via vim.g.terminal_color_*)
local function plugin_ansi()
  require("strudel.theme").apply(require("strudel.config").resolve())
  local out = {}
  for i = 0, 15 do
    out[i + 1] = (vim.g["terminal_color_" .. i] or ""):lower()
  end
  return out
end

local w = wezterm_ansi()
local p = plugin_ansi()
assert(#w == 16, "wezterm palette must declare 16 entries, got " .. #w)
assert(#p == 16, "plugin terminal_color_* must set 16 entries, got " .. #p)
for i = 0, 15 do
  assert(w[i + 1] == p[i + 1],
    string.format("ansi %d mismatch: wezterm %s vs plugin %s", i, w[i + 1], p[i + 1]))
end

print("PALETTE PARITY OK (16/16 match)")