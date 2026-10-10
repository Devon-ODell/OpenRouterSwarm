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
if ! grep -qs '^OPENROUTER_API_KEY=.\+' "$ROOT/.env" 2>/dev/null; then
  key=""
  if [ -t 0 ]; then
    echo ""
    echo "You need an OpenRouter API key (free to create): https://openrouter.ai/keys"
    read -r -s -p "OpenRouter API key (sk-or-...): " key; echo
  fi
  if [ -n "$key" ]; then
    umask 077
    printf 'OPENROUTER_API_KEY=%s\n' "$key" >> "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    echo "==> wrote $ROOT/.env"
  else
    echo "!! no API key yet: add OPENROUTER_API_KEY=sk-or-... to $ROOT/.env, then re-run this script"
  fi
else
  echo "==> .env has OPENROUTER_API_KEY"
fi

# ---------------------------------------------------------------- 3. config
if [ ! -f "$ROOT/swarm/config.json" ]; then
  cp "$ROOT/swarm/config.example.json" "$ROOT/swarm/config.json"
  echo "==> created swarm/config.json from the template (it is not tracked by git)"
else
  echo "==> keeping your swarm/config.json"
fi

# ---------------------------------------------------------------- 3b. commands
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

# ---------------------------------------------------------------- 6. OpenRouter credit-tier check
# OpenRouter's own policy, not something this project invents: a key with $0 ever purchased
# is capped at a low daily rate for free models. Load $10 in credits once (never spent down —
# free models stay free) and the cap rises to 1000/day, which is what every tuned config here
# assumes. This step tells you, concretely, which side of that line your key is on right now.
echo ""
echo "==> checking your OpenRouter account tier"
if grep -qs '^OPENROUTER_API_KEY=.\+' "$ROOT/.env" 2>/dev/null; then
  "$PY" - "$ROOT" <<'PYEOF' || true
import sys
sys.path.insert(0, sys.argv[1])
import os
for line in open(os.path.join(sys.argv[1], ".env")):
    if line.startswith("OPENROUTER_API_KEY="):
        os.environ["OPENROUTER_API_KEY"] = line.strip().split("=", 1)[1]
import flint
info = flint.fetch_key_info(os.environ["OPENROUTER_API_KEY"])
if info is None:
    print("    could not reach OpenRouter to check (offline, or the key is invalid) — "
          "the swarm will tell you the same thing on first run.")
elif info.get("is_free_tier"):
    quota = info.get("free_model_daily_requests") or {}
    print(f"    free tier, no credits purchased yet — today's free-model limit is "
          f"{quota.get('limit', 'low')}, not the 1000/day this project is tuned for.")
    print("    One-time fix: add $10 in credits at https://openrouter.ai/credits")
    print("    (you will not be charged unless you also turn on paid fallback — free models stay free)")
else:
    quota = info.get("free_model_daily_requests") or {}
    print(f"    credits on file — free-model limit today: {quota.get('limit', '1000')}/day. You're set.")
PYEOF
else
  echo "    skipped (no API key yet)"
fi

cat <<EOF

Done. Next:
  flint                                          # one interactive session, right here
  swarm init ~/path/to/some/other/repo           # point the swarm at a project (writes a tuned config)
  swarm grind ~/path/to/some/other/repo --goal "what it should become"   # start it

Fully quit Cursor and open it again, then look for Flint in the secondary sidebar.
EOF
