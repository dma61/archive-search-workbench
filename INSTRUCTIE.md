# Archive Search Workbench — Build Instruction

> **Version:** 1.0
> **Date:** 2026-05-24
> **Machine:** the indexing app host (<server-ip>), Ubuntu 24.04
> **Goal:** Personal "digital archaeology" workbench for old storage media

---

## Goal

Safely connect old external storage media and make them searchable.
Searchable: PDF, Word, Excel, databases, SQL, Markdown/YAML/code, images, archives, old project structures, knowledge fragments.

## Supported media

- USB HDD
- USB SSD
- USB stick / flash drive
- SD card
- external SATA via USB
- later: NAS share or disk image

## Core principles

- **Read-only** toward external media — ALWAYS
- **Never delete, move, or write** to external media
- **Store everything locally** (metadata, indexes, reports)
- **Metadata and searchability first**, deduplication later
- **First version:** one medium at a time
- **Later version:** multiple media via powered USB hub with a queue
- **Silent failure forbidden** (DEVs-Base Core Rule 0)
- **Every error is logged, counted, made visible**

## Architecture

    External physical carrier
        ↓
    read-only mount (remount if needed)
        ↓
    register physical medium + assign sticker label
        ↓
    metadata scan (with checkpoint/resume)
        ↓
    SQLite metadata catalog
        ↓
    Recoll full-text index (per medium)
        ↓
    search interface + reports

## Project directory

    ~/archive-search-workbench/

## Structure

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
    └── INSTRUCTIE.md (this document)

## Reality discovered on first run (2026-05-24)

### Existing tools on the app host
- python3 3.12.3 (/usr/bin/python3)
- sqlite3 present
- smartctl present (but USB bridge requires the -d option)

### Missing tools (to be installed)
- recoll + recollindex
- ripgrep (rg)
- exiftool / libimage-exiftool-perl
- python3-venv, python3-pip
- antiword, catdoc
- poppler-utils
- unzip, p7zip-full, unrar
- mediainfo

### Mount problem discovered
The FREECOM 250GB was mounted by devmon as:
- Type: fuseblk (NTFS via ntfs-3g)
- Options: rw,nosuid,nodev,noatime,user_id=0,group_id=0,default_permissions,allow_other
- Problem: user_id=0 → only root can read
- Solution: remount with the uid/gid of <nas-user>, or use sudo for the scan
- Extra: mount is rw, must become ro for safety

### SMART via USB
- USB bridge 0x07ab:0xfcfe not automatically recognized
- Must be tried with -d sat or -d scsi
- Non-blocking: SMART is nice-to-have, not required

### First test medium
- Label: FREECOM_250GB
- UUID: 08F42991F42981D4
- Filesystem: NTFS
- Model: 5DLAT80 (Maxtor 250GB IDE via USB)
- Files: 134
- Used: 95GB (vmware images)
- Contents: PINGS (old PC backups), vmware conv machines

## Setup requirements

### System packages (apt)
python3-venv, python3-pip, sqlite3, recoll, antiword, catdoc,
poppler-utils, unzip, p7zip-full, unrar, ripgrep, smartmontools,
exiftool (libimage-exiftool-perl), mediainfo

### Python venv packages (.venv)
pyyaml, rich, tqdm, pandas, openpyxl, python-docx, pymupdf, pillow, tabulate

### Setup must be idempotent
- Running multiple times must not break anything
- Do not overwrite existing config without a backup
- Do not delete an existing database

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

### Table: physical_media
- media_id (PK, autoincrement)
- archive_label (unique, e.g. ARCHIVE-DISK-001)
- media_type (usb_hdd|usb_ssd|usb_flash|sd_card|external_sata_usb|unknown)
- first_seen (ISO timestamp)
- last_seen (ISO timestamp)
- filesystem_uuid
- volume_label
- device_model
- device_serial
- size_bytes
- filesystem_type
- smart_status (if available)
- sticker_confirmed (boolean)
- sticker_confirmed_at (ISO timestamp)
- notes

### Table: scans
- scan_id (PK)
- media_id (FK)
- archive_label
- source_root
- start_time
- end_time
- number_of_files
- total_bytes
- last_checkpoint_dir (for resume)
- status (running|completed|failed|interrupted)
- errors

### Table: files
(All fields from the original instruction + extra:)
- scan_checkpoint_batch (which batch processed this file)

### Indexes
filename, extension, original_content_date, author, title,
source_root, archive_label, media_id, extension_group

## Improvements over the original plan

1. **Encoding fallback** — filename_encoding_fallback in config
2. **Scan checkpointing** — per-directory checkpoint, resume after crash
3. **Symlink/junction protection** — follow_symlinks: false + realpath tracking
4. **SMART health check** — on registration, warning if failing
5. **Recoll config generation** — create a recoll.conf per index
6. **Lock mechanism** — prevent double scan or unmount-during-scan
7. **Zip bomb protection** — max uncompressed size, max files, max path depth
8. **DB migration** — schema versioning for later extensions
9. **Silent failure forbidden** — error counting and reporting everywhere
10. **Glossary + VOORTGANG.md** — per DEVs-Base standards

## DEVs-Base documentation requirements (additionally mandatory)

1. archive_search_workbench_glossary.json (glossary)
2. VOORTGANG.md (session log, status)
3. COMPONENTS.md (per component: input/output/error contract)
4. Smoke test (tests/smoke_test.sh)
5. No silent failures — every except logs + counts
6. Progressive disclosure docs (layered README)
7. Docstrings and comments in the project language

## Menu (menu.sh)

[1] Show connected storage media
[2] Show mountpoints
[3] Register / label a new physical medium
[4] Mount external medium read-only
[5] View/edit config
[6] Scan metadata
[7] Build/update Recoll index
[8] Search on filename/metadata
[9] Search inside document content
[10] Generate reports
[11] Show scan status
[12] Safe unmount
[13] Show known physical media
[14] View logs
[0] Quit

---
