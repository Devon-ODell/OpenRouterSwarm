--- strudel.nvim
---
--- Neovim configured for Strudel livecoding, driven headlessly by an OpenRouter
--- agent (z-ai/glm-5.3-flash) while the human plays 2h sets or streams 4-6h.
---
--- There are three layers that loads together:
---   * init.lua   — plugin entry point (this file): setup(), autocmds, timer
---   * theme.lua  — the pastel blue/purple/orange colorscheme + statusline
---   * strudel.lua— .strudel filetype, send-to-browser, snippets
---   * agent.lua  — OpenRouter chat driver with a ⚡ glm cost meter

local M = {}
local config = require("strudel.config")

local cfg = nil ---@type table effective config

local theme, strudel, agent --- module handles, loaded lazily

local session = {
  enabled = false, -- set by the timer / :StrudelSession
  start = os.time(),
}

-------------------------------------------- autocmds --------------------

--- Filetype mapping: *.strudel and *.ss sets are Strudel code.
--- (naming forced through explicit filetype so we never own Javascript.)
local function ft_from_path(path)
  if path:match("%.strudel$") or path:match("%.ss$") then
    return "strudel"
  end
  return nil
end

local function setup_autocmds()
  vim.api.nvim_create_autocmd("BufNewFile", {
    pattern = { "*.strudel", "*.ss" },
    callback = function(ev)
      vim.bo[ev.buf].filetype = "strudel"
    end,
  })
  vim.api.nvim_create_autocmd("BufRead", {
    pattern = { "*.strudel", "*.ss" },
    callback = function(ev)
      vim.bo[ev.buf].filetype = "strudel"
    end,
  })
  -- Keep the literal filetype outside of ftdetect, but map a couple of
  -- neighbour languages that people paste from.
  vim.api.nvim_create_autocmd("FileType", {
    pattern = { "javascript", "typescript", "js", "ts" },
    callback = function(ev)
      if ft_from_path(vim.api.nvim_buf_get_name(ev.buf)) == "strudel" then
        vim.bo[ev.buf].filetype = "strudel"
      end
    end,
  })
end

---------------------------------------------- base options ---------------

local function setup_options()
  vim.opt.termguicolors = true
  vim.opt.background = "dark"
  vim.opt.colorcolumn = "100" ---@diagnostic disable-line: unused-local
  vim.opt.signcolumn = "yes"
  vim.opt.number = true
  vim.opt.relativenumber = true
  vim.opt.cursorline = true
  vim.opt.cursorlineopt = "number" -- only underline the number gutter
  vim.opt.scrolloff = 8
  vim.opt.wrap = false
  vim.opt.swapfile = false
  vim.opt.splitright = true
  vim.opt.splitbelow = true
  vim.opt.mouse = "a"
  vim.opt.list = true
  vim.opt.listchars = { tab = "»·", trail = "·", extends = "❯", precedes = "❮" }
  vim.opt.foldlevel = 99 -- buffers come unfolded for the agent
  vim.opt.sessionoptions:append("globals") -- :mksession restores strudel_g_*  (implicit)
  vim.opt.sessionoptions = "blank,buffers,curdir,folds,globals,help,tabpages,terminal,winpos,winsize"
  vim.opt.statusline = "" -- we own it, see theme.statusline()
end

---------------------------------------------- keymaps -------------------

local K = { noremap = true, silent = true }

local function map(mode, lhs, rhs, opts)
  if lhs and lhs ~= "" then
    vim.keymap.set(mode, lhs, rhs, vim.tbl_extend("force", K, opts or {}))
  end
end

local function setup_keymaps()
  local k = cfg.keys
  map("n", k.send, function()
    strudel.send_selection()
  end, { desc = "Strudel: send selection / line / buffer to live" })
  map("v", k.send, function()
    strudel.send_selection()
  end, { desc = "Strudel: send selection to live" })
  map("n", k.send_all, function()
    strudel.send_buffer()
  end, { desc = "Strudel: send whole set live" })
  map("n", k.snippets, function()
    strudel.insert_snippet(vim.ui.select and "menu" or "cpick")
  end, { desc = "Strudel: insert snippet" })
  map("n", k.agent_ask, function()
    agent.ask_cursor()
  end, { desc = "agent: ask glm about the cursor line" })
  map("n", k.agent_chat, function()
    agent.chat()
  end, { desc = "agent: open chat with glm-5.3-flash" })
  map("n", k.agent_meter, function()
    require("strudel.agent").toggle_meter()
  end, { desc = "agent: toggle ⚡ glm meter" })
  map("n", k.agent_stop, function()
    require("strudel.agent").stop_all()
  end, { desc = "agent: stop in-flight completions" })
  map("n", "<leader>sr", function()
    vim.cmd("StrudelReload")
  end, { desc = "Strudel: reload configuration" })
  map("n", "<leader>st", function()
    vim.cmd("StrudelSessionToggle")
  end, { desc = "Strudel: toggle set clock" })
  map("n", "<leader>sm", function()
    vim.cmd("StrudelMode")
  end, { desc = "Strudel: switch driver mode" })
end

---------------------------------------------- session clock --------------

