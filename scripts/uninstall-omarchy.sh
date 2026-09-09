#!/usr/bin/env bash
# Remove Omarkets Omarchy launcher / install (keeps watchlist state).
set -euo pipefail

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarkets"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/omarkets.svg"
PIDFILE="$DATA_DIR/omarkets.pid"
# also stop a server running from the old dev path
DEV_PID="/home/omnium/dev/omarkets/omarkets.pid"

echo "Uninstalling Omarkets (Omarchy)…"

stop_pidfile() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  local pid
  pid="$(cat "$f" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$f"
}

stop_pidfile "$PIDFILE"
stop_pidfile "$DEV_PID"
fuser -k 1987/tcp 2>/dev/null || true

rm -f "$BIN_DIR/omarkets"
rm -f "$APP_DIR/omarkets.desktop"
rm -f "$ICON"
rm -rf "$DATA_DIR"

update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

echo "Removed launcher and app files."
echo "Watchlist kept at ~/.local/state/omarkets/ (delete manually if you want)."
