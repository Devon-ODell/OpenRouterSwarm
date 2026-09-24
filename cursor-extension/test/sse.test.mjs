import { streamChat, OpenRouterError } from "../out/openrouter.js";

function bodyFrom(chunks) {
  const enc = new TextEncoder();
  let i = 0;
  return { getReader: () => ({ read: async () => i < chunks.length
    ? { done: false, value: enc.encode(chunks[i++]) } : { done: true } }) };
}
function mockFetch(chunks, ok = true, status = 200, text = "") {
  globalThis.fetch = async () => ({ ok, status, body: ok ? bodyFrom(chunks) : null, text: async () => text });
}
const frame = (c) => `data: ${JSON.stringify({ choices: [{ delta: { content: c } }] })}\n\n`;
const run = async () => {
  let toks = [];
  const out = await streamChat(
    { apiKey: "k", model: "m", messages: [], temperature: 0, maxTokens: 10, signal: new AbortController().signal },
    { onToken: (t) => toks.push(t) });
  return { out, toks };
};

let pass = 0, fail = 0;
const check = (name, cond, extra = "") => { cond ? (pass++, console.log("  PASS", name)) : (fail++, console.log("  FAIL", name, extra)); };

// 1. Clean frames
mockFetch([frame("Hello"), frame(" world"), "data: [DONE]\n\n"]);
let r = await run();
check("clean frames", r.out === "Hello world", r.out);

// 2. Frames split at arbitrary byte boundaries (the real-world case)
const whole = frame("Hel") + frame("lo!") + "data: [DONE]\n\n";
const split = [];
for (let i = 0; i < whole.length; i += 7) split.push(whole.slice(i, i + 7));
mockFetch(split);
r = await run();
check("split mid-frame", r.out === "Hello!", JSON.stringify(r.out));

// 3. CRLF line endings + keep-alive comments
mockFetch([": keep-alive\r\n\r\n", `data: ${JSON.stringify({choices:[{delta:{content:"X"}}]})}\r\n\r\n`, "data: [DONE]\r\n\r\n"]);
r = await run();
check("CRLF + comments", r.out === "X", JSON.stringify(r.out));

// 4. Malformed frame is skipped, stream survives
mockFetch(["data: {not json\n\n", frame("ok"), "data: [DONE]\n\n"]);
r = await run();
check("malformed frame skipped", r.out === "ok", JSON.stringify(r.out));

// 5. In-stream error surfaces
mockFetch([`data: ${JSON.stringify({ error: { message: "upstream died" } })}\n\n`]);
try { await run(); check("in-stream error", false, "did not throw"); }
catch (e) { check("in-stream error", e instanceof OpenRouterError && /upstream died/.test(e.message), e.message); }

// 6. HTTP 401 gets the actionable message + status
mockFetch([], false, 401, '{"error":{"message":"no auth"}}');
try { await run(); check("401 handling", false, "did not throw"); }
catch (e) { check("401 handling", e.status === 401 && /Set API Key/.test(e.message), e.message); }

// 7. Empty deltas (role-only first frame) produce no spurious tokens
mockFetch([`data: ${JSON.stringify({choices:[{delta:{role:"assistant"}}]})}\n\n`, frame("hi"), "data: [DONE]\n\n"]);
r = await run();
check("empty delta ignored", r.out === "hi" && r.toks.length === 1, JSON.stringify(r.toks));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
