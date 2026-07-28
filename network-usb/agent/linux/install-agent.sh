#!/usr/bin/env bash
# install-agent.sh - installeer de ArchSW-loc agent (Linux) als systemd-service.
#
# Lokale agent voor de server Archive Search Workbench. Draai op de machine WAAR DE
# SCHIJF HANGT (exporter). Vereist sudo.
#   ./install-agent.sh [SERVER_URL]     (default http://<server-ip>:5059)
#   ./install-agent.sh --uninstall

set -euo pipefail

PORT="${AGENT_PORT:-5060}"
SERVER_URL="${1:-http://<server-ip>:5059}"
INSTALL_DIR="/opt/archsw-loc-agent"
TOKEN_DIR="/etc/archsw-loc-agent"
TOKEN_FILE="$TOKEN_DIR/token"
UNIT="/etc/systemd/system/archsw-loc-agent.service"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Migratie: oude namen opruimen
cleanup_old() {
    sudo systemctl disable --now archief-agent.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/archief-agent.service
    sudo rm -rf /opt/archief-agent /etc/archief-agent
    sudo systemctl daemon-reload 2>/dev/null || true
}

if [ "${1:-}" = "--uninstall" ]; then
    sudo systemctl disable --now archsw-loc-agent.service 2>/dev/null || true
    sudo rm -f "$UNIT"; sudo systemctl daemon-reload
    sudo rm -rf "$INSTALL_DIR" "$TOKEN_DIR"
    cleanup_old
    echo "ArchSW-loc agent verwijderd."
    exit 0
fi

echo "=== ArchSW-loc agent (Linux) installeren ==="
cleanup_old

if ! command -v usbip >/dev/null 2>&1; then
    echo "[*] usbip installeren..."
    sudo apt-get update -qq && sudo apt-get install -y usbip linux-tools-generic 2>/dev/null || sudo apt-get install -y usbip
fi
sudo modprobe usbip-host 2>/dev/null || sudo modprobe usbip_host 2>/dev/null || true
echo -e "usbip_core\nusbip_host" | sudo tee /etc/modules-load.d/usbip-exporter.conf >/dev/null

sudo mkdir -p "$INSTALL_DIR"
if [ -f "$HERE/archsw-loc-agent.py" ]; then
    sudo cp -f "$HERE/archsw-loc-agent.py" "$INSTALL_DIR/archsw-loc-agent.py"
else
    echo "[*] archsw-loc-agent.py downloaden van de server..."
    sudo curl -fsSL "$SERVER_URL/netwerk-usb/dl/archsw-loc-agent.py" -o "$INSTALL_DIR/archsw-loc-agent.py"
fi

sudo mkdir -p "$TOKEN_DIR"
if [ ! -s "$TOKEN_FILE" ]; then
    TOK="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo -n "$TOK" | sudo tee "$TOKEN_FILE" >/dev/null
    sudo chmod 600 "$TOKEN_FILE"
    echo "[*] Nieuw token gegenereerd."
fi
TOKEN="$(sudo cat "$TOKEN_FILE")"

sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=ArchSW-loc agent (lokale agent voor de server Archive Search Workbench)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=AGENT_PORT=$PORT
Environment=AGENT_TOKEN_FILE=$TOKEN_FILE
ExecStart=/usr/bin/python3 $INSTALL_DIR/archsw-loc-agent.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF

sudo systemctl daemon-reload
sudo systemctl enable --now archsw-loc-agent.service
echo "[*] systemd-service gestart."

if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow from <server-ip> to any port "$PORT" proto tcp 2>/dev/null || true
fi

sleep 2
if curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; then
    echo "[*] Agent draait."
else
    echo "[!] Agent nog niet bereikbaar - check: journalctl -u archsw-loc-agent"
fi
if curl -fsS -X POST "$SERVER_URL/api/remote/register-agent" \
    -H "Content-Type: application/json" \
    -d "{\"port\": $PORT, \"token\": \"$TOKEN\"}" >/dev/null 2>&1; then
    echo "[*] Aangemeld bij de app."
else
    echo "[!] Kon niet aanmelden bij $SERVER_URL. Token (voor handmatige registratie):"
    echo "    $TOKEN"
fi

echo "Klaar. De app kan nu bestanden lokaal lezen en (indien nodig) schijven via USB/IP delen."
