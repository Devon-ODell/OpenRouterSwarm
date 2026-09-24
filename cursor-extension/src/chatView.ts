import * as vscode from "vscode";
import { ChatMessage, OpenRouterError, streamChat } from "./openrouter";

export const SECRET_KEY = "openrouterswarm.apiKey";
const CFG = "openrouterswarm";

/** Random nonce so the webview CSP can allow exactly our one inline script. */
function nonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let out = "";
  for (let i = 0; i < 32; i++) {
    out += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return out;
}

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "openrouterswarm.chat";

  private view?: vscode.WebviewView;
  private history: ChatMessage[] = [];
  private inFlight?: AbortController;

  constructor(private readonly ctx: vscode.ExtensionContext) {}

  private cfg() {
    return vscode.workspace.getConfiguration(CFG);
  }

  /** Prompts for a key and stores it in the OS keychain via SecretStorage. */
  public async promptForKey(): Promise<string | undefined> {
    const entered = await vscode.window.showInputBox({
      title: "OpenRouter API Key",
      prompt: "Stored in the OS keychain via VS Code SecretStorage - never in settings.json.",
      placeHolder: "sk-or-v1-...",
      password: true,
      ignoreFocusOut: true,
      validateInput: (v) =>
        v.trim().length === 0
          ? "Key cannot be empty."
          : v.trim().startsWith("sk-or-")
            ? undefined
            : "Expected an OpenRouter key beginning with \"sk-or-\".",
    });
    if (!entered) {
      return undefined;
    }
    await this.ctx.secrets.store(SECRET_KEY, entered.trim());
    void vscode.window.showInformationMessage("OpenRouter API key saved to the keychain.");
    void this.pushState();
    return entered.trim();
  }

  public async clearKey(): Promise<void> {
    await this.ctx.secrets.delete(SECRET_KEY);
    void this.pushState();
    void vscode.window.showInformationMessage("OpenRouter API key removed from the keychain.");
  }

  public async pickModel(): Promise<void> {
    const models = this.cfg().get<string[]>("models", []);
    if (models.length === 0) {
      void vscode.window.showWarningMessage("No models configured under openrouterswarm.models.");
      return;
    }
    const active = this.cfg().get<string>("model", "");
    const choice = await vscode.window.showQuickPick(
      models.map((m) => ({ label: m, description: m === active ? "current" : undefined })),
      { title: "Select OpenRouter model", placeHolder: active },
    );
    if (choice) {
      await this.cfg().update("model", choice.label, vscode.ConfigurationTarget.Global);
      void this.pushState();
    }
  }

  public newChat(): void {
    this.inFlight?.abort();
    this.history = [];
    this.view?.webview.postMessage({ type: "cleared" });
    void this.pushState();
  }

  public resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.ctx.extensionUri, "media")],
    };
    view.webview.html = this.html(view.webview);

    view.webview.onDidReceiveMessage(async (msg: { type: string; text?: string }) => {
      switch (msg.type) {
        case "ready":
          await this.pushState();
          break;
        case "send":
          await this.send(msg.text ?? "");
          break;
        case "abort":
          this.inFlight?.abort();
          break;
        case "newChat":
          this.newChat();
          break;
        case "pickModel":
          await this.pickModel();
          break;
        case "setKey":
          await this.promptForKey();
          break;
      }
    });

    // Keep the header in sync when the model is changed from settings.
    const sub = vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration(CFG)) {
        void this.pushState();
      }
    });
    view.onDidDispose(() => sub.dispose());
  }

  private async pushState(): Promise<void> {
    const hasKey = Boolean(await this.ctx.secrets.get(SECRET_KEY));
    this.view?.webview.postMessage({
      type: "state",
      hasKey,
      model: this.cfg().get<string>("model", ""),
      turns: this.history.filter((m) => m.role === "user").length,
    });
  }

  /** Editor selection, attached as a fenced block so the model sees real context. */
  private selectionContext(): string {
    if (!this.cfg().get<boolean>("includeSelection", true)) {
      return "";
    }
    const ed = vscode.window.activeTextEditor;
    if (!ed || ed.selection.isEmpty) {
      return "";
    }
    const text = ed.document.getText(ed.selection);
    if (!text.trim()) {
      return "";
    }
    const rel = vscode.workspace.asRelativePath(ed.document.uri);
    const start = ed.selection.start.line + 1;
    const end = ed.selection.end.line + 1;
    return `\n\nSelected from \`${rel}\` (lines ${start}-${end}):\n\`\`\`${ed.document.languageId}\n${text}\n\`\`\``;
  }

  private async send(raw: string): Promise<void> {
    const text = raw.trim();
    if (!text || !this.view) {
      return;
    }

    let key = await this.ctx.secrets.get(SECRET_KEY);
    if (!key) {
      key = await this.promptForKey();
      if (!key) {
        this.view.webview.postMessage({ type: "error", text: "No API key set." });
        return;
      }
    }

    const shown = text;
    this.history.push({ role: "user", content: text + this.selectionContext() });
    this.view.webview.postMessage({ type: "user", text: shown });
    this.view.webview.postMessage({ type: "assistantStart" });

    const system = this.cfg().get<string>("systemPrompt", "");
    const messages: ChatMessage[] = system
      ? [{ role: "system", content: system }, ...this.history]
      : [...this.history];

    this.inFlight?.abort();
    const ctrl = new AbortController();
    this.inFlight = ctrl;

    try {
      const full = await streamChat(
        {
          apiKey: key,
          model: this.cfg().get<string>("model", "inclusionai/ling-3.0-flash-fin:free"),
          messages,
          temperature: this.cfg().get<number>("temperature", 0.3),
          maxTokens: this.cfg().get<number>("maxTokens", 2048),
          signal: ctrl.signal,
        },
        { onToken: (t) => this.view?.webview.postMessage({ type: "token", text: t }) },
      );
      this.history.push({ role: "assistant", content: full });
      this.view.webview.postMessage({ type: "done" });
    } catch (err) {
      // A user-initiated abort is not an error; keep the partial reply.
      if (err instanceof Error && err.name === "AbortError") {
        this.view.webview.postMessage({ type: "done" });
      } else {
        const message = err instanceof Error ? err.message : String(err);
        this.view.webview.postMessage({ type: "error", text: message });
        if (err instanceof OpenRouterError && err.status === 401) {
          await this.ctx.secrets.delete(SECRET_KEY);
        }
        // Drop the unanswered turn so a retry doesn't stack duplicates.
        this.history.pop();
      }
    } finally {
      if (this.inFlight === ctrl) {
        this.inFlight = undefined;
      }
      await this.pushState();
    }
  }

  private html(webview: vscode.Webview): string {
    const media = (f: string) =>
      webview.asWebviewUri(vscode.Uri.joinPath(this.ctx.extensionUri, "media", f));
    const n = nonce();
    // No remote origins: styles and scripts load only from the extension bundle.
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${n}'`,
      `font-src ${webview.cspSource}`,
    ].join("; ");

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta http-equiv="Content-Security-Policy" content="${csp}" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<link href="${media("main.css")}" rel="stylesheet" />
<title>OpenRouterSwarm Chat</title>
</head>
<body>
  <header id="bar">
    <button id="model" class="chip" title="Change model"></button>
    <span class="spacer"></span>
    <button id="new" class="chip" title="New chat">New</button>
  </header>
  <main id="log" aria-live="polite"></main>
  <div id="banner" class="hidden"></div>
  <footer>
    <textarea id="input" rows="3" placeholder="Ask about your code&#10;Enter to send, Shift+Enter for newline"></textarea>
    <div class="row">
      <span id="hint"></span>
      <span class="spacer"></span>
      <button id="stop" class="hidden">Stop</button>
      <button id="send">Send</button>
    </div>
  </footer>
  <script nonce="${n}" src="${media("main.js")}"></script>
</body>
</html>`;
  }
}
