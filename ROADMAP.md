# ROADMAP — Archive Search Workbench

## Fase 1: MVP (VOLTOOID 2026-05-24)
- [x] Eén medium tegelijk scannen
- [x] Read-only mount met verificatie
- [x] SQLite metadata catalogus
- [x] Recoll full-text indexering
- [x] Zoeken op bestandsnaam/metadata
- [x] Zoeken in documentinhoud
- [x] Rapportage (markdown + CSV)
- [x] Interactief menu
- [x] Stickerlabel workflow

## Fase 2: Verdieping
- [ ] Archive-inhoud indexering (zip/rar/7z uitpakken naar temp, scannen, opruimen)
- [ ] OCR voor gescande PDF's (tesseract)
- [ ] Hashing per bestand (sha256) voor deduplicatie-voorbereiding
- [ ] SMART health check bij registratie (met USB bridge fallback)
- [ ] Encoding-reparatie voor oude Windows-bestandsnamen (cp1252)
- [ ] Meer metadata: e-mail .msg/.eml headers, .pst navigatie

## Fase 3: Multi-media
- [ ] Meerdere media tegelijk via powered USB-hub
- [ ] Queue-systeem voor sequentiële verwerking
- [ ] Automatische detectie + ingest bij aansluiten (udev rule)
- [ ] Scan-resume na crash (per-directory checkpoint)
- [ ] Parallel indexering (Recoll per medium)

## Fase 3.5: Netwerk-USB (USB/IP) — schijf op elke machine indexeren
- [x] USB/IP-kanaal: schijf op andere machine als lokale `/dev/sdX` op de server
- [x] Server-helper `network-usb/usbip_ctl.sh` (ensure-module/list/attach/ports/detach)
- [x] App-endpoints `/api/remote/*` + "Netwerk-USB"-paneel in Beheer
- [x] Eject koppelt automatisch los (`usbip detach`)
- [x] Exporter-setup Windows (usbipd-win) en Linux (usbip/usbipd) + docs
- [ ] Apart de server→NAS kanaal (DSM mist usbip-modules)
- [ ] Optioneel: USB/IP over Tailscale/SSH-tunnel voor buiten-LAN gebruik

## Fase 4: Webinterface
- [ ] Flask webinterface (poort uit 5059-5099 range)
- [ ] Zoeken via browser
- [ ] Thumbnail previews (afbeeldingen, PDF eerste pagina)
- [ ] Resultaat-export (CSV, JSON)
- [ ] Availability dashboard (welk medium online/offline)

## Fase 5: Intelligence
- [ ] AI-samenvatting per document (lokaal LLM of API)
- [ ] Automatische categorisatie/tagging
- [ ] Knowledge graph (triplets uit documenten)
- [ ] Deduplicatie met similarity scoring
- [ ] Afbeeldingvergelijking (perceptual hash)
- [ ] Tijdlijn-visualisatie van documenten

## Fase 6: Integratie
- [ ] de server→NAS Netwerk-USB kanaal (apart kanaal)
- [ ] NAS-export (catalogus syncen naar Synology)
- [ ] Obsidian-integratie (zoekresultaten als markdown notes)
- [ ] YAML export voor kennisbank
- [ ] Disk image support (.img, .vhd, .vmdk mount)
- [ ] Backup-verificatie (vergelijk catalogus met NAS-inhoud)
