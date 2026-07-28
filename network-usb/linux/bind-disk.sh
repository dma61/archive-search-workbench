#!/usr/bin/env bash
# bind-disk.sh — deel (bind) een lokale USB-schijf via USB/IP.
#
# Draai op de exporter-machine (waar de schijf aan hangt). Zonder argument toont
# het de lokale USB-apparaten met busid; met een busid bindt het dat apparaat.
#
#   ./bind-disk.sh              # toon apparaten
#   ./bind-disk.sh 1-4          # deel busid 1-4
#   ./bind-disk.sh --unbind 1-4 # stop met delen
#
# Onderdeel van Archive Search Workbench — Netwerk-USB (USB/IP).

set -euo pipefail

USBIP="$(command -v usbip || echo /usr/bin/usbip)"
[ -x "$USBIP" ] || { echo "usbip niet gevonden — draai eerst setup-exporter.sh" >&2; exit 1; }

if [ "${1:-}" = "--unbind" ]; then
    busid="${2:-}"
    [ -n "$busid" ] || { echo "gebruik: $0 --unbind <busid>" >&2; exit 1; }
    sudo "$USBIP" unbind -b "$busid"
    echo "busid $busid niet langer gedeeld."
    exit 0
fi

if [ -z "${1:-}" ]; then
    echo "=== Lokale USB-apparaten (busid staat vooraan) ==="
    sudo "$USBIP" list -l
    echo ""
    echo "Deel een schijf met:  $0 <busid>"
    exit 0
fi

busid="$1"
echo "Schijf met busid $busid delen via USB/IP..."
sudo "$USBIP" bind -b "$busid"
echo "Gedeeld. Ga op de server naar het Netwerk-USB-paneel en koppel deze machine."
echo "Stoppen met delen:  $0 --unbind $busid"
