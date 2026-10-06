--- strudel.nvim — Strudel language mode + live set telemetry.
---
--- Strudel is a tiny JavaScript dialect for livecoding music
--- (https://strudel.cc). This module gives it a first-class filetype:
---
---   * literal "strudel" filetype (never hijacks .js)
---   * syntax in the pastel palette (Strudel* highlight groups)
---   * "send to live": clipboards + OSC 52 the selection/line/buffer into the
---     WezTerm pane that has Strudel live focused, with a configurable "pad"
---     string so the set keeps breathing while you edit
---   * an optional `wezterm cli send-text` passthrough for panes that don't
---     focus the browser tab
---   * a small snippet library in plain Lua (no external deps)

local M = {}

local cfg = nil

--- Encode bytes to base64 without external binaries (pure Lua, tiny strings
--- only — we send at most a whole buffer, a few KB).
local B64C = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
local function b64encode(s)
  local out = {}
  for i = 1, #s, 3 do
    local a = s:byte(i) or 0
    local b = s:byte(i + 1) or 0
    local c = s:byte(i + 2) or 0
    local n = a * 65536 + b * 256 + c
    out[#out + 1] = B64C:byte(math.floor(n / 262144) + 1)
    out[#out + 1] = B64C:byte(math.floor(n / 4096) % 64 + 1)
    out[#out + 1] = B64C:byte(math.floor(n / 64) % 64 + 1)
    out[#out + 1] = B64C:byte(n % 64 + 1)
  end
  local padding = (3 - #s % 3) % 3
  for i = 1, padding do
    out[#out] = ("="):byte()
  end
  return string.char(table.unpack(out))
end

--- Write OSC 52 <clipboard|primary|buffer> payload; WezTerm accepts this and
--- will route the paste to the recently focused window (the live tab).
local function osc52(kind, payload_b64)
  io.write(string.format("\027]52;%s;%s\007", kind, payload_b64))
  io.flush()
end

--- Read the "system clipboard" → send via OSC 52 to the strudel window.
--- `kind`: "c" = clipboard, "p" = primary, "b" = buffer.
function M.send_code(code)
  if code == nil or code == "" then
    return
  end
  -- 1. system clipboard so any paste key in the browser works too
  vim.fn.setreg("+", code)
  vim.fn.setreg('"', code)
  -- 2. OSC 52 to the WezTerm pane with live focus
  osc52("c", b64encode(code))
  -- 3. optional `wezterm cli send-text` direct stomp into the live pane
  if cfg.wezterm_cli then
    local sock = vim.env.WEZTERM_UNIX_SOCKET
    if sock and sock ~= "" then
      local tmp = vim.fn.tempname()
      vim.fn.writefile({ code }, tmp)
      local cmd = string.format(
        "WEZTERM_UNIX_SOCKET=%q wezterm cli send-text --pane-id %s -- < %q 2>/dev/null",
        sock, "$WEZTERM_PANE", tmp)
      vim.fn.system(cmd)
      os.remove(tmp)
    end
  end
  -- 4. pad keeps the loop busy while the human (or the agent) types next
  if cfg.strudel_pad and cfg.strudel_pad ~= "" then
    vim.fn.setreg("+", cfg.strudel_pad)
  end
  vim.notify(string.format("→ live · %d chars", #code), vim.log.levels.INFO)
end

--- Send current visual selection, else current line.
function M.send_selection()
  local text
  if vim.fn.mode():find("^[vV\22]") then
    local s = vim.fn.getpos("v")
    local e = vim.fn.getpos(".")
    local lines = vim.api.nvim_buf_get_lines(0, s[2] - 1, e[2], false)
    if #lines == 1 then
      text = lines[1]:sub(s[3], e[3])
    else
      lines[1] = lines[1]:sub(s[3])
      lines[#lines] = lines[#lines]:sub(1, e[3])
      text = table.concat(lines, "\n")
    end
    vim.cmd("normal! \\<Esc>")
  else
    text = vim.fn.getline(".")
  end
  M.send_code(text)
end

--- Send the whole buffer.
function M.send_buffer()
  M.send_code(table.concat(vim.api.nvim_buf_get_lines(0, 0, -1, false), "\n"))
end

--- Snippet library, plain data.
M.snippets = {
  hero = [[
s(1, [0, 3, 7, 10]).freq(60)
  .clip(1).gain(0.9)
  .add(0.125)
  .lpf(4000)]],
  arp = [[
s(1, [0, 4, 7, 12]).arp(0.0625).clip(1)
  .lpf(8000).delay(0.25, 0.3, 0.5)]],
  beat_algo = [[
s(8*4, [1, 0, 1, 0, 1, 0, 1, 1]).arp(0.125)
  .gain(0.8).hp(200).delay(0.375, 0.4, 0.4)]],
  ["drop (react to clock)"] = [[
tidalCycles("v v v v").scale("C4 major")
  .clip(1).gain(0.9).superimpose("<0 2 4 7>")]],
  ["swing (offbeat)"] = [[
s(12*4, [0, 2, 3, 5, 7, 9, 10, 12]).arp(0.166).clip(1)
  .lpf(7000).gain(0.8)]],
  ["lfo wobble"] = [[
s(16*4, [0, 0, 3, 7]).arp(0.125)
  .delay(0.375, 0.3, 0.5)
  .lfo('tri', 1).range(0.2, 1).map('mult')
  .clip(1).gain(0.8)]],
  palindrome = [[
s(8*2, [0, 3, 5, 7, 5, 3]).arp(0.25).clip(1).gain(0.9)]],
}

--- Interactive snippet picker (vim.ui.select — works headless-ish and with
--- telescope if the user remaps pickers).
function M.insert_snippet()
  local names = vim.tbl_keys(M.snippets)
  vim.ui.select(names, { prompt = "strudel snippet" }, function(name)
    if name then
      vim.api.nvim_put(vim.split(M.snippets[name], "\n", { plain = true }), "l", true, true)
    end
  end)
end

--- Register filetype-local options.
function M.setup(opts)
  cfg = opts
  vim.api.nvim_create_autocmd("FileType", {
    pattern = "strudel",
    callback = function(ev)
      vim.bo[ev.buf].expandtab = true
      vim.bo[ev.buf].shiftwidth = 2
      vim.bo[ev.buf].tabstop = 2
      vim.bo[ev.buf].commentstring = "// %s"
    end,
  })
  -- roaring roar for the buffer name in the winbar
end

return M