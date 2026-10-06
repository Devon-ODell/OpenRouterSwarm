#!/usr/bin/env bash
# strudel.nvim headless test runner — macOS/Linux, nvim >= 0.10
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NVIM="${NVIM:-nvim}"

echo "==> strudel.nvim tests (nvim $("$NVIM" --version | head -1))"

for t in tests/smoke_test.lua tests/check_test.lua tests/wezterm_parity_test.lua; do
  if [ -f "$t" ]; then
    echo "==> running $t"
    "$NVIM" --headless -u NONE --cmd "set rtp+=$ROOT" -c "luafile $t" -c "qa"
  fi
done

echo "==> done"