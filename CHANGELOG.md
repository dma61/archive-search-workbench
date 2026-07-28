# Changelog — HDD Indexer (Archive Search Workbench)

Tweetalig (Nederlands / English). Formaat: Keep a Changelog. Afgeleid uit de git-historie.
Dit project heeft (nog) geen formele semver-tags; onderstaande versies zijn functionele mijlpalen.

---

## Nederlands

### [1.0.0] — 2026-07-28 — Eerste publieke release
**Toegevoegd**
- **Publieke uitgave onder Apache-2.0** (+ `NOTICE` met behoud van de donatie-verwijzing).
- **Tweetalige documentatie (EN/NL):** README, INSTALL-gids, en generieke Engelse
  architectuur-/sequence-diagrammen (PUML + SVG).
- **Docker:** `Dockerfile` + `docker-compose.yml` met twee modi (schone web-only en
  volledige, privileged Linux-host) + `DOCKER.md`/`DOCKER.nl.md` incl. uitleg voor functionelen.
- **AI-Handover** als startcontext voor coding-agents; README-sectie over mens-AI-samenwerking.

**Gewijzigd**
- Alle machine-/persoons-/netwerk-identifiers vervangen door generieke placeholders;
  per-deployment host-config uit git (`config/remote_hosts.example.yaml` als sjabloon).

### [0.4] — 2026-07-24 — Netwerk-USB, agent & auto-koppelen
**Toegevoegd**
- **Netwerk-USB (USB/IP):** een archiefschijf aan een andere machine hangen en toch via de
  de server (importer) read-only indexeren/uitlezen; exporter-scripts (Windows `usbipd-win`, Linux
  `usbip`) + onboarding-pagina met downloadbare scripts.
- **ArchSW-loc agent** (per machine, token-auth): detecteert schijven, deelt ze op verzoek, en
  **leest bestanden lokaal** zodat de schijf gewoon bij de gebruiker blijft (geen USB/IP-overname).
- **Auto-koppelen bij openen:** klik in de zoekresultaten op openen → de app bepaalt jouw machine,
  koppelt de juiste schijf en opent het bestand; verificatie via de **filesystem-UUID** (twee
  schijven met hetzelfde label "Elements" correct onderscheiden).
- **Meerdere bestanden aanvinken en samen als ZIP downloaden** (ook binaries); aanvinken kan
  alleen bij aangesloten schijven.
- **GUI conform DEVs-Base canon:** licht thema als standaard + **licht/donker-schakelaar**,
  kleuren als CSS-variabelen, vaste statuskleuren, eigen favicon.

**Opgelost**
- Duidelijke reden bij niet-opgehaalde bestanden ("schijf niet aangesloten", niet "corrupt").
- Robuuste agent-koppeling: terugval op alle agents als het bron-IP (VPN/proxy) geen agent heeft.
- Padverschil-fallback bij lokaal lezen (volume-label-prefix strippen).
- Harde stop tegen een open-lus bij lokaal lezen; installer maakt poort 5060 vrij bij herinstallatie.

### [0.2] — 2026-05 — Schijfbeheer & UX
**Toegevoegd**: sticker-actie in mediatabel; multi-partitie-schijf als één ingest-kandidaat;
UI-prefill vanuit aangesloten schijven; `project.json`-manifest.
**Opgelost**: eject/detach-status; mount-reparatie en handmatige hervatting; valse scan-hervattingen
bij stale mounts; label-canonicalisatie en autoresume bij opstart.

### [0.1] — 2026-05-24 — MVP
Eerste werkende end-to-end: read-only mount met schrijfbeveiliging, SQLite-metadata-catalogus,
Recoll full-text-index, zoeken op naam/metadata en in inhoud, rapportages (Markdown + CSV),
interactief menu, stickerlabel-workflow.

---

## English

### [1.0.0] — 2026-07-28 — First public release
**Added**
- **Public release under Apache-2.0** (+ `NOTICE` preserving the donation reference).
- **Bilingual documentation (EN/NL):** README, INSTALL guide, and generic English
  architecture/sequence diagrams (PUML + SVG).
- **Docker:** `Dockerfile` + `docker-compose.yml` with two modes (clean web-only and a full,
  privileged Linux-host mode) + `DOCKER.md`/`DOCKER.nl.md` including a plain-language section.
- **AI handover** as start context for coding agents; README section on human–AI collaboration.

**Changed**
- All machine/person/network identifiers replaced with generic placeholders; per-deployment
  host config removed from git (`config/remote_hosts.example.yaml` as a template).

### [0.4] — 2026-07-24 — Network-USB, agent & auto-attach
**Added**
- **Network-USB (USB/IP):** attach an archive disk to any machine and still index/read it read-only
  via the de server (importer); exporter scripts (Windows `usbipd-win`, Linux `usbip`) + an onboarding
  page with downloadable scripts.
- **ArchSW-loc agent** (per machine, token auth): detects disks, shares them on request, and
  **reads files locally** so the disk stays with the user (no USB/IP takeover).
- **Auto-attach on open:** click open in the search results → the app determines your machine,
  attaches the right disk and opens the file; verified via the **filesystem UUID** (correctly
  distinguishing two disks that share the volume label "Elements").
- **Multi-select and download several files as a ZIP** (binaries too); selection is only possible
  for currently connected disks.
- **GUI per the DEVs-Base canon:** light theme by default + **light/dark toggle**, colors as CSS
  variables, fixed status colors, a dedicated favicon.

**Fixed**
- Clear reason for files that could not be fetched ("disk not connected", not "corrupt").
- Robust agent binding: fall back to all agents when the source IP (VPN/proxy) has no agent.
- Path-difference fallback for local reads (strip the volume-label prefix).
- Hard stop against an open-loop on local read; installer frees port 5060 on reinstall.

### [0.2] — 2026-05 — Disk management & UX
**Added**: sticker action in the media table; multi-partition disk treated as a single ingest
candidate; UI prefill from connected disks; `project.json` manifest.
**Fixed**: eject/detach state; mount repair and manual resume; false scan resumes from stale
mounts; label canonicalization and startup autoresume.

### [0.1] — 2026-05-24 — MVP
First working end-to-end: read-only mount with write protection, SQLite metadata catalog, Recoll
full-text index, search by name/metadata and by content, reports (Markdown + CSV), interactive
menu, sticker-label workflow.
