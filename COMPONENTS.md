# COMPONENTS — Archive Search Workbench

## Shell Scripts

### detect_disks.sh
- **Function:** Shows all connected block devices with UUID, label, model, serial, mount status, estimated media type
- **Input:** none (reads /sys, lsblk, blkid, udevadm)
- **Output:** Terminal output (text)
- **Errors:** Fails gracefully if device info is not readable
- **Status:** stable

### mount_readonly.sh
- **Function:** Mounts external media read-only, remounts existing rw mounts, verifies write protection
- **Input:** device path + archive label (CLI args)
- **Output:** Mount under /mnt/archive-ingest/LABEL/, log file
- **Errors:** Exit 1 on invalid device, non-read-only mount, or failed write test
- **Status:** stable

### build_recoll_index.sh
- **Function:** Generates recoll.conf per medium, builds full-text index with nice/ionice
- **Input:** optional archive label (CLI arg), otherwise all mounted media
- **Output:** Recoll index in recoll-indexes/LABEL/
- **Errors:** Exit 1 if source directory does not exist
- **Status:** stable

### search_content.sh
- **Function:** Wrapper around recoll query, searches across all or a specific medium
- **Input:** search term (CLI arg), optional --label and --limit
- **Output:** Terminal: file paths + scores
- **Errors:** Graceful if no index found
- **Status:** stable

## Network-USB (USB/IP)

### network-usb/usbip_ctl.sh
- **Function:** Thin, privileged wrapper around `usbip` on the importer (the app host). Verbs:
  `ensure-module` (loads `vhci-hcd`), `list <host>`, `attach <host> <busid>`, `ports`, `detach <port>`
- **Input:** subcommand + args (CLI); uses `sudo -n` (NOPASSWD)
- **Output:** raw usbip text on stdout (parsing happens in `web_app.py`)
- **Errors:** exit 1 = usage error, 2 = usbip error; error message to stderr, no silent failure
- **Status:** new

### web_app.py — /api/remote/* endpoints
- **Function:** Network-USB channel: show hosts, query exportable devices, attach/detach,
  active ports. An attached disk then appears in the existing `/api/disks` flow
- **Input:** `config/remote_hosts.yaml` (hosts); POST body `{host, busid}` / `{port}`
- **Output:** JSON; state in `data/remote_usbip_state.json` (`port↔host↔busid↔dev`)
- **Errors:** reachability check on port 3240; errors as `{...,"error":...}` (UI shows them),
  logged in `logs/network-usb.log`
- **Integration:** `_maybe_detach_remote()` in `api_eject` breaks the USB/IP connection on eject
- **Status:** new

### network-usb/windows/*.ps1 and network-usb/linux/*.sh
- **Function:** Exporter setup and disk sharing (bind) on the machine where the disk is attached
- **Input:** busid (CLI/param); Administrator (Windows) / sudo (Linux) for bind
- **Output:** shared USB disk on port 3240
- **Errors:** requires-admin checks, explicit messages; graceful when tools are missing
- **Status:** new

## Python Scripts

### scripts/db_init.py
- **Function:** Creates/migrates SQLite database with schema versioning
- **Input:** none (reads DB_PATH constant)
- **Output:** data/archive_catalog.db
- **Errors:** Exit 1 on migration error, reports schema version mismatch
- **Contract:** MIGRATIONS dict with version number → function mapping. Idempotent.
- **Status:** stable

### scripts/extract_metadata.py
- **Function:** Metadata extraction per file type + date priority determination
- **Input:** file path (str)
- **Output:** dict with metadata fields
- **Supported types:**
  - PDF → pymupdf (author, title, date, producer)
  - DOCX/XLSX → python-docx/openpyxl (core properties)
  - Images → exiftool (EXIF DateTimeOriginal)
  - Filename → regex date recognition
- **Errors:** Returns error_message in dict, does not crash
- **Status:** stable

### scripts/scan_metadata.py
- **Function:** Recursive scan of source directory, writes metadata to SQLite, with lock and checkpointing
- **Input:** config.yaml (scan_roots), mounted media under /mnt/archive-ingest/
- **Output:** Records in files table, scan record in scans table, log file
- **Errors:** Counts errors separately (files_ok/files_error), logs per file, marks scan as failed/interrupted
- **Contract:** Lock file prevents parallel scans. Checkpoint per 10 directories.
- **Status:** stable

### scripts/search_filename.py
- **Function:** SQLite search on filename, extension, author, title, date, path, label
- **Input:** CLI args (query, filters)
- **Output:** Terminal: formatted results list with offline instruction
- **Errors:** Returns "No results" for an empty set, no crashes
- **Status:** stable

### scripts/report.py
- **Function:** Generates rapport.md + various CSV exports from the catalog
- **Input:** data/archive_catalog.db
- **Output:** output/rapport_YYYYMMDD-HHMM/ with .md and .csv files
- **Errors:** Skips empty categories, reports "empty, skipped"
- **Status:** stable
