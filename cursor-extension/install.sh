#!/usr/bin/env bash
# Test, package and install the Flint Swarm extension into Cursor (and VS Code when present).
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
printf '%s\n' "$ROOT" > flint-root.txt        # where the packaged extension finds swarm/bridge.py
node test/extension.test.js
npx --yes @vscode/vsce@3 package --skip-license --out flint-swarm.vsix
installed=0
CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
if [ -x "$CURSOR_CLI" ]; then "$CURSOR_CLI" --install-extension flint-swarm.vsix --force && installed=1; fi
if command -v code >/dev/null 2>&1; then code --install-extension flint-swarm.vsix --force && installed=1; fi
if [ "$installed" = 1 ]; then
  echo "Installed. In Cursor run 'Developer: Reload Window', then open the Flint Swarm icon in the activity bar."
else
  echo "Packaged $(pwd)/flint-swarm.vsix; install it with 'Extensions: Install from VSIX...'."
fi
