#!/usr/bin/env bash
# One-time setup for this checkout: Python venv, API key, `flint`/`swarm` commands,
# tests, and the Cursor extension wired to *this* folder. Safe to re-run.
#
#   ./setup.sh                 # everything
#   ./setup.sh --no-extension  # skip packaging/installing the Cursor extension
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
EXTENSION=1
[ "${1:-}" = "--no-extension" ] && EXTENSION=0
echo "flint checkout: $ROOT"

# ---------------------------------------------------------------- 1. venv
if [ ! -x "$PY" ]; then
  echo "==> creating .venv"
  python3 -m venv "$ROOT/.venv"
fi
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$ROOT/requirements.txt" truststore
echo "==> venv ready ($("$PY" -V))"

# ---------------------------------------------------------------- 2. API key
if ! grep -qs '^OPENROUTER_API_KEY=.\+' "$ROOT/.env"; then
  key=""
  for old in "$HOME/Desktop/LingAI-Trader/.env" "$ROOT/../LingAI-Trader/.env"; do
    if [ -f "$old" ] && grep -qs '^OPENROUTER_API_KEY=.\+' "$old"; then
      key="$(grep '^OPENROUTER_API_KEY=' "$old" | tail -1 | cut -d= -f2-)"
      echo "==> using OPENROUTER_API_KEY from $old"
      break
    fi
  done
  if [ -z "$key" ] && [ -t 0 ]; then
    read -r -s -p "OpenRouter API key (sk-or-...): " key; echo
  fi
  if [ -n "$key" ]; then
    umask 077
    printf 'OPENROUTER_API_KEY=%s\n' "$key" >> "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    echo "==> wrote $ROOT/.env"
  else
    echo "!! no API key yet: add OPENROUTER_API_KEY=sk-or-... to $ROOT/.env"
  fi
else
  echo "==> .env has OPENROUTER_API_KEY"
fi

# ---------------------------------------------------------------- 3. commands
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\nexec "%s" "%s/flint.py" "$@"\n' "$PY" "$ROOT" > "$HOME/.local/bin/flint"
printf '#!/bin/sh\nexec "%s" "%s/swarm/swarmd.py" "$@"\n' "$PY" "$ROOT" > "$HOME/.local/bin/swarm"
chmod +x "$HOME/.local/bin/flint" "$HOME/.local/bin/swarm"
if ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
  for rc in "$HOME/.zshrc" "$HOME/.bash_profile"; do
    [ -f "$rc" ] || continue
    grep -q '.local/bin' "$rc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
  done
  echo "==> added ~/.local/bin to PATH (open a new terminal to pick it up)"
fi
echo "==> 'flint' and 'swarm' now run this checkout"

# ---------------------------------------------------------------- 4. tests
echo "==> running offline tests"
(cd "$ROOT" && FLINT_HOME="$(mktemp -d)" "$PY" -m unittest discover -s tests -q)

# ---------------------------------------------------------------- 5. extension
if [ "$EXTENSION" = 1 ]; then
  if command -v npx >/dev/null 2>&1; then
    echo "==> packaging the Cursor extension against $ROOT"
    "$ROOT/cursor-extension/install.sh" || echo "!! extension install failed; see above"
  else
    echo "!! Node.js (npx) not found: install it (brew install node), then run cursor-extension/install.sh"
  fi
fi

cat <<EOF

Done. Next:
  .venv/bin/python flint.py                     # or just: flint
  swarm grind ~/path/to/game --goal "..."        # start the swarm on a project
In Cursor: 'Developer: Reload Window'. If you ever set 'Flint Swarm: Flint Path'
to the old LingAI-Trader folder, clear it (or set it to $ROOT).
EOF
