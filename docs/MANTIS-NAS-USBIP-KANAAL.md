# Mantis-bevinding — Apart de server→NAS kanaal voor Netwerk-USB

> Kant-en-klare bevinding om in te dienen in MantisBT. Vul project/categorie naar
> eigen inrichting in. Zie ook `network-usb/README.md` en `ROADMAP.md`.

- **Samenvatting:** Netwerk-USB (USB/IP) uitbreiden met een apart kanaal de server → Synology NAS.
- **Categorie:** Archive Search Workbench / Netwerk-USB
- **Prioriteit:** normaal
- **Ernst:** feature / uitbreiding
- **Gerapporteerd:** 2026-07-24

## Situatie

De workbench heeft nu een **Netwerk-USB (USB/IP)** kanaal (fase "Netwerk-USB"): een schijf
aan een Windows- of Linux-machine wordt via `usbipd`/`usbip` doorgegeven aan de server
(`<server-ip>`, importer), die hem als lokale `/dev/sdX` read-only mount en indexeert.

De **Synology NAS (`<nas-ip>`)** doet hier bewust nog niet aan mee: DSM levert standaard
de usbip-kernelmodules niet (`usbip-host`/`usbip_host` ontbreken of zijn niet geladen), dus de
NAS kan nu geen exporter zijn. De gebruiker wil op termijn wél een route de server → NAS, als
**apart kanaal** in de app.

## Gewenst

Een schijf die aan de NAS hangt (of een op de NAS aanwezige share/disk-image) via een apart,
duidelijk gescheiden kanaal in de app kunnen indexeren/uitlezen, naast het bestaande USB/IP-kanaal.

## Opties (te wegen bij uitwerking)

1. **usbip-modules voor DSM bouwen** — `usbip_host`/`usbip_common_mod` compileren tegen de
   DSM-kernel (toolchain per DSM-versie). Meest "gelijk" aan het bestaande kanaal, maar
   fragiel bij DSM-updates.
2. **File-level NAS-kanaal (aanbevolen als apart kanaal)** — de NAS deelt de schijf/inhoud
   read-only (NFS/SMB of een kleine read-only agent); de app krijgt een tweede, expliciet
   "NAS"-kanaal dat die share mount/bevraagt. Robuust en DSM-update-bestendig, en dekt ook
   disk-images op de NAS. Sluit aan op bestaande ROADMAP fase 6 (NAS-integratie) en fase 2
   (disk-image support).
3. **Disk-image via de NAS** — `.img/.vhd/.vmdk` op de NAS read-only loop-mounten op de
   de server en indexeren (overlapt met ROADMAP fase 6 "disk image support").

## Aanwijzing voor implementatie

Houd het als **apart kanaal** naast Netwerk-USB (aparte config-sectie + eigen endpoints/paneel),
zodat de USB/IP-aannames (busid, `vhci-hcd`, `usbip attach`) niet vermengd raken met het
NAS-pad. Hergebruik de bestaande read-only mount + scan/index-pijplijn.

## Acceptatiecriterium

Een aan de NAS gekoppelde schijf (of NAS-share/disk-image) is via de app te mounten (read-only),
te scannen en te doorzoeken, zonder de bestaande USB/IP-flow te wijzigen.
