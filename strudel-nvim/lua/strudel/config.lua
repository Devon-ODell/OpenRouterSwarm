--- strudel.nvim — configuration defaults + user override merge.
--- Every option can be set ahead of time with vim.g.strudel_<key> or passed to
--- require('strudel').setup{ ... }.

local M = {}

--- Defaults. Keys map 1:1 to vim.g.strudel_<key> overrides.
M.defaults = {
  theme = "blue", -- "blue" | "purple" | "orange" (aliases: "pastel" -> blue)

  --- Strudel language -------------------------------------------------
  -- Browser/terminal window title that Strudel "live" exposes so we can send
  -- code with OSC 52 to it. Leave nil to use whatever wezterm thinks is focused.
  strudel_window = nil,
  -- When sending snippets/selection prepend a short string so the set keeps
  -- playing while you type ("" means no extra sound).
  strudel_pad = " ",
  -- Default filetype extension for saved sets.
  strudel_ext = "strudel",

  --- Agent driver (OpenRouter, z-ai/glm-5.3-flash) ----------------------
  model = "z-ai/glm-5.3-flash", -- livecoding workhorse on OpenRouter
  temperature = 0.65, -- < .7 keeps 2h sets coherent, > .9 gets spicy
  max_tokens = 2200, -- per response
  timeout = 90, -- seconds, streaming sets can be slow but this is chat
  api_key = nil, -- falls back to $OPENROUTER_API_KEY
  base_url = "https://openrouter.ai/api/v1",
  -- Optional hard budget in USD for the session; agent stops asking when hit.
  budget_usd = nil,
  -- Show the ⚡ glm meter in the statusline (vim.o.statusline must be ours).
  statusline_meter = true,
  -- How chat history is trimmed to fit the context window.
  max_history_chars = 200000, -- ~200k chars, far under glm's 1M window

  --- Mode the driver is living in (shown in the statusline) -------------
  -- "generate" | "drift" | "chill" | "fix"
  mode = "generate",

  --- Aesthetics ---------------------------------------------------------
  transparent = false, -- respect the terminal's true background
  bold_keywords = true,
  italic_comments = true,
  undercurl = true, -- use undercurl instead of underline for diagnostics
  window_blend = 18, -- 0..100, float windows (Pmenu/NvimTree style) alpha
  winbar = true, -- show file + session clock in the winbar
  playtime_timer = true, -- HH:MM:SS set clock in the statusline

  --- Keymaps (empty string disables). -----------------------------------
  keys = {
    send = "<leader>ss", -- send selection / current line / whole buffer
    send_all = "<leader>sa",
    snippets = "<leader>sk",
    agent_ask = "<leader>aq",
    agent_chat = "<leader>ac",
    agent_meter = "<leader>at", -- toggle statusline meter
    agent_stop = "<leader>ax",
  },
}

--- Effective config: vim.g.strudel_* wins over setup{} wins over defaults.
function M.resolve(opts)
  opts = opts or {}
  local cfg = vim.deepcopy(M.defaults)
  for k, v in pairs(cfg) do
    if opts[k] ~= nil then
      cfg[k] = opts[k]
    end
    local gv = vim.g["strudel_" .. k]
    if gv ~= nil then
      cfg[k] = gv
    end
  end
  -- normalise a few strings
  if cfg.theme == "pastel" or cfg.theme == "default" then
    cfg.theme = "blue"
  end
  if not (cfg.theme == "blue" or cfg.theme == "purple" or cfg.theme == "orange") then
    cfg.theme = "blue"
  end
  cfg.mode = (cfg.mode == "drift" or cfg.mode == "chill" or cfg.mode == "fix") and cfg.mode or "generate"
  cfg.api_key = cfg.api_key or os.getenv("OPENROUTER_API_KEY")
  return cfg
end

return M