local function tick()
  if not session.enabled then
    return
  end
  local el = os.difftime(os.time(), session.start)
  session.elapsed = el
  session.now = os.date("!%H:%M:%S", el) -- runs past 24h, fine
  if vim.o.statusline ~= "" then -- someone else owns the statusline
    return
  end
  local ok = package.loaded["strudel.theme"]
  if ok and ok.statusline then
    vim.cmd("redrawstatus")
  end
end

---------------------------------------------- commands -------------------

local function setup_commands()
  vim.api.nvim_create_user_command("StrudelTheme", function()
    -- toggle between the three palettes, live, on the running instance
    local order = { "blue", "purple", "orange" }
    local cur = theme.variant or "blue"
    local idx = 1
    for i, name in ipairs(order) do
      if name == cur then
        idx = i
      end
    end
    local next = order[idx % 3 + 1]
    cfg.theme = next
    theme.apply(cfg)
    vim.notify("strudel theme → " .. next)
  end, { desc = "Cycle the pastel theme live" })

  vim.api.nvim_create_user_command("StrudelReload", function()
    package.loaded["strudel.config"] = nil
    package.loaded["strudel"] = nil
    package.loaded["strudel.strudel"] = nil
    package.loaded["strudel.agent"] = nil
    package.loaded["strudel.theme"] = nil
    package.loaded["strudel.statusline"] = nil
    vim.g.strudel_loaded = nil
    require("strudel").setup()
    vim.notify("strudel.nvim reloaded")
  end, { desc = "Reload strudel.nvim" })

  vim.api.nvim_create_user_command("StrudelSessionToggle", function()
    session.enabled = not session.enabled
    session.start = os.time()
    if session.enabled then
      vim.notify("rodando · set clock on")
    else
      vim.notify("set clock off")
    end
    vim.cmd("redrawstatus")
  end, { desc = "Toggle the set clock" })

  vim.api.nvim_create_user_command("StrudelMode", function(input)
    local modes = { "generate", "drift", "chill", "fix" }
    local cur = cfg.mode
    if input.args ~= "" then
      if vim.tbl_contains(modes, input.args) then
        agent.set_mode(input.args)
      else
        vim.notify("unknown mode: " .. input.args .. " (generate|drift|chill|fix)", vim.log.levels.WARN)
      end
      return
    end
    vim.ui.select(modes, { prompt = "agent mode (currently " .. cur .. ")" }, function(m)
      if m then
        agent.set_mode(m)
      end
    end)
  end, { nargs = "?", desc = "Set the agent's livecoding mode" })

  vim.api.nvim_create_user_command("StrudelSend", function(input)
    if input.args == "" then
      vim.notify("usage: StrudelSend <code>", vim.log.levels.WARN)
      return
    end
    strudel.send_code(input.args)
  end, { nargs = "*", desc = "Send raw code to Strudel live" })

  vim.api.nvim_create_user_command("StrudelEvalEval", function()
    vim.notify("try :StrudelSend or the <leader>ss maps", vim.log.levels.INFO)
  end, { desc = "hint command" })
end

---------------------------------------------- timer ----------------------

local function setup_timer()
  if not cfg.playtime_timer then
    return
  end
  -- Set clock starts when nvim is ready, and the 1s ticker only paints when
  -- the statusline is ours (vim.o.statusline == "").
  vim.api.nvim_create_autocmd("VimEnter", {
    callback = function()
      session.enabled = true
      session.start = os.time()
      session.now = "00:00:00"
      vim.api.nvim_create_timer(1000, function()
        tick()
      end, {})
      vim.api.nvim_create_autocmd("VimLeave", {
        callback = function()
          session.enabled = false
        end,
      })
    end,
  })
  vim.g.strudel_session = session -- exposed for any window-local statusline
end

---------------------------------------------- setup ----------------------

function M.setup(opts)
  if vim.g.strudel_loaded then
    return true -- already loaded (idempotent, :StrudelReload safe)
  end
  vim.g.strudel_loaded = true

  cfg = require("strudel.config").resolve(opts)

  setup_autocmds()
  setup_options()

  -- lazy-load the three modules so :StrudelReload can re-enter cleanly
  theme = require("strudel.theme")
  strudel = require("strudel.strudel")
  agent = require("strudel.agent")
  require("strudel.statusline") -- self-installs via theme.apply paths

  theme.apply(cfg)
  strudel.setup(cfg)
  agent.setup(cfg)
  require("strudel.statusline").apply(cfg)
  setup_keymaps()
  setup_commands()
  setup_timer()

  -- I/O macros + model-awareness that the headless driver expects back in it
  vim.g.strudel_model = cfg.model
  vim.g.strudel_mode = cfg.mode
  vim.cmd("runtime! ftdetect/strudel.vim")

  vim.notify("strudel.nvim loaded · glm " .. cfg.model .. " mode:" .. cfg.mode, vim.log.levels.INFO)
  return true
end

--- Started at a deterministic point so tests/agents can assert the layout.
M._test_snapshot = function()
  return {
    theme = cfg and cfg.theme or "blue",
    model = cfg and cfg.model or nil,
    mode = cfg and cfg.mode or nil,
    ft = vim.bo.filetype,
    session = session.enabled and session.now or nil,
  }
end

return M