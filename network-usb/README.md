# Netwerk-USB (USB/IP) — schijf op een andere machine indexeren

Met dit onderdeel kun je een archiefschijf aan **elke machine in je netwerk** hangen
en hem toch via de Archive Search Workbench op de server (`<server-ip>`) uitlezen
én indexeren. De USB-poort van die machine wordt als het ware een poort van de server.

## Hoe het werkt

```
 Schijf hangt hier Server (brein)
 ┌─────────────────────┐ USB/IP over ┌────────────────────────┐
 │ Exporter-machine │ TCP poort 3240 │ de server (importer)│
 │ usbipd → bind │ ───────────────► │ usbip attach → /dev/sdX│
 │ (Windows of Linux) │ │ → read-only mount →scan│
 └─────────────────────┘ └────────────────────────┘
```

- **Exporter** = de machine waar de schijf fysiek aan hangt. Die "bindt" de USB-schijf.
- **Importer** = de server. Die "attacht" de schijf; hij verschijnt daar als een echte
 lokale `/dev/sdX`, waardoor de bestaande read-only mount / scan / index-pijplijn
 ongewijzigd werkt.

De server is al voorbereid (usbip-tools + `vhci-hcd`-module aanwezig). Je hoeft alleen
de **exporter-machine** in te richten.

## Stap 1 — Exporter inrichten (eenmalig per machine)

### Windows (Desktop , ws-5)
Draai in **PowerShell als Administrator**, in deze map:

```powershell
.\windows\install-usbipd.ps1 # installeert usbipd-win (opent ook firewall 3240)
```

### Linux (bv. <andere-machine>)
```bash
./linux/setup-exporter.sh # installeert usbip, laadt modules, start usbipd
```

## Stap 2 — Schijf delen (elke keer dat je een schijf wilt indexeren)

Sluit de schijf aan op de exporter-machine en zoek de **busid**:

### Windows
```powershell
.\windows\export-disk.ps1 -List # toon apparaten + busid
.\windows\export-disk.ps1 -BusId 2-4 # deel de schijf (Administrator)
```

### Linux
```bash
./linux/bind-disk.sh # toon apparaten + busid
./linux/bind-disk.sh 1-4 # deel de schijf
```

## Stap 3 — Koppelen vanaf de server

Open de workbench: <http://<server-ip>:5059/> → tab **Beheer** →
paneel **"Netwerk-USB — schijf op andere machine"**:

1. Kies de machine (🟢 = bereikbaar op poort 3240).
2. Klik **Toon schijven op deze machine**.
3. Klik bij de juiste schijf op **Koppel aan server**.
4. De schijf verschijnt daarna bij **Aangesloten schijven** — label/mount/scan zoals normaal.

## Stap 4 — Loskoppelen

Werp de schijf uit via de normale **Uitwerpen**-knop bij de schijfkaart: dat unmount de
schijf én verbreekt automatisch de netwerk-koppeling (`usbip detach`). Je kunt een koppeling
ook handmatig verbreken in het Netwerk-USB-paneel onder **Actieve netwerk-koppelingen**.
Op de exporter-machine geef je de schijf weer vrij met `export-disk.ps1 -Unbind` (Windows)
of `bind-disk.sh --unbind` (Linux).

## Beveiliging (belangrijk)

- **USB/IP verkeer op poort 3240 is onversleuteld en ongeauthenticeerd.** Gebruik dit
 alleen op een **vertrouwd LAN**. Beperk firewall-poort 3240 tot het IP van de server
 (`<server-ip>`).
- Wil je een schijf van **buiten het LAN** doorgeven? Tunnel USB/IP dan over **Tailscale**
 of een **SSH-tunnel** in plaats van poort 3240 direct open te zetten.
- **Read-only blijft gegarandeerd:** de server mount de schijf altijd read-only
 (`mount_readonly.sh`, met schrijfbeveiliging-verificatie), ook al zit het block-device
 fysiek op een andere machine.
- Zolang een schijf gedeeld (bound) is, is hij **niet** als gewone schijf op de
 exporter-machine beschikbaar. Voor read-only archiefschijven is dat prima.

## NAS

De Synology NAS (``) valt nu buiten scope: DSM mist standaard de usbip-kernelmodules.
Zie [`../docs/MANTIS-NAS-USBIP-KANAAL.md`](../docs/MANTIS-NAS-USBIP-KANAAL.md) voor het
plan voor een apart NAS-kanaal.

## Bestanden

| Bestand | Rol |
|---|---|
| `usbip_ctl.sh` | Server-helper (importer): `ensure-module`, `list`, `attach`, `ports`, `detach`. Aangeroepen door `web_app.py`. |
| `windows/install-usbipd.ps1` | usbipd-win installeren (Administrator). |
| `windows/export-disk.ps1` | USB-schijf delen/vrijgeven op Windows. |
| `linux/setup-exporter.sh` | Linux-machine als exporter inrichten. |
| `linux/bind-disk.sh` | USB-schijf delen/vrijgeven op Linux. |
