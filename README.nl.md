# Archive Search Workbench

**🌐 Taal:** [English](README.md) · **Nederlands**

Persoonlijke "digitale archeologie"-werkbank voor oude opslagmedia. Indexeer je externe
USB-schijven, sticks en SD-kaarten **één keer** en doorzoek daarna de inhoud full-text
vanaf elke machine in je netwerk — **óók als de schijf niet is aangesloten**. Koppel een
schijf via USB/IP weer aan om gevonden bestanden te openen.

Zie het als *Google Desktop Search, maar voor je hele archief van externe schijven*.

- 🔍 **Full-text- én metadata-zoeken** over alle schijven die je ooit hebt geïndexeerd.
- 🔌 **Koppel een schijf aan elke machine** in je LAN via USB/IP en indexeer op afstand.
- 🔒 **Read-only van opzet** — er wordt nooit naar externe media geschreven.
- 💾 **Werkt offline** — catalogus en index staan lokaal, dus zoeken blijft werken na
  loskoppelen; je koppelt alleen weer aan om een bestand te openen.

---

## Hoe het werkt

1. **Aansluiten** — Sluit een externe drager aan (USB HDD/SSD, stick, SD-kaart).
2. **Registreren** — de app detecteert het medium en geeft een label
   (bijv. `ARCHIVE-DISK-001`); plak een bijpassende sticker op het medium.
3. **Read-only mount** — automatisch gemount onder `/mnt/archive-ingest/LABEL/`
   (read-only; schrijfbeveiliging wordt actief geverifieerd).
4. **Scannen** — alle bestanden worden in een lokale SQLite-catalogus gelezen met hun
   metadata (datums, auteurs, titels).
5. **Indexeren** — Recoll bouwt een full-text index van de documentinhoud.
6. **Zoeken** — vind bestanden over **alle** media tegelijk, op metadata of op inhoud.
7. **Loskoppelen** — veilig unmounten. Zoeken blijft werken via de lokale catalogus/index;
   de app vertelt welk stickerlabel je moet aansluiten om het origineel te openen.

## Mens-AI-samenwerking & bijdragen

Dit project is ontwikkeld in nauwe **samenwerking tussen mens en AI-agent**: de mens stuurt,
beslist en toetst; de AI analyseert, bouwt en documenteert — in kleine, controleerbare stappen
en met eerlijke documentatie (geen stille fouten). We bevelen aan om aanpassingen op **dezelfde
manier** te maken: mens-in-the-loop, transparant en herleidbaar.

Wil je meebouwen? Dan werk je het prettigst met een **coding-agent**: plaats de meest recente
**handover** uit de map [`handovers/`](handovers/) als startcontext in je agent (bijvoorbeeld
Claude Code). Die handover bevat de huidige stand, de leidende bestanden, de bewuste keuzes en de
eerstvolgende stap — zo kan de agent meteen gericht verder zonder het hele project opnieuw te
analyseren. Rond je werk af met een verse handover voor de volgende.

Verbeteringen, vragen en bevindingen zijn van harte welkom. **Koppel wijzigingen terug via een
issue** in de repository (beschrijf wat, waarom en hoe getest) — dat houdt het project voor iedereen
bruikbaar, herleidbaar en overdraagbaar. Dit is een door donaties ondersteund project; laat bij
(her)gebruik de herkomst en de donatie-verwijzing intact (zie [`NOTICE`](NOTICE)).

## Installatie

Zie **[INSTALL.nl.md](INSTALL.nl.md)** voor de volledige, stapsgewijze installatiegids
([English version](INSTALL.md)). Kort:

```bash
git clone https://github.com/dma61/archive-search-workbench.git
cd archive-search-workbench
./setup.sh                          # systeemtools + Python-virtualenv
.venv/bin/pip install -r requirements.txt   # webapp-dependencies (Flask, ...)
.venv/bin/python web_app.py         # → http://localhost:5059
```

## Twee manieren om het te gebruiken

### Webinterface (aanbevolen)

```bash
.venv/bin/python web_app.py         # → http://localhost:5059
```

Open `http://<server>:5059` in een browser om media te registreren, te scannen, te
indexeren, te zoeken (metadata + inhoud), logs te bekijken en netwerk-USB-schijven te beheren.

### Commando-menu

```bash
./menu.sh
```

Een interactief menu met dezelfde stappen: detecteren ([3]), registreren, scannen ([6]),
indexeren ([7]), metadata-zoeken ([8]), inhoud-zoeken ([9]), unmounten ([12]).

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

## Na loskoppelen

- **Metadata-zoeken** werkt altijd — data staat in de lokale SQLite-database.
- **Inhoud-zoeken** werkt altijd — de Recoll-index is lokaal opgeslagen.
- Origineel bestand openen: sluit de juiste drager opnieuw aan (het systeem vertelt welk stickerlabel).

## Veiligheid

- Externe media worden ALTIJD read-only gemount.
- Er wordt NOOIT naar externe media geschreven.
- Schrijfbeveiliging wordt actief geverifieerd na mount.
- Alle data (database, indexes, rapporten) staat lokaal.

## Vereisten

- **Een Linux-host** voor de app (die stuurt native Linux-tools aan: mount/ntfs-3g, Recoll, usbip).
  Debian/Ubuntu is aanbevolen en getest — `setup.sh` gebruikt `apt`; andere distro's werken ook
  met handmatig geïnstalleerde pakketten. Windows/macOS kunnen alleen netwerk-USB-*exporter* zijn.
- Python 3.10+
- sudo-rechten voor mount/unmount
- Recoll, ripgrep, exiftool, p7zip, unrar (geïnstalleerd via `setup.sh`)

Volledige details en gepinde Python-dependencies: [INSTALL.nl.md](INSTALL.nl.md) en
[`requirements.txt`](requirements.txt).

## Projectstructuur

    archive-search-workbench/
    ├── web_app.py               # Flask-webinterface (poort 5059) — primaire UI
    ├── setup.sh                 # Setup (idempotent): systeemtools + venv
    ├── menu.sh                  # Interactief CLI-menu
    ├── detect_disks.sh          # Media-detectie
    ├── mount_readonly.sh        # Read-only mount/unmount
    ├── build_recoll_index.sh    # Recoll-indexering
    ├── search_content.sh        # Full-text zoeken
    ├── config/config.yaml       # Configuratie
    ├── scripts/                 # DB-init, metadata scan/extract, zoeken, rapporten
    ├── network-usb/             # USB/IP-exporter-setup + agent per machine
    ├── docs/                    # Architectuur- & sequence-diagrammen (PUML + SVG)
    ├── handovers/               # AI-handovers (startcontext voor coding-agents)
    ├── data/archive_catalog.db  # SQLite-catalogus (gegenereerd)
    ├── recoll-indexes/          # Full-text indexes per medium (gegenereerd)
    └── logs/                    # Alle logbestanden (gegenereerd)

## Licentie & donatie

Gelicentieerd onder de **Apache License 2.0** — zie [`LICENSE`](LICENSE).

Dit is een door **donaties ondersteund** project. Op grond van Apache-2.0 §4(d) moet het
[`NOTICE`](NOTICE)-bestand behouden blijven bij (her)gebruik, wijziging en verspreiding.
Laat de donatie-verwijzing zichtbaar en werkend, zodat de doorontwikkeling ondersteund blijft.
