--- strudel.nvim — the OpenRouter agent driver.
---
--- Headless-ish driver for the livecoding sessions: a chat with
--- z-ai/glm-5.3-flash on OpenRouter, wrapped for musicians rather than devs.
---
---   * :StrudelAsk <prompt>      one shot, answer lands in the strudel buffer
---   * :StrudelChat             persistent chat in a float, streaming in
---   * :StrudelCheck            verify key + model reachability + cost pricing
---   * <leader>af / <leader>at   ask about cursor / toggle the ⚡ meter
---
--- The meter: every response carries "usage.prompt_tokens/completion_tokens"
--- and we know glm-5.3-flash's list prices, so the statusline can show a live
--- session spend ("⚡ 0.42¢ · 12%") which is very fun to watch on a 6h stream.

local M = {}

local cfg = nil
local U = {} -- usage tracker
local active_jobs = {} -- jobstart ids for in-flight curl streams

local function log(...)
  local ok = pcall(vim.notify, table.concat({ ... }, " "))
  if not ok then
    print(...)
  end
end

---------------------------------------------- meter state ---------------

--- Global the statusline reads: vim.g.strudel_agent.
local function pub()
  return {
    visible = U.visible,
    cost = U.cost,
    budget = cfg and cfg.budget_usd,
    mode = cfg and cfg.mode,
    model = cfg and cfg.model,
  }
end

local function emit()
  vim.g.strudel_agent = pub()
  if vim.o.statusline and vim.o.statusline:find("strudel", 1, true) then
    vim.cmd("redrawstatus")
  end
end

--- Account one OpenRouter usage row, converting tokens to USD cents.
local function acc(usage)
  if not usage then
    return
  end
  local p = tonumber(usage.prompt_tokens) or 0
  local c = tonumber(usage.completion_tokens) or 0
  -- z-ai/glm-5.3-flash list prices (per token, in dollars):
  --   0.00000015 prompt · 0.0000005 completion · cached reads 0.00000003
  U.prompt_tokens = (U.prompt_tokens or 0) + p
  U.completion_tokens = (U.completion_tokens or 0) + c
  U.cost = (U.cost or 0) + p * 0.00000015 + c * 0.0000005 + (tonumber(usage.total_cost) or 0)
  emit()
end

--- Get the performance log path from config or default.
local function get_log_path()
  if cfg.log_path and cfg.log_path ~= "" then
    return cfg.log_path
  end
  return vim.fn.stdpath('state') .. '/strudel_performance.jsonl'
end

--- Append a JSONL performance event to the log file.
--- @param event table Fields: schema_version, timestamp, latency_ms, model, mode, outcome, prompt_tokens, completion_tokens, cost_increment, cost_session, cue_id, run_id, response_chars
local function log_performance(event)
  local log_path = get_log_path()
  local ok, dir = pcall(vim.fn.fnamemodify, log_path, ':h')
  if not ok then
    dir = nil
  end
  if dir then
    pcall(vim.loop.fs_mkdir, dir, 511, function() end)
  end
  local file, err = io.open(log_path, "a")
  if not file then
    vim.notify("strudel: could not open performance log at " .. log_path .. ": " .. tostring(err), vim.log.levels.WARN)
    return
  end
  local json_str = vim.json.encode(event)
  if json_str then
    file:write(json_str .. "\n")
  else
    vim.notify("strudel: failed to encode performance event", vim.log.levels.WARN)
  end
  file:close()
end

--- Compute the cost in dollars from a usage object.
local function usage_cost(usage)
  if not usage then return 0 end
  local p = tonumber(usage.prompt_tokens) or 0
  local c = tonumber(usage.completion_tokens) or 0
  return p * 0.00000015 + c * 0.0000005 + (tonumber(usage.total_cost) or 0)
end

