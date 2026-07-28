# Archive Search Workbench

**🌐 Language:** **English** · [Nederlands](README.nl.md)

A personal "digital archaeology" workbench for old storage media. Index your external
USB drives, sticks and SD cards **once**, then full-text search their contents from any
machine on your network — **even when the drive is offline**. Reconnect a drive over
USB/IP to open the files you find.

Think of it as *Google Desktop Search, but for your whole archive of external drives*.

- 🔍 **Full-text + metadata search** across every drive you have ever indexed.
- 🔌 **Attach a drive to any machine** on your LAN via USB/IP and index it remotely.
- 🔒 **Read-only by design** — external media are never written to.
- 💾 **Offline-capable** — the catalog and index live locally, so search keeps working
  after you disconnect the drive; you only reconnect it to open a file.

---

## How it works

1. **Connect** an external medium (USB HDD/SSD, stick, SD card).
2. **Register** it — the app detects the medium and assigns a label
   (e.g. `ARCHIVE-DISK-001`); put a matching sticker on the physical medium.
3. **Read-only mount** — mounted automatically under `/mnt/archive-ingest/LABEL/`
   (read-only; write protection is actively verified).
4. **Scan** — all files are read into a local SQLite catalog with their metadata
   (dates, authors, titles).
5. **Index** — Recoll builds a full-text index of the document contents.
6. **Search** — find files across **all** media at once, by metadata or by content.
7. **Disconnect** — unmount safely. Search keeps working from the local catalog/index;
   the app tells you which sticker label to reconnect to open the original file.

## Human–AI collaboration & contributing

This project was built in close **collaboration between a human and an AI agent**: the
human steers, decides and reviews; the AI analyses, builds and documents — in small,
verifiable steps and with honest documentation (no silent failures). We recommend making
changes the **same way**: human-in-the-loop, transparent and traceable.

Want to help build? You will work most comfortably with a **coding agent**: drop the most
recent **handover** from [`handovers/`](handovers/) into your agent (e.g. Claude Code) as
starting context. That handover captures the current state, the leading files, the
deliberate choices and the next step — so the agent can continue precisely without
re-analysing the whole project. Finish your work by writing a fresh handover for the next
person.

Improvements, questions and findings are very welcome. **Report changes via an issue** in
the repository (describe *what*, *why* and *how you tested*) — that keeps the project
usable, traceable and transferable for everyone. This is a donation-supported project;
when reusing it, please keep the origin and donation reference intact (see [`NOTICE`](NOTICE)).

## Installation

See **[INSTALL.md](INSTALL.md)** for the full, step-by-step installation guide
([Nederlandse versie](INSTALL.nl.md)). In short:

```bash
git clone https://github.com/dma61/archive-search-workbench.git
cd archive-search-workbench
./setup.sh                          # system tools + Python virtualenv
.venv/bin/pip install -r requirements.txt   # web app dependencies (Flask, ...)
.venv/bin/python web_app.py         # → http://localhost:5059
```

## Two ways to use it

### Web interface (recommended)

```bash
.venv/bin/python web_app.py         # → http://localhost:5059
```

Open `http://<server>:5059` in a browser to register media, scan, index, search
(metadata + content), inspect logs, and manage network-USB drives.

### Command-line menu

```bash
./menu.sh
```

An interactive menu covering the same steps: detect ([3]), register, scan ([6]),
index ([7]), metadata search ([8]), content search ([9]), unmount ([12]).

## Using a drive on another machine (Network-USB / USB/IP)

You do not have to plug the drive into the server. Via **USB/IP** you can attach it to any
machine on your network; the server then imports it as a local disk and indexes it normally.

1. Set up the machine that holds the drive as an *exporter* once (see
   [`network-usb/README.md`](network-usb/README.md)) — Windows via `usbipd-win`, Linux via `usbip`.
2. Share the drive (`bind`) on that machine.
3. Open the workbench → **Manage** → **"Network-USB"** panel → pick the machine →
   **Attach to server**.
4. The drive appears under *Connected disks*; labelling/mounting/scanning works as usual.
5. **Ejecting** automatically breaks the network attachment.

> USB/IP traffic (port 3240) is unencrypted — use it on a trusted LAN, or tunnel via
> Tailscale/SSH for off-LAN use. On the server the drive is always mounted **read-only**.

## After disconnecting

- **Metadata search** always works — data lives in the local SQLite database.
- **Content search** always works — the Recoll index is stored locally.
- To open an original file, reconnect the right medium (the app tells you which sticker label).

## Safety

- External media are ALWAYS mounted read-only.
- The tool NEVER writes to external media.
- Write protection is actively verified after mounting.
- All data (database, indexes, reports) is stored locally.

## Requirements

- Ubuntu 22.04+ / 24.04 (Linux server side)
- Python 3.10+
- sudo rights for mount/unmount
- Recoll, ripgrep, exiftool, p7zip, unrar (installed by `setup.sh`)

Full details and pinned Python dependencies: [INSTALL.md](INSTALL.md) and
[`requirements.txt`](requirements.txt).

## Project structure

    archive-search-workbench/
    ├── web_app.py               # Flask web interface (port 5059) — primary UI
    ├── setup.sh                 # Setup (idempotent): system tools + venv
    ├── menu.sh                  # Interactive CLI menu
    ├── detect_disks.sh          # Media detection
    ├── mount_readonly.sh        # Read-only mount/unmount
    ├── build_recoll_index.sh    # Recoll indexing
    ├── search_content.sh        # Full-text search
    ├── config/config.yaml       # Configuration
    ├── scripts/                 # DB init, metadata scan/extract, search, reports
    ├── network-usb/             # USB/IP exporter setup + per-machine agent
    ├── docs/                    # Architecture & sequence diagrams (PUML + SVG)
    ├── handovers/               # AI handovers (start context for coding agents)
    ├── data/archive_catalog.db  # SQLite catalog (generated)
    ├── recoll-indexes/          # Full-text indexes per medium (generated)
    └── logs/                    # All log files (generated)

## License & donation

Licensed under the **Apache License 2.0** — see [`LICENSE`](LICENSE).

This is a **donation-supported** project. Under Apache-2.0 §4(d) the [`NOTICE`](NOTICE)
file must be preserved on reuse, modification and distribution. Please keep the donation
reference visible and working so continued development stays supported.
