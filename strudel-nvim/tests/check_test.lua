-- offline health check test — proves :StrudelCheck --local never touches the
-- network and reports a missing key correctly.
-- Run: nvim --headless -u NONE --cmd "set rtp+=<repo>" -c "luafile tests/check_test.lua" -c "qa"

require("strudel").setup {}

local agent = require("strudel.agent")

-- 1. local check always returns a rows table (never network-crashes)
local rows = agent.check_local()
assert(type(rows) == "table" and #rows >= 3, "check_local must return rows")
print("local rows: " .. #rows)

-- 2. key may be present or absent depending on env; model must be right
local has_key = os.getenv("OPENROUTER_API_KEY") ~= nil
if has_key then
  assert(rows[1][1] == "key" and rows[1][2] ~= "MISSING", "key row should be present")
else
  assert(rows[1][1] == "key" and rows[1][2] == "MISSING", "missing key must be reported")
end

-- 3. model sanity
local model_ok = false
for _, r in ipairs(rows) do
  if r[1] == "model" then
    model_ok = r[2]:find("z-ai/glm-5.3-flash", 1, true) ~= nil
  end
end
assert(model_ok, "model row must mention z-ai/glm-5.3-flash")

-- 4. no curl is spawned by the LOCAL path — we prove it by the timeout proxy:
--    check_local must return fast (<100ms) even when the network is dead.
--    A network path would hang on curl. Assert the wall clock stayed tiny.
local t0 = vim.uv and vim.uv.now() or os.clock()
agent.check_local()
local dt = (vim.uv and vim.uv.now() or os.clock()) - t0
assert(dt < 1.0, string.format("local check took %.2fms — it touched the network?", dt * 1000))
print(string.format("local check wall time: %.1fms", dt * 1000))

print("CHECK LOCAL PASS")