#!/usr/bin/env bash
# Install Omarkets as a standalone Omarchy app (Walker + webapp window).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarkets"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

echo "Installing Omarkets (Omarchy)…"
echo "  source: $ROOT"

chmod +x "$ROOT/omarkets" "$ROOT/scripts/install-omarchy.sh" "$ROOT/scripts/uninstall-omarchy.sh"

# App payload under ~/.local/share/omarkets (stable path, survives moving the repo)
mkdir -p "$DATA_DIR/static/css" "$DATA_DIR/static/js" "$DATA_DIR/data"
install -m 0755 "$ROOT/server.py" "$DATA_DIR/server.py"
install -m 0755 "$ROOT/omarkets" "$DATA_DIR/omarkets"
install -m 0644 "$ROOT/static/index.html" "$DATA_DIR/static/index.html"
install -m 0644 "$ROOT/static/css/app.css" "$DATA_DIR/static/css/app.css"
install -m 0644 "$ROOT/static/js/app.js" "$DATA_DIR/static/js/app.js"
install -m 0644 "$ROOT/data/watchlist.default.json" "$DATA_DIR/data/watchlist.default.json"
install -m 0644 "$ROOT/data/tickers.json" "$DATA_DIR/data/tickers.json"

mkdir -p "$BIN_DIR"
ln -sfn "$DATA_DIR/omarkets" "$BIN_DIR/omarkets"

mkdir -p "$ICON_DIR"
install -m 0644 "$ROOT/assets/omarkets.svg" "$ICON_DIR/omarkets.svg"

mkdir -p "$APP_DIR"
install -m 0644 "$ROOT/packaging/omarkets.desktop" "$APP_DIR/omarkets.desktop"
# drop old symlink desktop if present
rm -f "$APP_DIR/omarkets.desktop.bak" 2>/dev/null || true

update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

echo ""
echo "Omarchy install complete."
echo "  Launch:   Walker → Omarkets"
echo "  Or:       omarkets"
echo "  URL:      http://127.0.0.1:1987/"
echo "  Data:     $DATA_DIR"
echo "  Watchlist:~/.local/state/omarkets/watchlist.json"
echo "  Uninstall:$ROOT/scripts/uninstall-omarchy.sh"
