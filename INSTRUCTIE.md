# Archive Search Workbench — Bouwinstructie

> **Versie:** 1.0
> **Datum:** 2026-05-24
> **Machine:** de indexeer-server (<server-ip>), Ubuntu 24.04
> **Doel:** Persoonlijke "digitale archeologie"-werkbank voor oude opslagmedia

---

## Doel

Oude externe opslagmedia veilig aansluiten en doorzoekbaar maken.
Zoekbaar: PDF, Word, Excel, databases, SQL, Markdown/YAML/code, afbeeldingen, archieven, oude projectstructuren, kennisfragmenten.

## Ondersteunde media

- USB HDD
- USB SSD
- USB-stick / flash drive
- SD-kaart
- externe SATA via USB
- later: NAS-share of disk image

## Kernprincipes

- **Read-only** richting externe media — ALTIJD
- **Niets verwijderen, verplaatsen of schrijven** naar externe media
- **Alles lokaal opslaan** (metadata, indexes, rapporten)
- **Eerst metadata en zoekbaarheid**, later deduplicatie
- **Eerste versie:** één medium tegelijk
- **Latere versie:** meerdere media via powered USB-hub met queue
- **Silent failure verboden** (DEVs-Base Kernregel 0)
- **Elke fout wordt gelogd, geteld, zichtbaar gemaakt**

## Architectuur

    Externe fysieke drager
        ↓
    read-only mount (remount indien nodig)
        ↓
    fysiek medium registreren + stickerlabel geven
        ↓
    metadata scan (met checkpoint/resume)
        ↓
    SQLite metadata-catalogus
        ↓
    Recoll full-text index (per medium)
        ↓
    zoekinterface + rapportages

## Projectmap

    ~/archive-search-workbench/

## Structuur

    archive-search-workbench/
    ├── setup.sh
    ├── menu.sh
    ├── detect_disks.sh
    ├── mount_readonly.sh
    ├── build_recoll_index.sh
    ├── search_content.sh
    ├── config/
    │   └── config.yaml
    ├── scripts/
    │   ├── db_init.py
    │   ├── scan_metadata.py
    │   ├── extract_metadata.py
    │   ├── search_filename.py
    │   └── report.py
    ├── data/
    │   └── archive_catalog.db
    ├── output/
    ├── logs/
    ├── recoll-indexes/
    ├── temp/
    ├── tests/
    │   └── smoke_test.sh
    ├── README.md
    ├── VOORTGANG.md
    ├── COMPONENTS.md
    ├── ROADMAP.md
    ├── archive_search_workbench_glossary.json
    └── INSTRUCTIE.md (dit document)

## Ontdekte realiteit bij eerste uitvoering (2026-05-24)

### Bestaande tools op de server
- python3 3.12.3 (/usr/bin/python3)
- sqlite3 aanwezig
- smartctl aanwezig (maar USB-bridge vereist -d optie)

### Ontbrekende tools (te installeren)
- recoll + recollindex
- ripgrep (rg)
- exiftool / libimage-exiftool-perl
- python3-venv, python3-pip
- antiword, catdoc
- poppler-utils
- unzip, p7zip-full, unrar
- mediainfo

### Mount-probleem ontdekt
De FREECOM 250GB is door devmon gemount als:
- Type: fuseblk (NTFS via ntfs-3g)
- Opties: rw,nosuid,nodev,noatime,user_id=0,group_id=0,default_permissions,allow_other
- Probleem: user_id=0 → alleen root kan lezen
- Oplossing: remount met uid/gid van <nas-gebruiker>, of sudo gebruiken voor scan
- Extra: mount is rw, moet ro worden voor veiligheid

### SMART via USB
- USB-bridge 0x07ab:0xfcfe niet automatisch herkend
- Moet met -d sat of -d scsi geprobeerd worden
- Niet-blokkerend: SMART is nice-to-have, niet vereist

### Eerste testmedium
- Label: FREECOM_250GB
- UUID: 08F42991F42981D4
- Filesystem: NTFS
- Model: 5DLAT80 (Maxtor 250GB IDE via USB)
- Bestanden: 134
- Gebruikt: 95GB (vmware images)
- Inhoud: PINGS (oude PC-backups), vmware conv machines

## Setup vereisten

### Systeem-packages (apt)
python3-venv, python3-pip, sqlite3, recoll, antiword, catdoc,
poppler-utils, unzip, p7zip-full, unrar, ripgrep, smartmontools,
exiftool (libimage-exiftool-perl), mediainfo