--- Build a performance event table.
local function build_performance_event(latency_ms, model, mode, outcome, prompt_tokens, completion_tokens, cost_increment, cost_session, cue_id, run_id, response_chars)
  local end_time = vim.loop.gettimeofday()
  return {
    schema_version = "1.0.0",
    timestamp = end_time.seconds * 1000 + end_time.microseconds / 1000,
    latency_ms = latency_ms,
    model = model,
    mode = mode,
    outcome = outcome,
    prompt_tokens = prompt_tokens,
    completion_tokens = completion_tokens,
    cost_increment = cost_increment,
    cost_session = cost_session,
    cue_id = cue_id,
    run_id = run_id,
    response_chars = response_chars,
  }
end

function M.toggle_meter()
  U.visible = not U.visible
  if U.visible and U.started == nil then
    U.started = os.time()
  end
  emit()
  vim.notify("meter " .. (U.visible and "on" or "off"), vim.log.levels.INFO)
end

function M.session_stats()
  return {
    cost = U.cost or 0,
    prompt_tokens = U.prompt_tokens or 0,
    completion_tokens = U.completion_tokens or 0,
    started = U.started,
  }
end

---------------------------------------------- http helpers -------------

local function headers(extra)
  local h = {
    ["Authorization"] = "Bearer " .. (cfg.api_key or ""),
    ["Content-Type"] = "application/json",
    ["X-Title"] = "strudel.nvim (glm livecoding set)",
  }
  -- optional budget ceiling communicated upstream
  if cfg.budget_usd then
    h["X-Budget"] = tostring(cfg.budget_usd)
  end
  if extra then
    for k, v in pairs(extra) do
      h[k] = v
    end
  end
  return h
end

