# PROGRESS — Archive Search Workbench

## Status: MVP working (2026-05-24)

First full end-to-end run succeeded on the app host.

## Session 2026-07-24 — Network-USB (USB/IP)

Added: the ability to index/read a disk on **another machine** via USB/IP.
The app host is the importer; the machine where the disk is attached is the exporter.

### Done
- [x] Design choice: USB/IP (block level) — the disk appears on the app host as a local `/dev/sdX`,
 so the existing mount/scan/index pipeline works unchanged (including indexing remotely from scratch)
- [x] `network-usb/usbip_ctl.sh` — thin, privileged usbip wrapper (ensure-module/list/attach/ports/detach)
- [x] `config/remote_hosts.yaml` — exporter hosts ()
- [x] `web_app.py` — endpoints `/api/remote/{hosts,devices,attach,detach,ports}`,
 state `data/remote_usbip_state.json`, and "Network-USB" panel in Admin (with i18n nl/en)
- [x] Eject integration: `_maybe_detach_remote` in `api_eject` → `usbip detach` on eject
- [x] Exporter setup + docs: Windows (`usbipd-win`) and Linux (`usbip`/`usbipd`), `network-usb/README.md`
- [x] Design note for a future app-host→NAS channel

### Design decisions
- **Importer = the app host**: already has usbip tools + `vhci-hcd` module + sudo NOPASSWD
- **Keep the helper thin**: only privileged usbip verbs; parse output in Python (escaping control)
- **Auto-surfacing**: an attached USB/IP disk reports `tran=usb` in lsblk → appears
 automatically in `/api/disks`; no change to the scan/mount logic needed
- **NAS deferred**: DSM lacks usbip kernel modules → separate channel as a backlog finding

### Points of attention / open
- Physical end-to-end test requires a `usbipd-win` install (Administrator) on a Windows machine
- USB/IP port 3240 is unencrypted/unauthenticated → LAN-only; outside the LAN via Tailscale/SSH tunnel

## Session 2026-05-24

### Done
- [x] Created and ran setup.sh (all 8 tools OK, venv OK)
- [x] Created database schema v1 (physical_media, scans, files + indexes)
- [x] detect_disks.sh — shows connected media with details
- [x] mount_readonly.sh — remounted FREECOM from rw to ro, write protection verified
- [x] scan_metadata.py — 73 files, 0 errors, 94.2 GB processed
- [x] extract_metadata.py — PDF, Office, EXIF, date priority system
- [x] build_recoll_index.sh — full-text index built, 8 results for "Dell"
- [x] search_filename.py — SQLite search works
- [x] search_content.sh — Recoll full-text search works
- [x] report.py — rapport.md + 10 CSV exports generated
- [x] menu.sh — interactive menu with all 14 options
- [x] INSTRUCTIE.md with discovered issues

### Discovered issues (resolved)
1. **Bash arithmetic `((x++))` with `set -e`** — returns exit 1 at value 0. Fixed: `$((x+1))` syntax.
2. **Mount permissions devmon** — devmon mounts NTFS with uid=0,gid=0 → unreadable for <nas-user>. Fixed: remount via ntfs-3g with the user's uid/gid.
3. **SMART via USB bridge** — bridge 0x07ab:0xfcfe not recognized. Non-blocking, SMART is optional.
4. **Heredoc with single quotes in Python** — SSH heredoc fails on complex Python. Fixed: write locally + scp.
5. **blockdev without sudo** — size shows 0GB in detect_disks.sh. Cosmetic, not fixed.

### Design decisions
- **Checkpointing per directory** (not per file) — balance between granularity and overhead
- **Lock via PID file** — simple, stale lock detection via `os.kill(pid, 0)`
- **Recoll index per medium** — scales better, can be rebuilt individually
- **Schema versioning in DB** — MIGRATIONS dict, easy to extend
- **Archive label as directory name** under /mnt/archive-ingest/ — predictable, no registry needed

### First test medium
- **Label:** ARCHIVE-DISK-001
- **Physical:** Freecom Classic 250GB USB HDD (Maxtor 5DLAT80, IDE via USB bridge)
- **UUID:** 08F42991F42981D4
- **Contents:** Old PINGS (disk images Dell Dimensions 3000, Latitude D600) from 2003-2010, VMware conversion Toshiba 120GB
- **73 files, 94.2 GB** — mostly disk images (.000/.001 splits), 1 large .vmdk (77GB)

## Next steps
- [ ] Stick a label on FREECOM and confirm it in the DB
- [ ] Test a second medium (USB stick or SD card with more diverse files)
- [ ] Archive content indexing (search inside zip/rar/7z)
- [ ] Add OCR for scanned PDFs
- [ ] Web interface (Flask, port TBD from the 5059-5099 range)
