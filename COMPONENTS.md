# COMPONENTS — Archive Search Workbench

## Shell Scripts

### detect_disks.sh
- **Functie:** Toont alle aangesloten block devices met UUID, label, model, serial, mountstatus, geschat mediatype
- **Input:** geen (leest /sys, lsblk, blkid, udevadm)
- **Output:** Terminal output (tekst)
- **Fouten:** Faalt graceful als device-info niet leesbaar
- **Status:** stabiel

### mount_readonly.sh
- **Functie:** Mount extern medium read-only, hermont bestaande rw-mounts, verifieert schrijfbeveiliging
- **Input:** device pad + archive label (CLI args)
- **Output:** Mount onder /mnt/archive-ingest/LABEL/, logbestand
- **Fouten:** Exit 1 bij ongeldig device, niet-read-only mount, of mislukte schrijftest
- **Status:** stabiel

### build_recoll_index.sh
- **Functie:** Genereert recoll.conf per medium, bouwt full-text index met nice/ionice
- **Input:** optioneel archive label (CLI arg), anders alle gemounte media
- **Output:** Recoll index in recoll-indexes/LABEL/
- **Fouten:** Exit 1 als bronmap niet bestaat
- **Status:** stabiel

### search_content.sh
- **Functie:** Wrapper rond recoll query, zoekt over alle of specifiek medium
- **Input:** zoekterm (CLI arg), optioneel --label en --limit
- **Output:** Terminal: bestandspaden + scores
- **Fouten:** Graceful als geen index gevonden
- **Status:** stabiel

## Netwerk-USB (USB/IP)

### network-usb/usbip_ctl.sh
- **Functie:** Dunne, geprivilegieerde wrapper rond `usbip` op de importer (de server). Verbs:
  `ensure-module` (laadt `vhci-hcd`), `list <host>`, `attach <host> <busid>`, `ports`, `detach <port>`
- **Input:** subcommando + args (CLI); gebruikt `sudo -n` (NOPASSWD)
- **Output:** ruwe usbip-tekst op stdout (parsen gebeurt in `web_app.py`)
- **Fouten:** exit 1 = gebruiksfout, 2 = usbip-fout; foutmelding naar stderr, geen silent failure
- **Status:** nieuw

### web_app.py — /api/remote/* endpoints
- **Functie:** Netwerk-USB kanaal: hosts tonen, exporteerbare devices opvragen, attach/detach,
  actieve poorten. Een ge-attachte schijf verschijnt daarna in de bestaande `/api/disks`-flow
- **Input:** `config/remote_hosts.yaml` (hosts); POST-body `{host, busid}` / `{port}`
- **Output:** JSON; state in `data/remote_usbip_state.json` (`port↔host↔busid↔dev`)
- **Fouten:** bereikbaarheids-check op poort 3240; fouten als `{...,"error":...}` (UI toont ze),
  gelogd in `logs/network-usb.log`
- **Integratie:** `_maybe_detach_remote()` in `api_eject` verbreekt de USB/IP-koppeling bij uitwerpen
- **Status:** nieuw

### network-usb/windows/*.ps1 en network-usb/linux/*.sh
- **Functie:** Exporter-setup en schijf delen (bind) op de machine waar de schijf hangt
- **Input:** busid (CLI/param); Administrator (Windows) / sudo (Linux) voor bind
- **Output:** gedeelde USB-schijf op poort 3240
- **Fouten:** vereist-admin checks, expliciete meldingen; graceful bij ontbrekende tools
- **Status:** nieuw

## Python Scripts

### scripts/db_init.py
- **Functie:** Maakt/migreert SQLite database met schema versioning
- **Input:** geen (leest DB_PATH constant)
- **Output:** data/archive_catalog.db
- **Fouten:** Exit 1 bij migratiefout, meldt schema-versie mismatch
- **Contract:** MIGRATIONS dict met versienummer → functie mapping. Idempotent.
- **Status:** stabiel

### scripts/extract_metadata.py
- **Functie:** Metadata extractie per bestandstype + datum-prioriteitsbepaling
- **Input:** bestandspad (str)
- **Output:** dict met metadata-velden
- **Ondersteunde types:**
  - PDF → pymupdf (auteur, titel, datum, producer)
  - DOCX/XLSX → python-docx/openpyxl (core properties)
  - Afbeeldingen → exiftool (EXIF DateTimeOriginal)
  - Bestandsnaam → regex datumherkenning
- **Fouten:** Retourneert error_message in dict, crasht niet
- **Status:** stabiel

### scripts/scan_metadata.py
- **Functie:** Recursieve scan van bronmap, schrijft metadata naar SQLite, met lock en checkpointing
- **Input:** config.yaml (scan_roots), gemounte media onder /mnt/archive-ingest/
- **Output:** Records in files tabel, scan-record in scans tabel, logbestand
- **Fouten:** Telt fouten apart (files_ok/files_error), logt per bestand, markeert scan als failed/interrupted
- **Contract:** Lock file voorkomt parallelle scans. Checkpoint per 10 directories.
- **Status:** stabiel

### scripts/search_filename.py
- **Functie:** SQLite zoeken op filename, extensie, auteur, titel, datum, pad, label
- **Input:** CLI args (query, filters)
- **Output:** Terminal: geformateerde resultatenlijst met offline-instructie
- **Fouten:** Geeft "Geen resultaten" bij lege set, geen crashes
- **Status:** stabiel

### scripts/report.py
- **Functie:** Genereert rapport.md + diverse CSV exports uit de catalogus
- **Input:** data/archive_catalog.db
- **Output:** output/rapport_YYYYMMDD-HHMM/ met .md en .csv bestanden
- **Fouten:** Slaat lege categorieën over, meldt "leeg, overgeslagen"
- **Status:** stabiel
