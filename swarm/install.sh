#!/bin/bash
# install.sh — set up the flint swarm on this Mac.
#
#   ./swarm/install.sh /path/to/target-repo
#
# Idempotent: safe to re-run after you change things.
set -euo pipefail

SWARM="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SWARM")"
REPO="${1:-}"

if [ -z "$REPO" ]; then
  echo "usage: $0 /path/to/target-repo" >&2
  exit 2
fi
REPO="$(cd "$REPO" 2>/dev/null && pwd)" || { echo "no such directory: $1" >&2; exit 2; }

echo "harness : $ROOT"
echo "target  : $REPO"

# ---------------------------------------------------------------- 1. venv
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "==> creating venv"
  python3 -m venv "$ROOT/.venv"
fi
PY="$ROOT/.venv/bin/python"
"$PY" -m pip install -q -r "$ROOT/requirements.txt"
echo "==> venv ready ($("$PY" -V))"

# ---------------------------------------------------------------- 2. launcher
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/flint" <<EOF
#!/bin/sh
# flint from any directory. Without -C, flint works in your current folder.
exec "$PY" "$ROOT/flint.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/flint"

cat > "$HOME/.local/bin/swarm" <<EOF
#!/bin/sh
exec "$PY" "$SWARM/swarmd.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/swarm"

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
  for rc in "$HOME/.zshrc" "$HOME/.bash_profile"; do
    [ -f "$rc" ] || continue
    grep -q '.local/bin' "$rc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
  done
  echo "==> added ~/.local/bin to PATH (open a new terminal to pick it up)"
fi
echo "==> installed: flint, swarm"

# ---------------------------------------------------------------- 3. git
if ! git -C "$REPO" rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "!! $REPO is not a git repository."
  echo "   The swarm needs a committed baseline to create isolated worktrees."
  echo "   Run: git -C '$REPO' init && git -C '$REPO' add -A && git -C '$REPO' commit -m baseline"
  exit 3
fi
BASE="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
mkdir -p "$SWARM/logs" "$SWARM/state"
# Worktree dependency directories must be ignored by the target repository.
echo "==> workers test committed code in fresh worktrees; configure dependency setup in test_cmd"

# ---------------------------------------------------------------- 4. test cmd
if   [ -f "$REPO/go.mod" ];        then TEST_CMD="go test ./..."
elif [ -f "$REPO/pyproject.toml" ] || [ -f "$REPO/pytest.ini" ] || [ -d "$REPO/tests" ]; then
                                        TEST_CMD="python3 -m pytest -q"
elif [ -f "$REPO/package.json" ];  then TEST_CMD="npm test --silent"
elif [ -f "$REPO/Makefile" ];      then TEST_CMD="make test"
else                                    TEST_CMD="echo 'NO TEST COMMAND CONFIGURED' && false"
fi
echo "==> test command: $TEST_CMD"

# ---------------------------------------------------------------- 5. config
if [ ! -f "$SWARM/config.json" ] || grep -q '__REPO__' "$SWARM/config.json"; then
  "$PY" - "$SWARM/config.json" "$REPO" "$TEST_CMD" "$PY" "$BASE" <<'EOF'
import json, sys
path, repo, test, py, base = sys.argv[1:6]
c = json.load(open(path))
c.update(repo=repo, test_cmd=test, python=py, base_branch=base)
c.pop("_comment", None)
json.dump(c, open(path, "w"), indent=2)
EOF
  echo "==> wrote swarm/config.json"
else
  echo "==> config.json already customised — leaving it alone"
fi

# ---------------------------------------------------------------- 7. goal
if [ ! -f "$REPO/GOAL.md" ]; then
  cat > "$REPO/GOAL.md" <<'EOF'
# Goal

<!-- Replace this. The planner reads this file to decide what to build next,
     so vagueness here becomes wandering out there. Be concrete about the end
     state, and explicit about what is out of bounds. -->

## What this project should become

## Definition of done

## Out of bounds
- No live orders, no real credentials, no withdrawals.
- No network calls to exchanges outside recorded fixtures.
EOF
  echo "==> created $REPO/GOAL.md — FILL THIS IN before starting the swarm"
fi

cat <<EOF

Ready. Before starting:
  1. Fill in $REPO/GOAL.md with small, testable objectives.
  2. Edit $SWARM/config.json: verify test_cmd and daily_cap/reserve.
     test_cmd must work in a fresh worktree (install dependencies if needed).
  3. Run: swarm grind            (nonstop; or: swarm run --hours 8)
  4. Later: swarm report

Accepted work accumulates on swarm/trunk for review. Your current checkout
is never merged or reset by the supervisor. Ctrl-C stops the run.
EOF
