# Handover — HDD Indexer: Netwerk-USB, agent & auto-koppelen

### 0. Kernpunt van deze handover
Deze handover gaat over de HDD Indexer (Archive Search Workbench, Flask-webapp op de server
<server-ip>:5059). In deze sessie is de app uitgebreid van "schijf moet fysiek op de server
hangen" naar **"hang een geïndexeerde schijf aan elke machine en lees hem toch uit"**: via USB/IP
(blok-niveau) én, transparanter, via een lokale **ArchSW-loc agent** die bestanden rechtstreeks van
de aangesloten schijf leest. Daarbovenop: auto-koppelen bij het openen van een zoekresultaat
(met UUID-verificatie), multi-select ZIP-download, en een licht/donker-thema conform de DEVs-Base
GUI-canon. Alles is uitgerold (deploy-flow via de NAS bare repo) en gepusht t/m commit `a99a433`.
De opvolger moet als eerste weten: de schijf-identificatie gaat op **filesystem-UUID** (niet op het
niet-unieke volume-label), en "welke machine ben jij" wordt uit het browser-IP afgeleid met terugval
op alle geregistreerde agents.

### 1. Opdracht
- Doel van dit spoor: een geïndexeerde externe schijf aan een willekeurige machine kunnen hangen
 en die via de workbench uitlezen/indexeren.
- Gewenst resultaat deze run: werkende auto-koppel- en lokale-lees-flow + nette documentatie/licentie.

### 2. Huidige stand
- Werkt end-to-end: Netwerk-USB (USB/IP), ArchSW-loc agent v0.3.0 op de Desktop, auto-koppelen bij
 openen, lokaal lezen (schijf blijft in Windows), UUID-verificatie, multi-select ZIP-download,
 licht/donker-toggle. Gevalideerd met schijven ARCHIVE-DISK-005 (Elements) en 006 (Expansion).
- Wat nog niet: content-(full-text)-zoektab heeft nog geen selectievakjes; de server→NAS-kanaal is
 uitgesteld (Mantis #1111, DSM mist usbip-modules).
- Laatste status: alles gecommit en gepusht (`a99a433`); lokaal/NAS/de server in sync; service actief.

### 3. Focus en waarheid
- Leidende bestanden: `web_app.py` (Flask, ~6k regels, alle endpoints + embedded UI),
 `network-usb/usbip_ctl.sh`, `network-usb/agent/windows/archsw-loc-agent.ps1` + `install-agent.ps1`,
 `network-usb/agent/linux/archsw-loc-agent.py`, `config/remote_hosts.yaml`.
- Source of truth: de draaiende service op de server; deploy uitsluitend via git (zie sectie 8).
- Direct relevante afhankelijkheden: `/api/remote/*` endpoints, `/api/smart-serve`, `/api/download-zip`,
 agent op poort 5060 (token in `data/remote_agents.json`), pummelaar (diagrammen), Mantis (bevindingen).
- Permissiemodus van deze sessie: **waarschijnlijk verhoogd/bypass** — schrijven, committen, pushen,
 deployen en Mantis-issues aanmaken gebeurde zonder per-actie-bevestiging. Exacte modus: [onbekend];
 verifieer dit voordat je HITL-aannames doet.

### 4. Reeds geprobeerd
- USB/IP-basis + agent + auto-koppelen gebouwd, uitgerold en getest. Meerdere agent-herinstallaties
 (v0.1→0.3) i.v.m. nieuwe endpoints (`/read-file`, volume-UUID).
- Niet opnieuw doen zonder reden: matchen op volume-label (niet uniek — twee "Elements"-schijven);
 vertrouwen op één vast bron-IP (breekt bij VPN/proxy); em-dash/niet-ASCII in `.ps1` (PS 5.1 leest
 zonder BOM als ANSI → parse-fouten).

### 5. Eerstvolgende stap
1. De door De Releaser gegenereerde bestanden committen (LICENSE Apache-2.0, NOTICE, requirements.txt
 gepind, documentkaart, CHANGELOG tweetalig, deze handover).
2. Optioneel: selectievakjes op de content-(full-text)-zoektab.
3. Optioneel: SAG-bevinding (werkt-altijd verbindingstool) en de server→NAS-kanaal (Mantis #1111).
- Kleine succesdefinitie: de zes documenten staan onder git in de HDD Indexer met een duidelijke commit.

### 6. Karakter van dit spoor
- Projectethos: functioneel, transparant (HITL), sober; werkt-altijd boven elegant.
- Kwaliteitslat: geen silent failure; eerlijk melden wat niet lukt; controleerbaar.
- Voorkeursaanpak: klein, toetsbaar, server-side oplossen waar het kan (geen extra agent-reinstall).
- Vermijden: dubbelzinnige identificatie (label), aannames over netwerk/IP, niet-ASCII in PowerShell.
- Beslisregel bij twijfel: kies de eenvoudigste controleerbare oplossing die bij de gebruiker klopt.

### 7. Kaders
- Bewuste keuzes: read-only richting externe media; UUID als identiteitssleutel; browser-IP + terugval
 voor "huidige machine"; donatie geborgd via NOTICE (Apache §4d) + ingebouwd donate-contract/knop.
- Niet doen: schrijven naar externe archiefschijven; donate-contract/knop stilzwijgend verwijderen.

### 8. Praktische context
- App: de server <server-ip>:5059 (systemd `archive-search-workbench.service`, sudo NOPASSWD).
- Deploy: lokaal committen → `git push` naar NAS bare repo
 (`ssh://<nas-host>/~/git-repos/indexer-exhdd-archive-search-workbench.git`) → op de server
 `git pull --ff-only && sudo systemctl restart archive-search-workbench`.
- Agent: Desktop <jouw-machine-ip> (DHCP verschuift; was), poort 5060, token in `data/remote_agents.json`.
- Poorten: 5059 web, 5060 agent, 3240 usbip. Diagrammen: pummelaar :5050. Issues: Mantis :5231.
- Aan te koppelen (sub)mappen: hele repo volstaat; voor netwerk-USB-werk vooral `network-usb/`.

### 9. Werkafspraak voor de volgende agent
Gebruik deze handover als primaire context. Controleer eerst alleen de leidende bestanden, de
`/api/remote/*`-flow en de eerstvolgende stap. Ga niet het hele `web_app.py` heranalyseren tenzij
de handover aantoonbaar niet meer klopt. Rapporteer eerst: wat geverifieerd, of de handover klopt,
en de uitkomst van de eerstvolgende stap.

### 10. Afsluiting
⚠️ HANDOVER VASTGELEGD — 20260728-1242
Bestand: `handovers/20260728-1242_handover_netwerk-usb-agent.md` (valt onder git van de HDD Indexer — commit mee).
Aan te koppelen (sub)mappen: geen — hele projectroot volstaat.

> Let op: deze handover is opgesteld door de `/handover`-capability toe te passen (opgehaald van de
> ontwikkelmachine), niet door het slashcommando zelf. De sessie is NIET gearchiveerd — dat vraagt een aparte,
> expliciete bevestiging en beëindigt het gesprek; dat doe ik hier bewust niet.
