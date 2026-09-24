// Webview UI. Runs sandboxed under a strict CSP: no remote loads, no eval.
// Model output is inserted with textContent only, never innerHTML, so a reply
// containing markup can never execute inside the panel.
(function () {
  const vscode = acquireVsCodeApi();

  const log = document.getElementById("log");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const stopBtn = document.getElementById("stop");
  const newBtn = document.getElementById("new");
  const modelBtn = document.getElementById("model");
  const banner = document.getElementById("banner");
  const hint = document.getElementById("hint");

  let streamBody = null;
  let hasKey = false;

  function atBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  }
  function scroll(force) {
    if (force) { log.scrollTop = log.scrollHeight; }
  }

  function bubble(role, text) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + role;
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = role === "user" ? "You" : "Assistant";
    const body = document.createElement("div");
    body.className = "body";
    if (text !== undefined) { body.textContent = text; }
    wrap.append(who, body);
    log.append(wrap);
    scroll(true);
    return body;
  }

  function showBanner(text) {
    banner.textContent = text;
    banner.classList.remove("hidden");
  }
  function hideBanner() {
    banner.classList.add("hidden");
  }

  function streaming(on) {
    sendBtn.classList.toggle("hidden", on);
    stopBtn.classList.toggle("hidden", !on);
    input.disabled = on;
  }

  function send() {
    const text = input.value.trim();
    if (!text) { return; }
    input.value = "";
    hideBanner();
    vscode.postMessage({ type: "send", text: text });
  }

  sendBtn.addEventListener("click", send);
  stopBtn.addEventListener("click", () => vscode.postMessage({ type: "abort" }));
  newBtn.addEventListener("click", () => vscode.postMessage({ type: "newChat" }));
  modelBtn.addEventListener("click", () => vscode.postMessage({ type: "pickModel" }));

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  window.addEventListener("message", (event) => {
    const m = event.data;
    switch (m.type) {
      case "state":
        hasKey = m.hasKey;
        modelBtn.textContent = m.model || "(no model)";
        hint.textContent = hasKey ? "" : "No API key set - click Send to add one.";
        break;
      case "user":
        bubble("user", m.text);
        break;
      case "assistantStart":
        streamBody = bubble("assistant", "");
        streaming(true);
        break;
      case "token": {
        if (!streamBody) { streamBody = bubble("assistant", ""); }
        const stick = atBottom();
        streamBody.textContent += m.text;
        scroll(stick);
        break;
      }
      case "done":
        streamBody = null;
        streaming(false);
        input.focus();
        break;
      case "error":
        streamBody = null;
        streaming(false);
        showBanner(m.text);
        break;
      case "cleared":
        log.replaceChildren();
        hideBanner();
        streaming(false);
        break;
    }
  });

  vscode.postMessage({ type: "ready" });
})();