### Python venv packages (.venv)
pyyaml, rich, tqdm, pandas, openpyxl, python-docx, pymupdf, pillow, tabulate

### Setup moet idempotent zijn
- Meerdere keren draaien mag niets stukmaken
- Bestaande config niet overschrijven zonder backup
- Bestaande database niet verwijderen

## Config (config/config.yaml)

scan_roots:
  - /mnt/archive-ingest/disk01

output_dir: ./output
sqlite_db: ./data/archive_catalog.db
recoll_index_base: ./recoll-indexes
read_only_required: true
cautious_mode: true
max_file_size_for_text_index_mb: 500
max_archive_recursion_depth: 3
temporary_extract_dir: ./temp
filename_encoding_fallback: [utf-8, cp1252, latin-1]
follow_symlinks: false
scan_checkpoint_enabled: true

include_extensions:
  documenten: [pdf, doc, docx, odt, rtf, txt]
  spreadsheets: [xls, xlsx, xlsm, csv]
  databases: [mdb, accdb, sqlite, db, dbf, sql]
  afbeeldingen: [jpg, jpeg, png, gif, tiff, bmp, heic, webp]
  archieven: [zip, rar, 7z, tar, gz]
  code_kennis: [md, yaml, yml, json, xml, puml, py, ps1, sh, sql, html, css, js]

exclude_dirs:
  - $RECYCLE.BIN
  - System Volume Information
  - node_modules
  - .git
  - AppData
  - Windows
  - Program Files
  - Program Files (x86)
  - RECYCLER

## Database schema

### Tabel: physical_media
- media_id (PK, autoincrement)
- archive_label (uniek, bv ARCHIVE-DISK-001)
- media_type (usb_hdd|usb_ssd|usb_flash|sd_card|external_sata_usb|unknown)
- first_seen (ISO timestamp)
- last_seen (ISO timestamp)
- filesystem_uuid
- volume_label
- device_model
- device_serial
- size_bytes
- filesystem_type
- smart_status (indien beschikbaar)
- sticker_confirmed (boolean)
- sticker_confirmed_at (ISO timestamp)
- notes

### Tabel: scans
- scan_id (PK)
- media_id (FK)
- archive_label
- source_root
- start_time
- end_time
- number_of_files
- total_bytes
- last_checkpoint_dir (voor resume)
- status (running|completed|failed|interrupted)
- errors

### Tabel: files
(Alle velden uit oorspronkelijke instructie + extra:)
- scan_checkpoint_batch (welke batch dit bestand verwerkte)

### Indexes
filename, extension, original_content_date, author, title,
source_root, archive_label, media_id, extension_group

## Verbeteringen t.o.v. oorspronkelijk plan

1. **Encoding fallback** — filename_encoding_fallback in config
2. **Scan checkpointing** — per-directory checkpoint, resume na crash
3. **Symlink/junction protectie** — follow_symlinks: false + realpath tracking
4. **SMART health check** — bij registratie, waarschuwing als failing
5. **Recoll config generatie** — per index een recoll.conf maken
6. **Lock mechanisme** — voorkom dubbele scan of unmount-tijdens-scan
7. **Zip bomb protectie** — max uncompressed size, max files, max path depth
8. **DB migratie** — schema versioning voor latere uitbreidingen
9. **Silent failure verboden** — overal foutentelling en -rapportage
10. **Glossary + VOORTGANG.md** — conform DEVs-Base standaarden

## DEVs-Base documentatie-eisen (extra verplicht)

1. archive_search_workbench_glossary.json (begrippen)
2. VOORTGANG.md (sessielog, status)
3. COMPONENTS.md (per component: input/output/fouten contract)
4. Smoke test (tests/smoke_test.sh)
5. Geen silent failures — elke except logt + telt
6. Progressive disclosure docs (README gelaagd)
7. Nederlandstalige docstrings en commentaar

## Menu (menu.sh)

[1] Toon aangesloten opslagmedia
[2] Toon mountpoints
[3] Registreer / label nieuw fysiek medium
[4] Mount extern medium read-only
[5] Bekijk/wijzig config
[6] Scan metadata
[7] Bouw/update Recoll index
[8] Zoek op bestandsnaam/metadata
[9] Zoek in documentinhoud
[10] Maak rapportages
[11] Toon scanstatus
[12] Veilige unmount
[13] Toon bekende fysieke media
[14] Bekijk logs
[0] Stop

---
