-- smoke test for strudel.nvim — run headless, asserts via print
local assert = assert
require("strudel").setup {}

-- 1. setup state
assert(vim.g.strudel_loaded, "plugin loaded flag")
assert(vim.g.strudel_model == "z-ai/glm-5.3-flash", "model default")

-- 2. filetype detection happens via runtime on BufRead — open a strudel file
vim.cmd("e /tmp/test.strudel")
vim.cmd("silent! doautocmd BufRead")
assert(vim.bo.filetype == "strudel", "filetype is strudel, got " .. tostring(vim.bo.filetype))
print("FT:", vim.bo.filetype)

-- 3. theme applied + has our groups
require("strudel.theme").apply(require("strudel.config").resolve())
assert(vim.fn.hlexists("StrudelPulse") == 1, "StrudelPulse group exists")
assert(vim.fn.hlexists("StrudelMeter") == 1, "StrudelMeter group exists")
print("theme variant:", require("strudel.theme").variant)

-- 4. statusline + winbar installed
assert(vim.o.statusline ~= "", "statusline installed")
assert(vim.o.winbar ~= "", "winbar installed")
print("statusline len:", #vim.o.statusline)

-- 5. agent meter global
local a = vim.g.strudel_agent
assert(a and a.visible, "agent meter visible")
assert(a.model == "z-ai/glm-5.3-flash", "meter carries model")
print("meter cost:", a.cost, "visible:", a.visible)

-- 6. keymaps
assert(vim.fn.maparg("<leader>ss", "n") ~= "", "send keymap")
assert(vim.fn.maparg("<leader>ac", "n") ~= "", "chat keymap")
print("keymaps ok")

-- 7. mode switching
require("strudel.agent").set_mode("drift")
assert(vim.g.strudel_mode == "drift", "mode persisted to g")
print("mode:", vim.g.strudel_mode)

-- 8. snippets defined
local sd = require("strudel.strudel").snippets
assert(next(sd) ~= nil, "snippet library non-empty")
print("snippet count:", #vim.tbl_keys(sd))

-- 9. session clock global present (timer fires post-VimEnter)
assert(vim.g.strudel_session ~= nil, "session global set")

-- 10. theme cycle command
vim.api.nvim_exec2("StrudelTheme", {})
print("theme after cycle:", require("strudel.theme").variant)
vim.api.nvim_exec2("StrudelTheme", {})
print("theme after cycle2:", require("strudel.theme").variant)

print("=== ALL SMOKE TESTS PASSED ===")