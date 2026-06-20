#!/bin/bash
# install.sh — Deploy btdig-torznab and ext-to-torznab bridges
# Usage: sudo ./install.sh [--port 5555] [--host 0.0.0.0]
#        sudo ./install.sh --ext-to (installs ext.to bridge only)
#        sudo ./install.sh --all   (installs both bridges)

set -euo pipefail

PORT="${BTDIG_PORT:-5555}"
HOST="${BTDIG_HOST:-0.0.0.0}"
INSTALL_DIR="/opt/btdig-torznab"
SCRIPT="btdig_torznab.py"
SERVICE="btdig-torznab.service"
INSTALL_EXT_TO=false

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --ext-to) INSTALL_EXT_TO=true ;;
        --all) INSTALL_EXT_TO=true ;;
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

# --- ext.to bridge (optional) ---
if $INSTALL_EXT_TO; then
    EXT_PORT="${EXT_TO_PORT:-5556}"
    EXT_SCRIPT="ext_to_torznab.py"
    EXT_SERVICE="ext-to-torznab.service"

    echo "[install] Installing ext.to bridge..."

    # Install Python dependencies
    pip3 install --quiet requests beautifulsoup4 2>/dev/null || \
        apt-get install --yes python3-requests python3-bs4

    # Copy script
    cp "$(dirname "$0")/$EXT_SCRIPT" "$INSTALL_DIR/$EXT_SCRIPT"
    chmod 755 "$INSTALL_DIR/$EXT_SCRIPT"

    # Install systemd service
    cat > /etc/systemd/system/$EXT_SERVICE <<EOSERVICE
[Unit]
Description=ext.to Torznab bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=btdig-torznab
Group=btdig-torznab
ExecStart=/usr/bin/env python3 $INSTALL_DIR/$EXT_SCRIPT
Restart=on-failure
RestartSec=10
WorkingDirectory=$INSTALL_DIR
AmbientCapabilities=
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
Environment=EXT_TO_HOST=$HOST
Environment=EXT_TO_PORT=$EXT_PORT
Environment=LOG_LEVEL=INFO
Environment=INCLUDE_ADULT=true

[Install]
WantedBy=multi-user.target
EOSERVICE

    systemctl daemon-reload
    systemctl enable "$EXT_SERVICE"
    systemctl restart "$EXT_SERVICE"

    sleep 2
    if systemctl is-active --quiet "$EXT_SERVICE"; then
        echo "[install] ✓ ext-to-torznab is running on port $EXT_PORT"
        echo "[install] Add to *arr as: http://<host>:$EXT_PORT/torznab/api"
        echo "[install] Note: ext.to requires FlareSolverr. See README for Docker setup."
    else
        echo "[install] ✗ ext-to-torznab failed to start. Check: journalctl -u $EXT_SERVICE"
    fi
fi
