#!/usr/bin/env bash
# setup-exporter.sh — richt een Linux-machine in als USB/IP exporter.
#
# Draai dit op de machine WAAR DE SCHIJF AAN HANGT (niet op de server).
# Installeert usbip, laadt de host-module en start de usbipd-daemon zodat de
# de server () schijven kan importeren.
#
# Vereist sudo. Getest op Ubuntu/Debian. Onderdeel van Archive Search Workbench.

set -euo pipefail

echo "=== USB/IP exporter-setup (Linux) ==="

# 1. Pakketten
if command -v apt-get >/dev/null 2>&1; then
 KVER="$(uname -r)"
 echo "[1/4] usbip installeren (apt)..."
 sudo apt-get update -qq
 # 'usbip' zit in linux-tools; installeer ook de kernel-specifieke tools indien beschikbaar.
 sudo apt-get install -y usbip "linux-tools-${KVER}" linux-tools-generic 2>/dev/null \
 || sudo apt-get install -y usbip
else
 echo "Geen apt gevonden — installeer het 'usbip'-pakket via je eigen packagemanager." >&2
fi

# 2. Kernelmodules (exporter-zijde = usbip-host / usbip_host)
echo "[2/4] Kernelmodules laden..."
sudo modprobe usbip-core 2>/dev/null || sudo modprobe usbip_core 2>/dev/null || true
sudo modprobe usbip-host 2>/dev/null || sudo modprobe usbip_host 2>/dev/null || true
# Autoload bij boot
echo -e "usbip_core\nusbip_host" | sudo tee /etc/modules-load.d/usbip-exporter.conf >/dev/null

# 3. usbipd-daemon starten (deelt de USB-bus op poort 3240)
echo "[3/4] usbipd-daemon starten..."
if command -v usbipd >/dev/null 2>&1; then
 # Draai als achtergronddaemon; voor permanent gebruik: maak een systemd-unit.
 if ! pgrep -x usbipd >/dev/null 2>&1; then
 sudo usbipd -D
 echo " usbipd gestart (achtergrond)."
 else
 echo " usbipd draait al."
 fi
else
 echo " LET OP: 'usbipd' binary niet gevonden. Controleer de usbip-installatie." >&2
fi

# 4. Firewall-hint
echo "[4/4] Firewall: sta TCP-poort 3240 toe vanaf de server (<server-ip>)."
if command -v ufw >/dev/null 2>&1; then
 echo " Bijv.: sudo ufw allow from <server-ip> to any port 3240 proto tcp"
fi

echo ""
echo "Klaar. Bind vervolgens de schijf met: ./bind-disk.sh"
