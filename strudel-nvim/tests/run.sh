#!/usr/bin/env bash
# strudel.nvim headless test runner — macOS/Linux, nvim >= 0.10
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NVIM="${NVIM:-nvim}"
NVIM_TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$NVIM_TEST_DIR"' EXIT

NVIM_VERSION="$(cd "$NVIM_TEST_DIR" && NVIM_LOG_FILE="$NVIM_TEST_DIR/nvim.log" "$NVIM" --version | head -1)"
echo "==> strudel.nvim tests (nvim $NVIM_VERSION)"

for t in tests/smoke_test.lua tests/check_test.lua tests/regression_test.lua tests/wezterm_parity_test.lua; do
  if [ -f "$ROOT/$t" ]; then
    echo "==> running $t"
    (cd "$NVIM_TEST_DIR" && NVIM_LOG_FILE="$NVIM_TEST_DIR/nvim.log" "$NVIM" --headless -u NONE -i NONE \
      --cmd "set rtp^=$ROOT" --cmd "let g:strudel_test_root='$ROOT'" \
      -c "luafile $ROOT/$t" -c "if v:errmsg != '' | cquit 1 | endif" -c "qa")
  fi
done

echo "==> done"
