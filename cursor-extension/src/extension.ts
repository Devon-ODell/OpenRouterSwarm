import * as vscode from "vscode";
import { ChatViewProvider } from "./chatView";

export function activate(ctx: vscode.ExtensionContext): void {
  const provider = new ChatViewProvider(ctx);

  ctx.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, provider, {
      // Keep the conversation when the panel is hidden behind another view.
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.commands.registerCommand("openrouterswarm.setApiKey", () => provider.promptForKey()),
    vscode.commands.registerCommand("openrouterswarm.clearApiKey", () => provider.clearKey()),
    vscode.commands.registerCommand("openrouterswarm.pickModel", () => provider.pickModel()),
    vscode.commands.registerCommand("openrouterswarm.newChat", () => provider.newChat()),
  );
}

export function deactivate(): void {
  // Nothing to tear down: streams are aborted by the view's own lifecycle.
}
