# ROADMAP — Archive Search Workbench

## Phase 1: MVP (COMPLETED 2026-05-24)
- [x] Scan one medium at a time
- [x] Read-only mount with verification
- [x] SQLite metadata catalog
- [x] Recoll full-text indexing
- [x] Search on filename/metadata
- [x] Search inside document content
- [x] Reporting (markdown + CSV)
- [x] Interactive menu
- [x] Sticker label workflow

## Phase 2: Depth
- [ ] Archive content indexing (extract zip/rar/7z to temp, scan, clean up)
- [ ] OCR for scanned PDFs (tesseract)
- [ ] Hashing per file (sha256) to prepare for deduplication
- [ ] SMART health check on registration (with USB bridge fallback)
- [ ] Encoding repair for old Windows filenames (cp1252)
- [ ] More metadata: email .msg/.eml headers, .pst navigation

## Phase 3: Multi-media
- [ ] Multiple media at once via powered USB hub
- [ ] Queue system for sequential processing
- [ ] Automatic detection + ingest on connect (udev rule)
- [ ] Scan resume after crash (per-directory checkpoint)
- [ ] Parallel indexing (Recoll per medium)

## Phase 3.5: Network-USB (USB/IP) — index a disk on any machine
- [x] USB/IP channel: disk on another machine as a local `/dev/sdX` on the app host
- [x] App-host helper `network-usb/usbip_ctl.sh` (ensure-module/list/attach/ports/detach)
- [x] App endpoints `/api/remote/*` + "Network-USB" panel in Admin
- [x] Eject detaches automatically (`usbip detach`)
- [x] Exporter setup Windows (usbipd-win) and Linux (usbip/usbipd) + docs
- [ ] Separate app-host→NAS channel (DSM lacks usbip modules)
- [ ] Optional: USB/IP over Tailscale/SSH tunnel for use outside the LAN

## Phase 4: Web interface
- [ ] Flask web interface (port from the 5059-5099 range)
- [ ] Search via browser
- [ ] Thumbnail previews (images, PDF first page)
- [ ] Result export (CSV, JSON)
- [ ] Availability dashboard (which medium online/offline)

## Phase 5: Intelligence
- [ ] AI summary per document (local LLM or API)
- [ ] Automatic categorization/tagging
- [ ] Knowledge graph (triplets from documents)
- [ ] Deduplication with similarity scoring
- [ ] Image comparison (perceptual hash)
- [ ] Timeline visualization of documents

## Phase 6: Integration
- [ ] App-host→NAS Network-USB channel (separate channel)
- [ ] NAS export (sync catalog to Synology)
- [ ] Obsidian integration (search results as markdown notes)
- [ ] YAML export for knowledge base
- [ ] Disk image support (.img, .vhd, .vmdk mount)
- [ ] Backup verification (compare catalog with NAS contents)
