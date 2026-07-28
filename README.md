# Archive Search Workbench

Persoonlijke "digitale archeologie"-werkbank voor oude opslagmedia.
Maakt externe USB-schijven, sticks en SD-kaarten doorzoekbaar zonder ze permanent aangesloten te houden.

## Mens-AI-samenwerking & bijdragen

Dit project is ontwikkeld in nauwe **samenwerking tussen mens en AI-agent**: de mens stuurt,
beslist en toetst; de AI analyseert, bouwt en documenteert — in kleine, controleerbare stappen en
met eerlijke documentatie (geen stille fouten). We bevelen aan om aanpassingen op **dezelfde manier**
te maken: mens-in-the-loop, transparant en herleidbaar.

Wil je meebouwen? Dan werk je het prettigst met een **coding-agent**: plaats de meest recente
**handover** uit de map [`handovers/`](handovers/) als startcontext in je agent (bijvoorbeeld
Claude Code). Die handover bevat de huidige stand, de leidende bestanden, de bewuste keuzes en de
eerstvolgende stap — zo kan de agent meteen gericht verder zonder het hele project opnieuw te
analyseren. Rond je werk af met een verse handover voor de volgende.

Verbeteringen, vragen en bevindingen zijn van harte welkom. **Koppel wijzigingen terug via een
issue** in de repository (beschrijf wat, waarom en hoe getest) — dat houdt het project voor iedereen
bruikbaar, herleidbaar en overdraagbaar. Dit is een door donaties ondersteund project; laat bij
(her)gebruik de herkomst en de donatie-verwijzing intact (zie `NOTICE`).

## Snel starten

```bash
cd ~/archive-search-workbench
./setup.sh          # Eenmalig: installeer dependencies
./menu.sh           # Start interactief menu
```

## Hoe het werkt

1. **Aansluiten** — Sluit een externe drager aan (USB HDD, SSD, stick, SD-kaart)
2. **Registreren** — Menu optie [3]: detecteert het medium, geeft een label (bijv. ARCHIVE-DISK-001), vraagt om sticker te plakken
3. **Read-only mount** — Automatisch gemount onder `/mnt/archive-ingest/LABEL/` (read-only, schrijfbeveiliging geverifieerd)
4. **Scannen** — Menu optie [6]: scant alle bestanden, extraheert metadata (datums, auteurs, titels)
5. **Indexeren** — Menu optie [7]: bouwt Recoll full-text index voor zoeken in documentinhoud
6. **Zoeken** — Menu optie [8] (metadata) of [9] (inhoud): vind bestanden over alle media heen
7. **Loskoppelen** — Menu optie [12]: veilig unmounten. Zoeken blijft werken via lokale database/index.

## Na loskoppelen

- Metadata-zoeken (optie 8) werkt altijd — data staat in lokale SQLite database
- Inhoud-zoeken (optie 9) werkt altijd — Recoll index is lokaal opgeslagen
- Origineel bestand openen: sluit de juiste drager opnieuw aan (het systeem vertelt welk stickerlabel)

## Schijf op een andere machine (Netwerk-USB / USB/IP)

Je hoeft de schijf niet per se op de server aan te sluiten. Via **USB/IP** kun je hem aan
elke machine in je netwerk hangen; de server importeert hem dan als lokale schijf en
indexeert hem normaal.

1. Richt de machine waar de schijf hangt eenmalig in als *exporter* (zie
   [`network-usb/README.md`](network-usb/README.md)) — Windows via `usbipd-win`, Linux via `usbip`.
2. Deel de schijf (`bind`) op die machine.
3. Open de workbench → **Beheer** → paneel **"Netwerk-USB"** → kies de machine → **Koppel aan server**.
4. De schijf verschijnt bij *Aangesloten schijven*; labelen/mounten/scannen gaat zoals normaal.
5. **Uitwerpen** verbreekt automatisch de netwerk-koppeling.

> USB/IP-verkeer (poort 3240) is onversleuteld — gebruik dit op een vertrouwd LAN, of tunnel
> via Tailscale/SSH voor buiten-LAN. De schijf wordt op de server altijd **read-only** gemount.
> De Synology NAS ondersteunt dit nog niet (zie `docs/MANTIS-NAS-USBIP-KANAAL.md`).

## Veiligheid

- Externe media worden ALTIJD read-only gemount
- Er wordt NOOIT naar externe media geschreven
- Schrijfbeveiliging wordt actief geverifieerd na mount
- Alle data (database, indexes, rapporten) staat lokaal

## Commando-referentie

```bash
./detect_disks.sh                         # Toon aangesloten media
./mount_readonly.sh /dev/sda1 LABEL       # Mount read-only
./mount_readonly.sh --unmount LABEL       # Veilig unmounten

# Via Python venv:
.venv/bin/python scripts/scan_metadata.py          # Scan alle gemounte media
.venv/bin/python scripts/search_filename.py "zoek" # Zoek op naam/metadata
.venv/bin/python scripts/search_filename.py -e pdf # Zoek alle PDF's
.venv/bin/python scripts/report.py                 # Genereer rapporten

./build_recoll_index.sh                   # Bouw Recoll index (alle media)
./build_recoll_index.sh ARCHIVE-DISK-001  # Specifiek medium
./search_content.sh "zoekterm"            # Zoek in documentinhoud
```

## Vereisten

- Ubuntu 22.04+ / 24.04
- Python 3.10+
- sudo-rechten voor mount/unmount
- Recoll, ripgrep, exiftool, p7zip, unrar (geinstalleerd via setup.sh)

## Projectstructuur

    archive-search-workbench/
    ├── setup.sh                 # Installatie (idempotent)
    ├── menu.sh                  # Interactief menu
    ├── detect_disks.sh          # Media detectie
    ├── mount_readonly.sh        # Read-only mount/unmount
    ├── build_recoll_index.sh    # Recoll indexering
    ├── search_content.sh        # Full-text zoeken
    ├── config/config.yaml       # Configuratie
    ├── scripts/
    │   ├── db_init.py           # Database schema + migraties
    │   ├── scan_metadata.py     # Metadata scanner (checkpointing)
    │   ├── extract_metadata.py  # Metadata extractie per type
    │   ├── search_filename.py   # SQLite zoekopdrachten
    │   └── report.py            # Rapportage generator
    ├── data/archive_catalog.db  # SQLite catalogus
    ├── recoll-indexes/          # Full-text indexes (per medium)
    ├── output/                  # Gegenereerde rapporten
    └── logs/                    # Alle logbestanden
