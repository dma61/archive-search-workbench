# Network-USB (USB/IP) — index a drive on another machine

This component lets you attach an archive drive to **any machine on your network** and still
read and index it through the Archive Search Workbench running on the app host. The USB port
of that other machine effectively becomes a port of the app host.

## How it works

```
 Drive is plugged in here            App host (the brain)
 ┌─────────────────────┐  USB/IP over  ┌────────────────────────┐
 │ Exporter machine    │  TCP port 3240│ App host (importer)    │
 │ usbipd -> bind      │ ────────────► │ usbip attach -> /dev/sdX│
 │ (Windows or Linux)  │               │ -> read-only mount->scan│
 └─────────────────────┘               └────────────────────────┘
```

- **Exporter** = the machine the drive is physically plugged into. It "binds" the USB drive.
- **Importer** = the app host. It "attaches" the drive; it appears there as a real local
  `/dev/sdX`, so the existing read-only mount / scan / index pipeline works unchanged.

The app host is already prepared (usbip tools + the `vhci-hcd` module). You only need to set
up the **exporter machine**.

## Step 1 — Set up the exporter (once per machine)

### Windows
Run in **PowerShell as Administrator**, from this directory:

```powershell
.\windows\install-usbipd.ps1   # installs usbipd-win (also opens firewall port 3240)
```

### Linux
```bash
./linux/setup-exporter.sh      # installs usbip, loads modules, starts usbipd
```

## Step 2 — Share a drive (each time you want to index one)

Plug the drive into the exporter machine and find its **busid**:

### Windows
```powershell
.\windows\export-disk.ps1 -List          # list devices + busid
.\windows\export-disk.ps1 -BusId 2-4     # share the drive (Administrator)
```

### Linux
```bash
./linux/bind-disk.sh                      # list devices + busid
./linux/bind-disk.sh 1-4                  # share the drive
```

## Step 3 — Attach from the app host

Open the workbench: `http://<app-host>:5059/` → **Manage** tab →
the **"Network-USB — drive on another machine"** panel:

1. Pick the machine (🟢 = reachable on port 3240).
2. Click **Show drives on this machine**.
3. Click **Attach to host** on the right drive.
4. The drive then appears under **Connected disks** — label/mount/scan as usual.

> Tip: for a copy-paste onboarding of a brand-new exporter machine, see
> [`../docs/ONBOARDING-NEW-MACHINE.md`](../docs/ONBOARDING-NEW-MACHINE.md).

## Step 4 — Detach

Eject the drive with the normal **Eject** button on the disk card: that unmounts the drive
**and** automatically breaks the network attachment (`usbip detach`). You can also detach
manually in the Network-USB panel under **Active network attachments**. On the exporter
machine, release the drive again with `export-disk.ps1 -Unbind` (Windows) or
`bind-disk.sh --unbind` (Linux).

## Security (important)

- **USB/IP traffic on port 3240 is unencrypted and unauthenticated.** Use it only on a
  **trusted LAN**. Restrict firewall port 3240 to the app host's IP.
- Need to pass through a drive from **outside the LAN**? Tunnel USB/IP over **Tailscale** or
  an **SSH tunnel** instead of exposing port 3240 directly.
- **Read-only stays guaranteed:** the app host always mounts the drive read-only
  (`mount_readonly.sh`, with write-protection verification), even though the block device is
  physically on another machine.
- While a drive is shared (bound), it is **not** available as a normal disk on the exporter
  machine. For read-only archive drives that is fine.

## NAS as exporter

A typical consumer NAS (e.g. Synology DSM) usually cannot act as an exporter out of the box,
because it ships without the usbip kernel modules (`usbip-host` / `usbip_host`). If you want
to index drives held on a NAS, a file-level channel (a read-only NFS/SMB share, or a small
read-only agent) is more robust than trying to build usbip modules for the NAS firmware.

## Files

| File | Role |
|---|---|
| `usbip_ctl.sh` | App-host helper (importer): `ensure-module`, `list`, `attach`, `ports`, `detach`. Called by `web_app.py`. |
| `windows/install-usbipd.ps1` | Install usbipd-win (Administrator). |
| `windows/export-disk.ps1` | Share/release a USB drive on Windows. |
| `linux/setup-exporter.sh` | Set up a Linux machine as exporter. |
| `linux/bind-disk.sh` | Share/release a USB drive on Linux. |
