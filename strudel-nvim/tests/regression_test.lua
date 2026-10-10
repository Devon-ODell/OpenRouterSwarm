-- Regression coverage for runtime paths that the original smoke suite did not execute.

require("strudel").setup {}

-- The VimEnter callback must use a real Neovim/libuv timer API and start cleanly.
vim.api.nvim_exec_autocmds("VimEnter", {})
assert(require("strudel")._test.timer_active(), "playtime timer did not start")

-- Appending a turn must retain every earlier integer-indexed history entry.
local agent = require("strudel.agent")
local old = {
  { role = "user", content = "first" },
  { role = "assistant", content = "answer" },
}
local appended = agent._test.append_message(old, { role = "user", content = "second" })
assert(#old == 2, "append mutated existing history")
assert(#appended == 3, "new turn did not append")
assert(appended[1].content == "first" and appended[2].content == "answer" and appended[3].content == "second",
  "history order was clobbered")
agent._test.reset_history()
agent._test.record_turn("first", "answer")
agent._test.record_turn("second", "answer two")
local history = agent._test.history()
assert(#history == 4 and history[1].content == "first" and history[3].content == "second",
  "completed streaming turns were not retained in order")

-- StrudelStop must act on the job ids the agent owns, not a nonexistent vim.v.jobs.
local jid = vim.fn.jobstart({ "sh", "-c", "sleep 10" })
assert(jid > 0, "test job failed to start")
agent._test.active_jobs[jid] = true
agent.stop_all()
assert(next(agent._test.active_jobs) == nil, "stopped jobs remained tracked")
local status = vim.fn.jobwait({ jid }, 1000)[1]
assert(status ~= -1, "StrudelStop left the tracked job running")

-- RFC 4648 vectors exercise both one- and two-character padding.
local b64 = require("strudel.strudel")._test.b64encode
assert(b64("f") == "Zg==", "two-byte base64 padding is malformed")
assert(b64("fo") == "Zm8=", "one-byte base64 padding is malformed")
assert(b64("foo") == "Zm9v", "unpadded base64 is malformed")

print("RUNTIME REGRESSIONS PASS")
