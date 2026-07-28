# Installation Guide — Archive Search Workbench

**🌐 Language:** **English** · [Nederlands](INSTALL.nl.md)

This guide gets you from a clean machine to a running workbench: the Flask web interface
on port `5059`, backed by a local SQLite catalog and Recoll full-text index.

> The **server side runs on Linux** (Ubuntu 22.04/24.04 tested). Windows and other Linux
> machines can act as **network-USB exporters** — see [Network-USB](#optional-network-usb-usbip).

---

## 1. Prerequisites

- **OS:** Ubuntu 22.04+ / 24.04 (or a Debian-based distro with `apt`).
- **Python:** 3.10 or newer.
- **Privileges:** a user with `sudo` rights (needed to mount/unmount external media and to
  create `/mnt/archive-ingest`).
- **Network:** internet access for the first install (apt + pip).

## 2. Get the code

```bash
git clone https://github.com/dma61/archive-search-workbench.git
cd archive-search-workbench
```

## 3. Run the setup script

`setup.sh` is **idempotent** (safe to re-run). It:

- creates the working directories (`config data output logs recoll-indexes temp tests`);
- creates the read-only ingest mountpoint `/mnt/archive-ingest` (via `sudo`);
- installs the system packages listed below (via `apt`);
- creates a Python virtualenv in `.venv/` and installs helper libraries;
- verifies that the critical tools are present.

```bash
./setup.sh
```

**System packages it installs:**
`python3-venv`, `python3-pip`, `sqlite3`, `recoll`, `antiword`, `catdoc`, `poppler-utils`,
`unzip`, `p7zip-full`, `unrar`, `ripgrep`, `smartmontools`, `libimage-exiftool-perl`,
`mediainfo`, `ntfs-3g`, `udisks2`.

For the network-USB feature you also need `usbip` on the server (Linux):

```bash
sudo apt install usbip
```

## 4. Install the web-app dependencies (pinned)

`setup.sh` sets up the virtualenv and general tooling, but the **web application**
dependencies (Flask and the document parsers used by the app) are pinned in
[`requirements.txt`](requirements.txt) for reproducibility. Install them into the venv:

```bash
.venv/bin/pip install -r requirements.txt
```

This installs: `Flask`, `PyYAML`, `PyMuPDF`, `openpyxl`, `python-docx`.

## 5. Run the web interface

```bash
.venv/bin/python web_app.py
```

You should see:

```
Archive Search Workbench v2 — Web Interface
URL: http://0.0.0.0:5059
```

Open **`http://localhost:5059`** (or `http://<server-ip>:5059` from another machine on your LAN).

To stop it, press `Ctrl+C`.

## 6. Run it as a service (optional, recommended for a server)

To keep the workbench running across reboots, install a systemd unit. Create
`/etc/systemd/system/archive-search-workbench.service` (adjust `User` and paths):

```ini
[Unit]
Description=Archive Search Workbench
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/archive-search-workbench
ExecStart=/path/to/archive-search-workbench/.venv/bin/python web_app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now archive-search-workbench
sudo systemctl status archive-search-workbench
```

> Because the app mounts/unmounts external media with `sudo`, the service user needs the
> appropriate sudo rights. Restrict them via a dedicated sudoers rule for `mount`/`umount`
> rather than granting blanket sudo.

## 7. Configuration

- **`config/config.yaml`** — scan roots, database/index paths, read-only enforcement,
  file-type filters, size limits. The defaults work out of the box.
- **Network-USB hosts** — copy the example and edit it for your own machines:

  ```bash
  cp config/remote_hosts.example.yaml config/remote_hosts.yaml
  ```

  `config/remote_hosts.yaml` is **git-ignored** (it is per-deployment). If it is absent,
  the app falls back to the example file.

## 8. First use

1. Connect an external drive.
2. In the web UI: **register** it (it gets a label such as `ARCHIVE-DISK-001` — sticker the
   drive), then **scan**, then **index**.
3. **Search** across all indexed media — by metadata or by document content.
4. **Eject** when done; search keeps working offline.

You can also drive the same steps from the terminal with `./menu.sh`.

## Optional: Network-USB (USB/IP)

To index a drive that is plugged into **another** machine, set that machine up as a USB/IP
*exporter* and (optionally) install the per-machine agent. Full instructions:
[`network-usb/README.md`](network-usb/README.md).

- **Windows exporter:** `usbipd-win` — see `network-usb/windows/`.
- **Linux exporter:** `usbip` — see `network-usb/linux/`.
- **Per-machine agent** (lets the server list/read the disk locally): `network-usb/agent/`.

USB/IP traffic (port 3240) is unencrypted — use it on a trusted LAN or tunnel via
Tailscale/SSH. On the server the drive is always mounted **read-only**.

## Updating

```bash
git pull
./setup.sh                                   # picks up any new system packages
.venv/bin/pip install -r requirements.txt    # picks up dependency changes
# if running as a service:
sudo systemctl restart archive-search-workbench
```

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `ModuleNotFoundError: flask` | Run `.venv/bin/pip install -r requirements.txt` (step 4). |
| Web UI unreachable from another PC | Check the firewall allows TCP `5059`; the app binds `0.0.0.0`. |
| A tool is reported missing by `setup.sh` | Re-run `sudo apt install <package>`; check the log in `logs/setup_*.log`. |
| Mount fails / permission denied | Ensure the user has sudo rights for `mount`/`umount` and `/mnt/archive-ingest` exists. |
| Content search returns nothing | Make sure the **index** step ran (Recoll); check `recoll-indexes/`. |
| Network-USB machine not reachable | Verify port `3240` is open and the drive is `bind`-ed on the exporter. |

Logs live under `logs/` (setup, scan, mount, index, checks). The web UI also exposes logs
under **Manage → Logs**.

## Uninstall

```bash
# stop & remove the service (if installed)
sudo systemctl disable --now archive-search-workbench
sudo rm /etc/systemd/system/archive-search-workbench.service

# remove the project (generated data included)
rm -rf /path/to/archive-search-workbench

# optionally remove the ingest mountpoint
sudo rmdir /mnt/archive-ingest
```

System packages installed via `apt` are left in place; remove them manually if you no
longer need them.
