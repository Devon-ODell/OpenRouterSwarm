#!/usr/bin/env bash
# strudel.nvim installer — wires the plugin into ~/.config/nvim with zero
# manager dependencies (no lazy.nvim needed). Idempotent: safe to re-run.
#
#   ./install.sh            install (symlink) + print next steps
#   ./install.sh --copy     copy the files instead of symlinking
#   ./install.sh --uninstall
#
# The plugin is also a normal Neovim plugin, so lazy.nvim users can instead do:
#
#   { dir = "~/Desktop/OpenRouterSwarm/strudel-nvim", name = "strudel" }
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NVIM_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nvim"
# The plugin files live directly in the repo root (plugin/, lua/, etc.), not in
# a nested strudel-nvim/ subdir. Handle both layouts so install.sh stays
# idempotent no matter where it's run from.
if [ -d "$ROOT/strudel-nvim/plugin" ]; then
  SRC="$ROOT/strudel-nvim"
else
  SRC="$ROOT"
fi
MODE="${1:---link}"

mkdir -p "$NVIM_DIR"
mkdir -p "$NVIM_DIR/plugin"
mkdir -p "$NVIM_DIR/ftdetect"
mkdir -p "$NVIM_DIR/syntax"
mkdir -p "$NVIM_DIR/lua/strudel"
mkdir -p "$NVIM_DIR/after/ftplugin"

link() {
  local dst="$1"; shift
  rm -rf "$dst"
  ln -sfn "$SRC/$1" "$dst"
}

copy() {
  local dst="$1"; shift
  rm -rf "$dst"
  cp -R "$SRC/$1" "$dst"
}

install_one() { # dst_subpath src_relpath
  case "$MODE" in
    --link)   link  "$NVIM_DIR/$1" "$2" ;;
    --copy)   copy  "$NVIM_DIR/$1" "$2" ;;
    *) echo "unknown mode: $MODE"; exit 1 ;;
  esac
}

echo "==> installing strudel.nvim ($MODE) into $NVIM_DIR"

# core entry point — shared/after so plugins can't clobber our autocmds
install_one "plugin/strudel.lua"     "plugin/strudel.lua"
install_one "ftdetect/strudel.vim"   "ftdetect/strudel.vim"
install_one "syntax/strudel.vim"     "syntax/strudel.vim"
install_one "lua/strudel"            "lua/strudel"

# tiny after/ftplugin so filetype options apply on top of any others
install_one "after/ftplugin/strudel.lua" "after/ftplugin/strudel.lua"

echo "==> installed"

echo
echo "strudel.nvim installed."
echo
echo "  next steps:"
echo "     1. set an API key:   echo 'export OPENROUTER_API_KEY=sk-or-...' >> ~/.zshrc"
echo "     2. nvim any set:     nvim set.strudel"
echo "     3. health check:     :StrudelCheck"
echo "     4. drive a set:      :StrudelMode   /  <leader>ss sends to live"
echo "     5. wezterm look:     merge strudel-nvim/wezterm/wezterm.lua into"
echo "                          ~/.config/wezterm/wezterm.lua"
echo
echo "  keymaps (configurable):"
echo "     <leader>ss  send selection/line/buffer to live"
echo "     <leader>sa  send whole set"
echo "     <leader>sk  insert snippet"
echo "     <leader>ac  chat with glm-5.3-flash"
echo "     <leader>aq  ask about cursor line"
echo "     <leader>at  toggle the ⚡ cost meter"
echo "     <leader>ax  stop in-flight streams"