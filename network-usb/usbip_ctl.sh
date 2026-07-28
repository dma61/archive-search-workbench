#!/usr/bin/env bash
# usbip_ctl.sh — USB/IP besturingslaag (importer-zijde) voor de Archive Search Workbench.
#
# Dit script draait op de server (), de "importer". Het voert uitsluitend de
# geprivilegieerde usbip-verbs uit; het parsen van de output gebeurt in web_app.py.
# Zo blijft de logica testbaar en houden we de escaping in Python.
#
# Gebruik:
# usbip_ctl.sh ensure-module # laad vhci-hcd indien nodig
# usbip_ctl.sh list <host> # toon exporteerbare devices op <host> (ruwe usbip-output)
# usbip_ctl.sh attach <host> <busid> # attach remote device
# usbip_ctl.sh ports # toon actieve remote attachments (ruwe usbip-output)
# usbip_ctl.sh detach <port> # detach op poortnummer (bv. 00)
#
# Exitcodes: 0 = ok, 1 = gebruiksfout, 2 = usbip-fout. Fouten gaan naar stderr;
# nette (ruwe) resultaten naar stdout. GEEN silent failure: elke fout wordt gemeld.

set -uo pipefail

USBIP="${USBIP_BIN:-/usr/bin/usbip}"
MODPROBE="${MODPROBE_BIN:-/sbin/modprobe}"

die() { echo "usbip_ctl: $*" >&2; exit "${2:-2}"; }

need_bin() {
 [ -x "$USBIP" ] || command -v usbip >/dev/null 2>&1 || \
 die "usbip niet gevonden (verwacht op $USBIP) — installeer 'usbip' pakket" 2
 command -v "$USBIP" >/dev/null 2>&1 || USBIP="$(command -v usbip)"
}

ensure_module() {
 # vhci-hcd is de importer/client-module. Idempotent.
 if [ -d /sys/devices/platform/vhci_hcd.0 ] || lsmod 2>/dev/null | grep -q '^vhci_hcd'; then
 echo "vhci-hcd already loaded"
 return 0
 fi
 if command -v "$MODPROBE" >/dev/null 2>&1; then
 sudo -n "$MODPROBE" vhci-hcd 2>/dev/null || sudo -n modprobe vhci-hcd \
 || die "modprobe vhci-hcd mislukt (sudo/rechten?)" 2
 else
 sudo -n modprobe vhci-hcd || die "modprobe vhci-hcd mislukt" 2
 fi
 echo "vhci-hcd loaded"
}

cmd="${1:-}"
[ -n "$cmd" ] || die "geen commando opgegeven" 1
shift || true

need_bin

case "$cmd" in
 ensure-module)
 ensure_module
 ;;
 list)
 host="${1:-}"
 [ -n "$host" ] || die "list vereist <host>" 1
 # 'usbip list -r' contacteert de remote usbipd; lokaal geen root nodig.
 out="$($USBIP list -r "$host" 2>&1)" || die "usbip list -r $host mislukt: $out" 2
 printf '%s\n' "$out"
 ;;
 attach)
 host="${1:-}"; busid="${2:-}"
 [ -n "$host" ] && [ -n "$busid" ] || die "attach vereist <host> <busid>" 1
 ensure_module >/dev/null
 out="$(sudo -n "$USBIP" attach -r "$host" -b "$busid" 2>&1)" \
 || die "usbip attach -r $host -b $busid mislukt: $out" 2
 printf '%s\n' "${out:-attached}"
 ;;
 ports)
 ensure_module >/dev/null
 # 'usbip port' toont geimporteerde (attached) devices.
 out="$(sudo -n "$USBIP" port 2>&1)" || die "usbip port mislukt: $out" 2
 printf '%s\n' "$out"
 ;;
 detach)
 port="${1:-}"
 [ -n "$port" ] || die "detach vereist <port>" 1
 out="$(sudo -n "$USBIP" detach -p "$port" 2>&1)" \
 || die "usbip detach -p $port mislukt: $out" 2
 printf '%s\n' "${out:-detached}"
 ;;
 *)
 die "onbekend commando: $cmd" 1
 ;;
esac
