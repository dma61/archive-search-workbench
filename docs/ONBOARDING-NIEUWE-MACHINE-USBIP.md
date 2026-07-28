# Onboarding nieuwe machine — Netwerk-USB exporter (USB/IP)

> **Plak-klaar voor Mantis** (koppel aan het onderwerp "onboarding nieuwe machine", #527).
> Bewaard bij de app op de server: `~/archive-search-workbench/docs/` en downloadbaar
> vanaf de server. Zo hoef je bij een nieuwe machine niets te zoeken.

## In één zin

Wil je op een nieuwe machine een archiefschijf kunnen aanhangen en via de Archive Search
Workbench indexeren? Richt die machine in als **USB/IP-exporter** — alle scripts en commando's
staan op één pagina:

**➡️ http://<server-ip>:5059/netwerk-usb**

## Windows (kopieer-plak, PowerShell als Administrator)

```powershell
irm http://<server-ip>:5059/netwerk-usb/dl/install-usbipd.ps1 -OutFile $env:TEMP\install-usbipd.ps1
irm http://<server-ip>:5059/netwerk-usb/dl/export-disk.ps1 -OutFile $env:TEMP\export-disk.ps1
& $env:TEMP\install-usbipd.ps1
# schijf aansluiten, dan:
& $env:TEMP\export-disk.ps1 -List # zoek de busid
& $env:TEMP\export-disk.ps1 -BusId 2-4 # deel die schijf (busid invullen)
```

## Linux

```bash
curl -fsSL http://<server-ip>:5059/netwerk-usb/dl/setup-exporter.sh -o setup-exporter.sh
curl -fsSL http://<server-ip>:5059/netwerk-usb/dl/bind-disk.sh -o bind-disk.sh
bash setup-exporter.sh
bash bind-disk.sh # toon busid
bash bind-disk.sh 1-4 # deel die schijf
```

## Daarna

Workbench → tab **Beheer** → paneel **Netwerk-USB** → machine kiezen → **Koppel aan server**.
De schijf verschijnt bij *Aangesloten schijven* om te labelen, mounten en scannen.
Uitwerpen verbreekt automatisch de netwerk-koppeling.

## Waar staat wat

- **Bij de app op Ubuntu (de server):** `~/archive-search-workbench/network-usb/`
 (`windows/*.ps1`, `linux/*.sh`, `usbip_ctl.sh`, `README.md`).
- **In git:** repo `indexer-exhdd-archive-search-workbench`, map `network-usb/`.
- **Via de server te downloaden:** `http://<server-ip>:5059/netwerk-usb`.

## Beveiliging

USB/IP-poort 3240 is onversleuteld/ongeauthenticeerd → alleen op vertrouwd LAN; beperk de
firewall tot de server, of tunnel via Tailscale/SSH. De schijf wordt altijd read-only gemount.
De Synology NAS kan (nog) niet als exporter — zie `MANTIS-NAS-USBIP-KANAAL.md`.
