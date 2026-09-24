# OpenRouterSwarm Chat

A sidebar chat panel for **Cursor** (and VS Code) that talks to OpenRouter using
the same model roster as `swarm/config.json`. It docks in the activity bar
alongside other assistant panels.

Cursor is a VS Code fork, so this is an ordinary VS Code extension — nothing
Cursor-specific is required.

## Install (development)

```sh
cd cursor-extension
npm install
npm run compile
```

Then press <kbd>F5</kbd> in Cursor/VS Code to launch an Extension Development
Host with the panel loaded. To install it permanently, package it:

```sh
npx @vscode/vsce package      # produces openrouterswarm-chat-0.1.0.vsix
```

and in Cursor run **Extensions: Install from VSIX…** from the command palette.

## First run

Open the OpenRouterSwarm icon in the activity bar and send a message. You'll be
prompted for an OpenRouter API key once.

## Where your API key lives

The key is stored through VS Code's `SecretStorage`, which is backed by the OS
keychain (Keychain on macOS, libsecret on Linux, Credential Manager on Windows).

It is deliberately **not** a settings entry. Keys in `settings.json` get synced
across machines by Settings Sync, land in screen shares, and get committed by
accident — which is the exact failure this repo already had once.

Consequences worth knowing:

- The key never touches the repo, `settings.json`, or any log.
- A `401` clears the stored key automatically, so a rotated key can't get stuck.
- `OpenRouterSwarm: Clear Stored API Key` removes it on demand.

The only network destination in this extension is `https://openrouter.ai`.
There is no telemetry and no third-party endpoint.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `openrouterswarm.models` | the ten `:free` models from `swarm/config.json` | Roster offered by the model picker |
| `openrouterswarm.model` | `inclusionai/ling-3.0-flash-fin:free` | Active model |
| `openrouterswarm.systemPrompt` | concise-coding-assistant prompt | Prepended to every conversation |
| `openrouterswarm.includeSelection` | `true` | Attach the editor selection as context |
| `openrouterswarm.temperature` | `0.3` | Sampling temperature |
| `openrouterswarm.maxTokens` | `2048` | Max tokens per reply |

## Commands

- `OpenRouterSwarm: Set API Key`
- `OpenRouterSwarm: Clear Stored API Key`
- `OpenRouterSwarm: Select Model`
- `OpenRouterSwarm: New Chat`

## Usage notes

- <kbd>Enter</kbd> sends, <kbd>Shift</kbd>+<kbd>Enter</kbd> inserts a newline.
- With `includeSelection` on, a non-empty editor selection is appended to your
  message as a fenced block tagged with its file path and line range.
- **Stop** aborts a running stream and keeps the partial reply.
- Free OpenRouter models are rate-limited to roughly 20 requests/minute; a `429`
  is reported in the panel rather than retried silently.

## Webview hardening

The panel runs under a strict Content-Security-Policy: `default-src 'none'`,
scripts allowed only via a per-render nonce, and resources restricted to the
extension's own `media/` directory. Model output is rendered with `textContent`,
never `innerHTML`, so a reply containing markup cannot execute in the panel.

## Tests

```sh
npm test
```

Covers the SSE parser: frames split across arbitrary chunk boundaries, CRLF line
endings, keep-alive comments, malformed frames, in-stream errors, HTTP 401
handling, and role-only deltas.