local function jget(path)
  local cmd = {
    "curl", "-sS", "-m", tostring(cfg.timeout),
    "--fail-with-body",
  }
  for k, v in pairs(headers({ Accept = "application/json" })) do
    cmd[#cmd + 1] = "-H"
    cmd[#cmd + 1] = string.format("%s: %s", k, v)
  end
  cmd[#cmd + 1] = cfg.base_url .. path
  local out = vim.fn.system(cmd)
  local code = vim.v.shell_error
  local ok, data = pcall(vim.json.decode, out)
  if not ok then
    return nil, code, out
  end
  return data, code, out
end

--- Non-streaming POST; returns decoded json or nil,err.
local function jpost(path, body)
  local tmp = vim.fn.tempname()
  local f = assert(io.open(tmp, "w"))
  f:write(vim.json.encode(body))
  f:close()
  local cmd = { "curl", "-sS", "-m", tostring(cfg.timeout), "--fail-with-body" }
  for k, v in pairs(headers()) do
    cmd[#cmd + 1] = "-H"
    cmd[#cmd + 1] = string.format("%s: %s", k, v)
  end
  cmd[#cmd + 1] = "-d"
  cmd[#cmd + 1] = "@" .. tmp
  cmd[#cmd + 1] = cfg.base_url .. path
  local out = vim.fn.system(cmd)
  local code = vim.v.shell_error
  os.remove(tmp)
  local ok, data = pcall(vim.json.decode, out)
  if not ok then
    return nil, code, out
  end
  return data, code, out
end

---------------------------------------------- prompt assembly -----------

--- Living system prompt for a set. Tuned for 2h sets / 4-6h streams, so the
--- model knows the session shape and the mode we're in.
local function system_prompt()
  local hours = cfg.mode == "fix" and "surgery" or "2h set / 4-6h stream"
  local moods = {
    generate = "keep the crowd moving: new phrases, smart dissonance, then resolve. DRIVE.",
    drift = "looser — textural pads, sparse percussion, let the loop breathe.",
    chill = "minimal, warm, slow. The DJ dropped it low. Keep it simple and pretty.",
    fix = "surgical: find what is broken and repair it cleanly, one small change at a time.",
  }
  local mood = moods[cfg.mode]
  return table.concat({
    "You are driving a Strudel livecoding set inside Neovim. Strudel is a tiny",
    "JavaScript dialect for music (strudel.cc). You write short, complete",
    "expressions — a `.strudel` buffer — that a human hits <leader>ss to send",
    "into the live tab. The human is playing " .. hours .. ".",
    "",
    "Session: " .. mood,
    "",
    "Guidelines:",
    "- output ONLY Strudel code, no backticks, no commentary (comments # are fine)",
    "- one or two-line phrases; leverage s(), melody, chords, delay, lfo, gain",
    "- keep the current groove; evolve it, don't restart it from zero",
    "- when asked to fix, explain in one short line what you changed",
    "- you are z-ai/glm-5.3-flash via OpenRouter, context window 1M tokens",
  }, "\n")
end

--- Compact discussion history into the model prompt.
local function messages(history)
  local msgs = { { role = "system", content = system_prompt() } }
  local total = 0
  for _, m in ipairs(history or {}) do
    total = total + #m.content
    if total > (cfg.max_history_chars or 200000) then
      break
    end
    table.insert(msgs, m)
  end
  return msgs
end

local function append_message(history, message)
  local copy = {}
  for _, item in ipairs(history or {}) do
    copy[#copy + 1] = item
  end
  copy[#copy + 1] = message
  return copy
end

local function record_turn(user_text, assistant_text)
  U.history = append_message(U.history, { role = "user", content = user_text })
  U.history = append_message(U.history, { role = "assistant", content = assistant_text })
end

---------------------------------------------- core call ----------------

--- The one-shot completion. Returns text, or nil, error.
function M.complete(prompt_extra)
  local prompt = tostring(prompt_extra)
  local body = {
    model = cfg.model,
    messages = messages({ { role = "user", content = prompt } }),
    temperature = cfg.temperature,
    max_tokens = cfg.max_tokens,
    stream = false,
  }
  local data, code, raw = jpost("/chat/completions", body)
  if not data then
    if code == 402 then
      return nil, "402 — budget exhausted (openrouter) or key limits. check :StrudelMeter"
    elseif code == 401 then
      return nil, "401 — OPENROUTER_API_KEY missing or invalid"
    end
    return nil, (code and "http " .. code) .. " " .. (raw or ""):sub(1, 300)
  end
  acc(data.usage)
  local choice = data.choices and data.choices[1]
  local text = choice and choice.message and (choice.message.content or choice.message.reasoning)
  if not text then
    return nil, "empty completion: " .. (raw or ""):sub(1, 300)
  end
  U.history = U.history or {}
  table.insert(U.history, { role = "user", content = prompt })
  table.insert(U.history, { role = "assistant", content = text })
  return text
end

--- Nonblocking streaming completion. Returns via callback(err, result).
--- result: { text, model, usage?, run_id?, cue_id? }
function M.complete_async(prompt, context, callback)
  if type(callback) ~= "function" then
    error("callback must be a function", 2)
  end
  local done = false
  local tmp = vim.fn.tempname()
  local start_time = vim.loop.gettimeofday()
  local f = assert(io.open(tmp, "w"))
  local body = {
    model = cfg.model,
    messages = messages(append_message((context and context.history) or U.history, { role = "user", content = prompt })),
    temperature = cfg.temperature,
    max_tokens = cfg.max_tokens,
    stream = true,
    stream_options = { include_usage = true },
  }
  f:write(vim.json.encode(body))
  f:close()
  local cmd = { "curl", "-sS", "-N", "-m", tostring(cfg.timeout) }
  for k, v in pairs(headers()) do
    cmd[#cmd + 1] = "-H"
    cmd[#cmd + 1] = string.format("%s: %s", k, v)
  end
  cmd[#cmd + 1] = "-d"
  cmd[#cmd + 1] = "@" .. tmp
  cmd[#cmd + 1] = cfg.base_url .. "/chat/completions"
  local response_parts = {}
  local last_usage = nil
  local jid
  jid = vim.fn.jobstart(cmd, {
    stdout_buffered = false,
    on_stdout = function(_, data)
      for _, chunk in ipairs(data) do
        for line in chunk:gmatch("[^\r\n]+") do
          if line:find("^data: ") then
            local payload = line:sub(7)
            if payload ~= "[DONE]" then
              local ok, obj = pcall(vim.json.decode, payload)
              if ok and obj.choices and obj.choices[1] and obj.choices[1].delta then
                local d = obj.choices[1].delta
                local piece = d.content and d.content ~= "" and d.content or d.reasoning
                if piece then
                  table.insert(response_parts, piece)
                end
                if obj.usage then
                  last_usage = obj.usage
                end
              end
            end
          end
        end
      end
    end,
    on_stderr = function(_, data)
      for _, line in ipairs(data or {}) do
        if line and line ~= "" then
          log("glm stderr:", line)
        end
      end
    end,
    on_exit = function(job_id, exit_code)
      active_jobs[job_id] = nil
      os.remove(tmp)
      local response = table.concat(response_parts)
      local end_time = vim.loop.gettimeofday()
      local latency_ms = (end_time.seconds - start_time.seconds) * 1000 + (end_time.microseconds - start_time.microseconds) / 1000
      local function log_and_fail(err, outcome_val)
        if done then return end
        done = true
        -- Log failure event
        local function safe_log()
          pcall(function()
            local usage = last_usage or {}
            local prompt_tokens = tonumber(usage.prompt_tokens) or 0
            local completion_tokens = tonumber(usage.completion_tokens) or 0
            local cost_increment = usage_cost(usage)
            local cost_session = U.cost or 0
            local event = build_performance_event(
              latency_ms,
              cfg.model,
              cfg.mode,
              outcome_val,
              prompt_tokens,
              completion_tokens,
              cost_increment,
              cost_session,
              context and context.cue_id,
              context and context.run_id,
              #response
            )
            log_performance(event)
          end)
        end
        safe_log()
        callback(err, nil)
      end
      local function log_and_succeed(text_val, usage_val)
        if done then return end
        done = true
        -- record turn and account usage
        M.record_turn(prompt, text_val)
        if usage_val then
          acc(usage_val)
        end
        -- Log success event
        local function safe_log()
          pcall(function()
            local usage = usage_val or {}
            local prompt_tokens = tonumber(usage.prompt_tokens) or 0
            local completion_tokens = tonumber(usage.completion_tokens) or 0
            local cost_increment = usage_cost(usage)
            local cost_session = U.cost or 0
            local event = build_performance_event(
              latency_ms,
              cfg.model,
              cfg.mode,
              "success",
              prompt_tokens,
              completion_tokens,
              cost_increment,
              cost_session,
              context and context.cue_id,
              context and context.run_id,
              #text_val
            )
            log_performance(event)
          end)
        end
        safe_log()
        callback(nil, {
          text = text_val,
          model = cfg.model,
          usage = usage_val,
          run_id = context and context.run_id,
          cue_id = context and context.cue_id,
        })
      end
      if exit_code == 0 and response ~= "" then
        log_and_succeed(response, last_usage)
      else
        log_and_fail("stream failed", "failure")
      end
    end,
  })
  if jid and jid > 0 then
    active_jobs[jid] = { tmp = tmp, prompt = prompt, context = context, callback = callback, start_time = start_time }
  else
    os.remove(tmp)
    local end_time = vim.loop.gettimeofday()
    local latency_ms = (end_time.seconds - start_time.seconds) * 1000 + (end_time.microseconds - start_time.microseconds) / 1000
    vim.notify("glm · could not start stream", vim.log.levels.ERROR)
    done = true
    -- Log start failure
    local function safe_log()
      pcall(function()
        local event = build_performance_event(
          latency_ms,
          cfg.model,
          cfg.mode,
          "start_failure",
          0,
          0,
          0,
          U.cost or 0,
          context and context.cue_id,
          context and context.run_id,
          0
        )
        log_performance(event)
      end)
    end
    safe_log()
    callback("could not start stream", nil)
  end
end

---------------------------------------------- user-facing commands ------

--- Ask GLM from the cursor line — super fast feedback loop for the agent in
--- the have-to-be-headless mode: prompt == visual line (or selection).
function M.ask_cursor()
  local text = vim.fn.getline(".")
  if vim.fn.mode():find("^[vV\22]") then
    local s = vim.fn.getpos("v")
    local e = vim.fn.getpos(".")
    text = table.concat(vim.api.nvim_buf_get_lines(0, s[2] - 1, e[2], false), "\n")
  end
  U.visible = true
  local res, err = M.complete(text)
  if res then
    local lines = vim.split(res, "\n", { plain = true })
    vim.api.nvim_put(lines, "l", true, true)
  else
    vim.notify("glm · " .. tostring(err), vim.log.levels.ERROR)
  end
end

--- Persistent chat: float window, history kept in-between calls.
M.float = nil

function M.chat()
  local width = math.min(100, math.floor(vim.o.columns * 0.55))
  local height = math.min(30, math.floor(vim.o.lines * 0.6))
  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_name(buf, "strudel-chat")
  vim.api.nvim_buf_set_option(buf, "buftype", "nofile")
  vim.api.nvim_buf_set_option(buf, "filetype", "markdown")
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, { "— strudel chat · " .. cfg.model .. " —", "", "" })
  local win = vim.api.nvim_open_win(buf, true, {
    relative = "editor",
    width = width,
    height = height,
    row = math.floor((vim.o.lines - height) / 2),
    col = math.floor((vim.o.columns - width) / 2),
    style = "minimal",
    border = "rounded",
    title = " ⚡ glm · " .. cfg.model .. " ",
    title_pos = "center",
  })
  vim.api.nvim_set_hl(0, "FloatBorder", { fg = "#b78fd6", bg = "#242033" })
  M.float = { buf = buf, win = win }
  return win
end

--- Send a chat message; streaming appends over a job.
function M.chat_send(text)
  if text == "" then
    return
  end
  local win = M.float and M.float.win
  local buf = M.float and M.float.buf
  if not (win and buf and vim.api.nvim_win_is_valid(win)) then
    win = M.chat()
    buf = M.float.buf
  end
  vim.api.nvim_buf_set_lines(buf, -1, -1, false, { "you: " .. text })
  vim.api.nvim_buf_set_lines(buf, -1, -1, false, { "glm: " })
  U.visible = true
  -- streaming via curl -N in a Job
  local tmp = vim.fn.tempname()
  local f = assert(io.open(tmp, "w"))
  f:write(vim.json.encode({
    model = cfg.model,
    messages = messages(append_message(U.history, { role = "user", content = text })),
    temperature = cfg.temperature,
    max_tokens = cfg.max_tokens,
    stream = true,
  }))
  f:close()
  local cmd = { "curl", "-sS", "-N", "-m", tostring(cfg.timeout) }
  for k, v in pairs(headers()) do
    cmd[#cmd + 1] = "-H"
    cmd[#cmd + 1] = string.format("%s: %s", k, v)
  end
  cmd[#cmd + 1] = "-d"
  cmd[#cmd + 1] = "@" .. tmp
  cmd[#cmd + 1] = cfg.base_url .. "/chat/completions"
  local reasoning_phase = false -- set once when the model switches to thought text
  local response_parts = {}
  local jid
  jid = vim.fn.jobstart(cmd, {
    stdout_buffered = false,
    on_stdout = function(_, data)
      for _, chunk in ipairs(data) do
        for line in chunk:gmatch("[^\r\n]+") do
          if line:find("^data: ") then
            local payload = line:sub(7)
            if payload ~= "[DONE]" then
              local ok, obj = pcall(vim.json.decode, payload)
              if ok and obj.choices and obj.choices[1] and obj.choices[1].delta then
                local d = obj.choices[1].delta
                -- glm-5.3-flash streams reasoning with content=""; surface it so
                -- the float is never silently empty. "⸙ " marks thought text.
                local is_reasoning = (d.content == nil or d.content == "") and d.reasoning ~= nil
                local piece = d.content and d.content ~= "" and d.content or d.reasoning
                if piece then
                  response_parts[#response_parts + 1] = piece
                  local cur = vim.api.nvim_buf_get_lines(buf, -1, -1, false)[1] or ""
                  if is_reasoning and not reasoning_phase then
                    cur = cur .. "⸙ "
                    reasoning_phase = true
                  elseif not is_reasoning then
                    reasoning_phase = false
                  end
                  vim.api.nvim_buf_set_lines(buf, -1, -1, false, { cur .. piece })
                  -- scroll float to bottom
                  if vim.api.nvim_win_is_valid(win) then
                    vim.api.nvim_win_set_cursor(win, { vim.api.nvim_buf_line_count(buf), 0 })
                  end
                end
              end
            end
          end
        end
      end
    end,
    on_stderr = function(_, data)
      for _, line in ipairs(data or {}) do
        if line and line ~= "" then
          log("glm stderr:", line)
        end
      end
    end,
    on_exit = function(job_id, exit_code)
      active_jobs[job_id] = nil
      os.remove(tmp)
      local response = table.concat(response_parts)
      if exit_code == 0 and response ~= "" then
        record_turn(text, response)
      end
      log("glm · stream done")
    end,
  })
  if jid and jid > 0 then
    active_jobs[jid] = { tmp = tmp }
  else
    os.remove(tmp)
    vim.notify("glm · could not start stream", vim.log.levels.ERROR)
  end
end

--- Kill in-flight streams (used by <leader>ax / :StrudelStop).
function M.stop_all()
  for jid, job in pairs(active_jobs) do
    active_jobs[jid] = nil
    pcall(vim.fn.jobstop, jid)
    if type(job) == "table" then
      if job.tmp then
        os.remove(job.tmp)
      end
      -- Emit cancellation event for this job
      local function safe_log()
        pcall(function()
          local end_time = vim.loop.gettimeofday()
          local start_time = job.start_time or vim.loop.gettimeofday()
          local latency_ms = (end_time.seconds - start_time.seconds) * 1000 + (end_time.microseconds - start_time.microseconds) / 1000
          local event = build_performance_event(
            latency_ms,
            cfg.model,
            cfg.mode,
            "cancelled",
            0,
            0,
            0,
            U.cost or 0,
            job.context and job.context.cue_id,
            job.context and job.context.run_id,
            0
          )
          log_performance(event)
        end)
      end
      safe_log()
      -- Invoke callback with cancellation error
      if job.callback then
        job.callback("cancelled", nil)
      end
    end
  end
  vim.notify("glm · stopped in-flight streams", vim.log.levels.INFO)
end

--- Set the livecoding mode (updates g + redraws statusline).
function M.set_mode(mode)
  cfg.mode = mode
  vim.g.strudel_mode = mode
  emit()
  vim.notify("agent mode → " .. mode, vim.log.levels.INFO)
end

---------------------------------------------- health check --------------

--- Offline half of the health check: key presence + model config sanity.
--- Never touches the network (no curl), so the agent can run it safely even
--- when the API is down or the key is missing.
function M.check_local()
  local rows = {}
  if cfg.api_key and cfg.api_key ~= "" then
    rows[#rows + 1] = { "key", cfg.api_key:sub(1, 8) .. "…" }
  else
    rows[#rows + 1] = { "key", "MISSING" }
    rows[#rows + 1] = { "hint", "set OPENROUTER_API_KEY or strudel_api_key" }
  end
  rows[#rows + 1] = { "model", cfg.model .. (cfg.model == "z-ai/glm-5.3-flash" and " ✓" or " (non-default)") }
  rows[#rows + 1] = { "mode", cfg.mode }
  rows[#rows + 1] = { "budget", tostring(cfg.budget_usd or "unset") }
  return rows
end

--- :StrudelCheck — key presence, model pricing, connectivity.
--- Network half is resilient: some OpenRouter aliases 404 on GET /models/<slug>
--- (the model exists but isn't individually addressable there), so we fall back
--- to a real 1-token chat completion as the reachability probe. That also
--- double-checks the key end-to-end instead of only the models listing.
function M.check(only_local)
  local all = {}
  for _, r in ipairs(M.check_local()) do
    all[#all + 1] = r
  end
  if not only_local then
    local data, code = jget("/models/" .. cfg.model)
    if data then
      local p = data.pricing or {}
      all[#all + 1] = { "model", ("%s (ctx %s)"):format(cfg.model, data.context_length or "?") }
      all[#all + 1] = { "price", ("$%.2e / $%.2e (prompt/completion)"):format(tonumber(p.prompt) or 0, tonumber(p.completion) or 0) }
      all[#all + 1] = { "auth", "ok — /models reachable" }
    else
      -- per-model endpoint 404'd (known OpenRouter quirk for some slugs).
      -- Fall back to a real completion so the check still proves auth + model.
      local probe, perr = M.complete("ping")
      if probe then
        all[#all + 1] = { "model", cfg.model .. " ✓ (via chat completion probe)" }
        all[#all + 1] = { "auth", "ok — chat/completions reachable (per-model GET 404'd)" }
      else
        all[#all + 1] = { "auth", "http " .. tostring(code or "?") .. " — check OPENROUTER_API_KEY" }
      end
    end
  end
  local lines = {}
  for _, r in ipairs(all) do
    lines[#lines + 1] = string.format("%-8s %s", r[1], r[2])
  end
  vim.notify(table.concat(lines, "\n"), vim.log.levels.INFO, { title = "strudel · health" })
  return all
end

---------------------------------------------- setup ----------------------

function M.setup(opts)
  cfg = opts
  U.visible = true
  U.cost = 0
  U.prompt_tokens = 0
  U.completion_tokens = 0
  emit()

  vim.api.nvim_create_user_command("StrudelAsk", function(input)
    local ok, err = pcall(M.complete, input.args)
    if not ok then
      vim.notify("glm · " .. tostring(err), vim.log.levels.ERROR)
    end
  end, { nargs = "*", desc = "One-shot ask to glm" })

  vim.api.nvim_create_user_command("StrudelChat", function()
    M.chat()
  end, { desc = "Open the glm chat float" })

  vim.api.nvim_create_user_command("StrudelCheck", function(input)
    if input.args:find("%f[%a]local") then
      M.check(true) -- offline only
    else
      M.check(false)
    end
  end, { nargs = "*", desc = "Health-check the OpenRouter driver (--local = offline)" })

  vim.api.nvim_create_user_command("StrudelMeter", function()
    M.toggle_meter()
  end, { desc = "Toggle the ⚡ cost meter" })

  vim.api.nvim_create_user_command("StrudelStop", function()
    M.stop_all()
  end, { desc = "Stop in-flight glm streams" })
end

M._test = {
  append_message = append_message,
  record_turn = record_turn,
  history = function() return U.history or {} end,
  reset_history = function() U.history = {} end,
  active_jobs = active_jobs,
  --- Test-only: accumulate text and usage from a list of SSE payload strings.
  --- Each string is the JSON payload after "data: " prefix.
  --- Returns accumulated text and last usage object seen.
  accumulate_from_chunks = function(chunks)
    local response_parts = {}
    local last_usage = nil
    for _, chunk in ipairs(chunks) do
      local ok, obj = pcall(vim.json.decode, chunk)
      if ok and obj.choices and obj.choices[1] and obj.choices[1].delta then
        local d = obj.choices[1].delta
        local piece = d.content and d.content ~= "" and d.content or d.reasoning
        if piece then
          table.insert(response_parts, piece)
        end
        if obj.usage then
          last_usage = obj.usage
        end
      end
    end
    return table.concat(response_parts), last_usage
  end,
  --- Test-only: simulate complete_async with synthetic SSE chunks and exit code.
  --- Invokes the callback exactly once with either success or error.
  simulate_complete_async = function(prompt, context, callback, chunks, exit_code)
    local tmp = vim.fn.tempname()
    local text, usage = M._test.accumulate_from_chunks(chunks)
    local done = false
    local function finish(err, result)
      if done then return end
      done = true
      os.remove(tmp)
      if err then
        callback(err, nil)
      else
        callback(nil, result)
      end
    end
    if exit_code == 0 and text ~= "" then
      -- record turn (but skip accounting to avoid affecting test meter)
      M._test.record_turn(prompt, text)
      finish(nil, { text = text, model = cfg.model, usage = usage, run_id = context and context.run_id, cue_id = context and context.cue_id })
    else
      finish("stream failed", nil)
    end
  end,
}

return M