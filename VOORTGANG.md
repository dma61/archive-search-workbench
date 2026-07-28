# VOORTGANG — Archive Search Workbench

## Status: MVP werkend (2026-05-24)

Eerste volledige end-to-end doorloop geslaagd op de server.

## Sessie 2026-07-24 — Netwerk-USB (USB/IP)

Toegevoegd: schijf op een **andere machine** kunnen indexeren/uitlezen via USB/IP.
De server is de importer; de machine waar de schijf hangt is de exporter.

### Uitgevoerd
- [x] Ontwerpkeuze: USB/IP (blok-niveau) — schijf verschijnt op de server als lokale `/dev/sdX`,
 zodat de bestaande mount/scan/index-pijplijn ongewijzigd werkt (ook nieuw indexeren op afstand)
- [x] `network-usb/usbip_ctl.sh` — dunne, geprivilegieerde usbip-wrapper (ensure-module/list/attach/ports/detach)
- [x] `config/remote_hosts.yaml` — exporter-hosts ()
- [x] `web_app.py` — endpoints `/api/remote/{hosts,devices,attach,detach,ports}`,
 state `data/remote_usbip_state.json`, en "Netwerk-USB"-paneel in Beheer (met i18n nl/en)
- [x] Eject-integratie: `_maybe_detach_remote` in `api_eject` → `usbip detach` bij uitwerpen
- [x] Exporter-setup + docs: Windows (`usbipd-win`) en Linux (`usbip`/`usbipd`), `network-usb/README.md`
- [x] Mantis-bevinding voor toekomstig de server→NAS kanaal (`docs/MANTIS-NAS-USBIP-KANAAL.md`)

### Ontwerpbeslissingen
- **Importer = de server**: heeft al usbip-tools + `vhci-hcd`-module + sudo NOPASSWD
- **Helper dun houden**: alleen privileged usbip-verbs; parsen van output in Python (escaping-controle)
- **Auto-surfacing**: een ge-attachte USB/IP-schijf rapporteert `tran=usb` in lsblk → verschijnt
 vanzelf in `/api/disks`; geen wijziging aan de scan/mount-logica nodig
- **NAS uitgesteld**: DSM mist usbip-kernelmodules → apart kanaal als Mantis-bevinding

### Aandachtspunten / open
- Fysieke end-to-end test vereist `usbipd-win`-install (Administrator) op een Windows-machine
- USB/IP poort 3240 is onversleuteld/ongeauthenticeerd → LAN-only; buiten-LAN via Tailscale/SSH-tunnel

## Sessie 2026-05-24

### Uitgevoerd
- [x] Setup.sh gemaakt en uitgevoerd (alle 8 tools OK, venv OK)
- [x] Database schema v1 aangemaakt (physical_media, scans, files + indexes)
- [x] detect_disks.sh — toont aangesloten media met details
- [x] mount_readonly.sh — hermont FREECOM van rw naar ro, schrijfbeveiliging geverifieerd
- [x] scan_metadata.py — 73 bestanden, 0 fouten, 94.2 GB verwerkt
- [x] extract_metadata.py — PDF, Office, EXIF, datum-prioriteitssysteem
- [x] build_recoll_index.sh — full-text index gebouwd, 8 resultaten voor "Dell"
- [x] search_filename.py — SQLite zoeken werkt
- [x] search_content.sh — Recoll full-text zoeken werkt
- [x] report.py — rapport.md + 10 CSV exports gegenereerd
- [x] menu.sh — interactief menu met alle 14 opties
- [x] INSTRUCTIE.md met ontdekte problemen

### Ontdekte problemen (opgelost)
1. **Bash arithmetic `((x++))` met `set -e`** — geeft exit 1 bij waarde 0. Opgelost: `$((x+1))` syntax.
2. **Mount permissies devmon** — devmon mount NTFS met uid=0,gid=0 → onleesbaar voor <nas-gebruiker>. Opgelost: remount via ntfs-3g met uid/gid van user.
3. **SMART via USB bridge** — bridge 0x07ab:0xfcfe niet herkend. Niet-blokkerend, SMART is optioneel.
4. **Heredoc met single-quotes in Python** — SSH heredoc faalt bij complexe Python. Opgelost: lokaal schrijven + scp.
5. **blockdev zonder sudo** — grootte-weergave 0GB in detect_disks.sh. Cosmetisch, niet gefixt.

### Ontwerpbeslissingen
- **Checkpointing per directory** (niet per bestand) — balans tussen granulariteit en overhead
- **Lock via PID-file** — eenvoudig, stale lock detectie via `os.kill(pid, 0)`
- **Recoll index per medium** — schaalt beter, kan individueel herbouwd worden
- **Schema versioning in DB** — MIGRATIONS dict, makkelijk uit te breiden
- **Archive label als directory-naam** onder /mnt/archive-ingest/ — voorspelbaar, geen register nodig

### Eerste testmedium
- **Label:** ARCHIVE-DISK-001
- **Fysiek:** Freecom Classic 250GB USB HDD (Maxtor 5DLAT80, IDE via USB bridge)
- **UUID:** 08F42991F42981D4
- **Inhoud:** Oude PINGS (disk images Dell Dimensions 3000, Latitude D600) uit 2003-2010, VMware conversie Toshiba 120GB
- **73 bestanden, 94.2 GB** — voornamelijk disk images (.000/.001 splits), 1 grote .vmdk (77GB)

## Volgende stappen
- [ ] Sticker plakken op FREECOM en bevestigen in DB
- [ ] Tweede medium testen (USB-stick of SD-kaart met meer diverse bestanden)
- [ ] Archive-inhoud indexering (zip/rar/7z doorzoeken)
- [ ] OCR toevoegen voor gescande PDF's
- [ ] Webinterface (Flask, poort TBD uit 5059-5099 range)
