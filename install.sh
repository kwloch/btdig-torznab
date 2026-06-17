#!/bin/bash
# install.sh — Deploy btdig-torznab bridge on a Debian/Alpine system
# Usage: sudo ./install.sh [--port 5555] [--host 0.0.0.0]

set -euo pipefail

PORT="${BTDIG_PORT:-5555}"
HOST="${BTDIG_HOST:-0.0.0.0}"
INSTALL_DIR="/opt/btdig-torznab"
SCRIPT="btdig_torznab.py"
SERVICE="btdig-torznab.service"

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

# --- Create user ---
if ! id btdig-torznab &>/dev/null; then
    echo "[install] Creating btdig-torznab user..."
    useradd --system --group --no-create-home btdig-torznab
fi

# --- Copy files ---
echo "[install] Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$(dirname "$0")/$SCRIPT" "$INSTALL_DIR/$SCRIPT"
chmod 755 "$INSTALL_DIR/$SCRIPT"
chown -R btdig-torznab:btdig-torznab "$INSTALL_DIR"

# --- Install systemd service ---
echo "[install] Installing systemd service..."
cp "$(dirname "$0")/$SERVICE" /etc/systemd/system/$SERVICE
sed -i "s/5555/$PORT/" /etc/systemd/system/$SERVICE

systemctl daemon-reload
systemctl enable btdig-torznab
systemctl restart btdig-torznab

# --- Verify ---
sleep 2
if systemctl is-active --quiet btdig-torznab; then
    echo "[install] ✓ btdig-torznab is running on port $PORT"
    echo "[install] Add to *arr as: http://<host>:$PORT/torznab/api"
else
    echo "[install] ✗ Service failed to start. Check: journalctl -u btdig-torznab"
    exit 1
fi
