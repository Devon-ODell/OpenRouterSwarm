--- strudel.nvim — statusline + winbar.
---
--- A three-stage powerline-alike segment: mode chip, file section, then the
--- ⚡ glm meter + set clock right-aligned. All painted with the theme palette
--- so it reads as one glow system on stream. Zero dependencies — an inline
--- vimscript expression calls back into Lua, so no lualine needed (but lualine
--- users can keep theirs by setting vim.o.statusline themselves).

local M = {}
local theme = require("strudel.theme")
local config = require("strudel.config")

local MODE_CHIPS = {
  generate = { "GEN", "StrudelModeGenerate" },
  drift = { "DRIFT", "StrudelModeDrift" },
  chill = { "CHILL", "StrudelModeChill" },
  fix = { "FIX", "StrudelModeFix" },
}

--- Wrap a chunk of text in %#Strudel..HL# ... %* escaping.
local function seg(hlname, text)
  return "%#" .. hlname .. "#" .. text .. "%*"
end

--- Left chip: the driver's livecoding mode.
local function mode_chip(mode)
  local c = MODE_CHIPS[mode] or MODE_CHIPS.generate
  return seg(c[2], " " .. c[1] .. " ")
end

--- Middle: file name, modified flag, git branch if any.
local function file_section()
  local mods = vim.bo.modified and " ●" or ""
  local name = vim.fn.expand("%:t")
  if name == "" then
    name = "[no name]"
  end
  local branch = ""
  if vim.fn.exists("*FugitiveHead") == 1 then
    local ok, res = pcall(vim.fn.FugitiveHead)
    if ok and res then
      branch = res
    end
  end
  if branch == "" then
    branch = vim.b.gitsigns_head or ""
  end
  local b = (branch ~= "" and " " .. branch .. " " or "")
  return seg("StrudelFile", " " .. name .. mods .. " " .. b)
end

--- ⚡ glm meter: session cost + optional budget % from agent.lua's global.
local function meter_section()
  local g = vim.g.strudel_agent or {}
  if not g.visible then
    return ""
  end
  local cost = g.cost or 0
  local budget = g.budget
  local pct = budget and (budget > 0 and math.floor(cost / budget * 100) or 0) or nil
  local color = "StrudelMeter"
  if pct and pct >= 80 then
    color = "StrudelMeterWarn"
  end
  local txt = string.format("⚡ %.2f¢", cost * 100)
  if pct then
    txt = txt .. string.format(" %d%%", pct)
  end
  return seg(color, " " .. txt .. " ")
end

--- Set clock: HH:MM:SS counting up from session start.
local function clock_section(session)
  local t = session.now or "00:00:00"
  return seg("StrudelClock", " " .. t .. " ") .. (session.enabled and "" or " ·")
end

--- Full statusline, called by the inline vimscript expression.
function M.statusline()
  local cfg = config.resolve()
  local session = vim.g.strudel_session or {}
  return mode_chip(cfg.mode)
    .. " "
    .. file_section()
    .. "%="
    .. meter_section()
    .. " "
    .. clock_section(session)
end

--- Winbar: a "♪" strudel chip, then name/dir. Right side hosted the set clock
--- too, but kept to a single line for the stream.
function M.winbar()
  local ft = vim.bo.filetype or ""
  local chip = (ft == "strudel") and seg("StrudelPulse", " ♪") or ""
  local name = vim.fn.expand("%:t")
  if name == "" then
    name = ""
  end
  local dir = vim.fn.fnamemodify(vim.fn.expand("%:p:h"), ":~")
  if dir == "." or dir == "" then
    dir = ""
  else
    dir = " " .. dir .. "/"
  end
  local clock = vim.g.strudel_session and vim.g.strudel_session.now or ""
  local right = (clock ~= "" and " " .. clock or "")
  return chip .. " " .. name .. dir .. "%=" .. right
end

--- Install ourselves only if nobody claimed the statusline yet.
function M.apply(cfg)
  if cfg.statusline_meter and (vim.o.statusline == "" or vim.o.statusline:find("strudel", 1, true)) then
    vim.o.statusline = "%!v:lua.require('strudel.statusline').statusline()"
  end
  if cfg.winbar then
    vim.o.winbar = "%!v:lua.require('strudel.statusline').winbar()"
  end
end

return M