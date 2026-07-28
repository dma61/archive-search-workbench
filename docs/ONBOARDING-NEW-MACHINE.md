# Onboarding a new machine — Network-USB exporter (USB/IP)

## In one sentence

Want to attach an archive drive to a new machine and index it through the Archive Search
Workbench? Set that machine up as a **USB/IP exporter** — all scripts and commands are on a
single page served by the app host:

**➡️ `http://<app-host>:5059/netwerk-usb`**

## Windows (copy-paste, PowerShell as Administrator)

```powershell
irm http://<app-host>:5059/netwerk-usb/dl/install-usbipd.ps1 -OutFile $env:TEMP\install-usbipd.ps1
irm http://<app-host>:5059/netwerk-usb/dl/export-disk.ps1   -OutFile $env:TEMP\export-disk.ps1
& $env:TEMP\install-usbipd.ps1
# plug the drive in, then:
& $env:TEMP\export-disk.ps1 -List          # find the busid
& $env:TEMP\export-disk.ps1 -BusId 2-4     # share that drive (fill in the busid)
```

## Linux

```bash
curl -fsSL http://<app-host>:5059/netwerk-usb/dl/setup-exporter.sh -o setup-exporter.sh
curl -fsSL http://<app-host>:5059/netwerk-usb/dl/bind-disk.sh      -o bind-disk.sh
bash setup-exporter.sh
bash bind-disk.sh                            # show busid
bash bind-disk.sh 1-4                        # share that drive
```

## Then

Workbench → **Manage** tab → **Network-USB** panel → pick the machine → **Attach to host**.
The drive appears under *Connected disks* to label, mount and scan. Ejecting automatically
breaks the network attachment.

## Where things live

- **Next to the app on the host:** `network-usb/` (`windows/*.ps1`, `linux/*.sh`,
  `usbip_ctl.sh`, `README.md`).
- **Downloadable from the app host:** `http://<app-host>:5059/netwerk-usb`.

## Security

USB/IP port 3240 is unencrypted/unauthenticated → use it only on a trusted LAN; restrict the
firewall to the app host, or tunnel via Tailscale/SSH. The drive is always mounted read-only.
A typical consumer NAS cannot act as an exporter out of the box (it lacks the usbip kernel
modules) — see [`../network-usb/README.md`](../network-usb/README.md) for a file-level
alternative.
