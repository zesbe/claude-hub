#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Claude Hub — one-shot installer
#
#   curl -fsSL https://raw.githubusercontent.com/zesbe/claude-hub/main/install.sh | bash
#
# Installs:
#   • the hub server  → ~/.claude-hub/server.py
#   • the launcher     → ~/.local/bin/claude-deep
#   • a systemd --user service (auto-start + auto-restart, survives reboot)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/zesbe/claude-hub/main"
HUB_DIR="$HOME/.claude-hub"
BIN_DIR="$HOME/.local/bin"
SVC_DIR="$HOME/.config/systemd/user"
PORT=8765

c_g(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_y(){ printf '\033[33m%s\033[0m\n' "$*"; }
c_r(){ printf '\033[31m%s\033[0m\n' "$*"; }
step(){ printf '\033[36m▶ %s\033[0m\n' "$*"; }

# ── 0. preflight ─────────────────────────────────────────────────────────────
step "Checking prerequisites"
command -v python3 >/dev/null || { c_r "python3 not found — install it first"; exit 1; }
command -v sqlite3 >/dev/null || c_y "  (optional) sqlite3 CLI missing — claude-deep menu needs it; install later if you want the launcher menu"
PYV=$(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')
c_g "  python3 $PYV ✓"

mkdir -p "$HUB_DIR" "$BIN_DIR" "$SVC_DIR"

# ── 1. fetch files (or copy if run from a clone) ─────────────────────────────
fetch(){ # fetch <name> <dest>
  local name="$1" dest="$2"
  if [ -f "$(dirname "$0")/$name" ]; then
    cp "$(dirname "$0")/$name" "$dest"
  else
    curl -fsSL "$REPO_RAW/$name" -o "$dest"
  fi
}
step "Installing hub server"
fetch server.py     "$HUB_DIR/server.py"
fetch requirements.txt "$HUB_DIR/requirements.txt"
fetch claude-deep   "$BIN_DIR/claude-deep"
chmod +x "$BIN_DIR/claude-deep"
c_g "  files installed ✓"

# ── 2. python deps (prefer a venv to avoid PEP-668 issues) ───────────────────
step "Installing Python dependencies"
if python3 -c 'import fastapi, uvicorn, httpx, pydantic' 2>/dev/null; then
  c_g "  deps already present ✓"
else
  if pip3 install --user -r "$HUB_DIR/requirements.txt" 2>/dev/null; then
    c_g "  installed with pip --user ✓"
  else
    c_y "  pip --user blocked, creating venv at $HUB_DIR/venv"
    python3 -m venv "$HUB_DIR/venv"
    "$HUB_DIR/venv/bin/pip" install -q -r "$HUB_DIR/requirements.txt"
    # point the service at the venv python
    PYBIN="$HUB_DIR/venv/bin/python"
    c_g "  venv ready ✓"
  fi
fi
PYBIN="${PYBIN:-/usr/bin/env python3}"

# ── 3. systemd --user service ────────────────────────────────────────────────
step "Setting up systemd service (auto-start + auto-restart)"
cat > "$SVC_DIR/claude-hub.service" <<EOF
[Unit]
Description=Claude Hub — multi-provider router + API gateway for Claude Code
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$HUB_DIR
ExecStart=$PYBIN -m uvicorn server:app --host 127.0.0.1 --port $PORT --app-dir $HUB_DIR
Restart=always
RestartSec=3
TimeoutStartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

if systemctl --user daemon-reload 2>/dev/null; then
  systemctl --user enable claude-hub.service >/dev/null 2>&1 || true
  systemctl --user restart claude-hub.service 2>/dev/null || systemctl --user start claude-hub.service
  # keep the service alive after logout / across reboots
  loginctl enable-linger "$USER" >/dev/null 2>&1 || sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || \
    c_y "  could not enable lingering automatically — run: sudo loginctl enable-linger $USER"
  c_g "  service enabled + started ✓"
  USED_SYSTEMD=1
else
  c_y "  systemd --user unavailable — starting with nohup (won't survive reboot)"
  pkill -f "uvicorn server:app.*$PORT" 2>/dev/null || true
  sleep 1
  ( cd "$HUB_DIR" && setsid nohup $PYBIN -m uvicorn server:app --host 127.0.0.1 --port $PORT --app-dir "$HUB_DIR" >"$HUB_DIR/server.log" 2>&1 < /dev/null & )
  USED_SYSTEMD=0
fi

# ── 4. health check ──────────────────────────────────────────────────────────
step "Verifying"
sleep 3
if curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
  c_g "  hub is up on http://localhost:$PORT ✓"
else
  c_r "  hub did not respond — check logs:"
  [ "${USED_SYSTEMD:-0}" = 1 ] && echo "    journalctl --user -u claude-hub -n 50" || echo "    tail -50 $HUB_DIR/server.log"
  exit 1
fi

# ── done ─────────────────────────────────────────────────────────────────────
echo
c_g "════════════════════════════════════════════════════════"
c_g " Claude Hub installed 🎉"
c_g "════════════════════════════════════════════════════════"
echo
echo "  Dashboard : http://localhost:$PORT/"
echo "  Launcher  : claude-deep            (menu pilih provider)"
echo "              claude-deep 2          (langsung provider #2)"
echo
echo "  Next steps:"
echo "   1. Buka http://localhost:$PORT/ di browser"
echo "   2. + Provider Baru → isi base URL + token, Discover model, Apply"
echo "   3. Jalankan: claude-<nama>   (mis. claude-deepseek)"
echo
echo "  API Gateway (opsional, buat tool luar): tombol 🔌 API di dashboard"
echo
[ "$(command -v sqlite3 || true)" ] || c_y "  Reminder: install sqlite3 buat fitur menu 'claude-deep'."
