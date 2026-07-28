# Installatiegids — Archive Search Workbench

**🌐 Taal:** [English](INSTALL.md) · **Nederlands**

Deze gids brengt je van een schone machine naar een draaiende workbench: de Flask-webinterface
op poort `5059`, met een lokale SQLite-catalogus en een Recoll full-text index.

> De **app-host moet Linux zijn** (Ubuntu 22.04/24.04 is getest; andere distro's werken ook).
> Windows en andere machines kunnen fungeren als **netwerk-USB-exporters** — zie
> [Netwerk-USB](#optioneel-netwerk-usb-usbip).

---

## 1. Vereisten

- **Een Linux-host.** De app stuurt native Linux-tools aan (`mount` + ntfs-3g voor read-only
  koppelen, Recoll voor indexeren, `usbip` voor netwerk-USB, `/dev/sdX`, `/mnt`). Windows en
  macOS kunnen alleen netwerk-USB-**exporter** (client) zijn, niet de app-host.
- **Debian/Ubuntu is aanbevolen, niet verplicht.** `setup.sh` installeert pakketten via `apt`,
  en Ubuntu 22.04/24.04 is de geteste route — op Debian/Ubuntu is de setup dus één commando.
  Op andere distro's (Fedora, Arch, …) werkt de app net zo goed; installeer de equivalente
  pakketten handmatig (zie de pakketlijst in stap 3) in plaats van `setup.sh` te draaien.
- **Python:** 3.10 of nieuwer.
- **Rechten:** een gebruiker met `sudo`-rechten (nodig om externe media te (un)mounten en om
  `/mnt/archive-ingest` aan te maken).
- **Netwerk:** internettoegang bij de eerste installatie (pakketten + pip).

## 2. Code ophalen

```bash
git clone https://github.com/dma61/archive-search-workbench.git
cd archive-search-workbench
```

## 3. Setup-script draaien

`setup.sh` is **idempotent** (veilig om opnieuw te draaien). Het:

- maakt de werkmappen aan (`config data output logs recoll-indexes temp tests`);
- maakt het read-only ingest-mountpoint `/mnt/archive-ingest` (via `sudo`);
- installeert de systeem-packages hieronder (via `apt`);
- maakt een Python-virtualenv in `.venv/` en installeert hulpbibliotheken;
- verifieert dat de kritieke tools aanwezig zijn.

```bash
./setup.sh
```

**Systeem-packages die het installeert:**
`python3-venv`, `python3-pip`, `sqlite3`, `recoll`, `antiword`, `catdoc`, `poppler-utils`,
`unzip`, `p7zip-full`, `unrar`, `ripgrep`, `smartmontools`, `libimage-exiftool-perl`,
`mediainfo`, `ntfs-3g`, `udisks2`.

Voor de netwerk-USB-functie heb je op de server ook `usbip` nodig (Linux):

```bash
sudo apt install usbip
```

## 4. Webapp-dependencies installeren (gepind)

`setup.sh` zet de virtualenv en algemene tooling op, maar de **webapplicatie**-dependencies
(Flask en de document-parsers die de app gebruikt) zijn gepind in
[`requirements.txt`](requirements.txt) voor reproduceerbaarheid. Installeer ze in de venv:

```bash
.venv/bin/pip install -r requirements.txt
```

Dit installeert: `Flask`, `PyYAML`, `PyMuPDF`, `openpyxl`, `python-docx`.

## 5. Webinterface starten

```bash
.venv/bin/python web_app.py
```

Je ziet dan:

```
Archive Search Workbench v2 — Web Interface
URL: http://0.0.0.0:5059
```

Open **`http://localhost:5059`** (of `http://<server-ip>:5059` vanaf een andere machine op je LAN).

Stoppen: `Ctrl+C`.

## 6. Als service draaien (optioneel, aanbevolen voor een server)

Om de workbench na een herstart te laten doordraaien, installeer je een systemd-unit. Maak
`/etc/systemd/system/archive-search-workbench.service` (pas `User` en paden aan):

```ini
[Unit]
Description=Archive Search Workbench
After=network.target

[Service]
Type=simple
User=JOUW_GEBRUIKER
WorkingDirectory=/pad/naar/archive-search-workbench
ExecStart=/pad/naar/archive-search-workbench/.venv/bin/python web_app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Inschakelen en starten:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now archive-search-workbench
sudo systemctl status archive-search-workbench
```

> Omdat de app externe media (un)mount met `sudo`, heeft de service-gebruiker de juiste
> sudo-rechten nodig. Beperk die via een specifieke sudoers-regel voor `mount`/`umount`
> in plaats van volledige sudo te geven.

## 7. Configuratie

- **`config/config.yaml`** — scan-roots, database-/index-paden, read-only-afdwinging,
  bestandstype-filters, groottelimieten. De standaardwaarden werken meteen.
- **Netwerk-USB-hosts** — kopieer het voorbeeld en pas het aan voor je eigen machines:

  ```bash
  cp config/remote_hosts.example.yaml config/remote_hosts.yaml
  ```

  `config/remote_hosts.yaml` staat in **.gitignore** (per-deployment). Ontbreekt het, dan
  valt de app terug op het voorbeeldbestand.

## 8. Eerste gebruik

1. Sluit een externe schijf aan.
2. In de web-UI: **registreer** hem (krijgt een label zoals `ARCHIVE-DISK-001` — sticker de
   schijf), dan **scannen**, dan **indexeren**.
3. **Zoek** over alle geïndexeerde media — op metadata of op documentinhoud.
4. **Uitwerpen** als je klaar bent; zoeken blijft offline werken.

Je kunt dezelfde stappen ook vanaf de terminal doen met `./menu.sh`.

## Optioneel: Netwerk-USB (USB/IP)

Om een schijf te indexeren die in een **andere** machine zit, richt je die machine in als
USB/IP-*exporter* en installeer je (optioneel) de agent per machine. Volledige instructies:
[`network-usb/README.md`](network-usb/README.md).

- **Windows-exporter:** `usbipd-win` — zie `network-usb/windows/`.
- **Linux-exporter:** `usbip` — zie `network-usb/linux/`.
- **Agent per machine** (laat de server de schijf lokaal opsommen/lezen): `network-usb/agent/`.

USB/IP-verkeer (poort 3240) is onversleuteld — gebruik het op een vertrouwd LAN of tunnel via
Tailscale/SSH. Op de server wordt de schijf altijd **read-only** gemount.

## Bijwerken

```bash
git pull
./setup.sh                                   # pikt nieuwe systeem-packages op
.venv/bin/pip install -r requirements.txt    # pikt dependency-wijzigingen op
# als het als service draait:
sudo systemctl restart archive-search-workbench
```

## Probleemoplossing

| Symptoom | Waarschijnlijke oorzaak / oplossing |
| --- | --- |
| `ModuleNotFoundError: flask` | Draai `.venv/bin/pip install -r requirements.txt` (stap 4). |
| Web-UI onbereikbaar vanaf andere pc | Firewall moet TCP `5059` toestaan; de app bindt `0.0.0.0`. |
| `setup.sh` meldt een ontbrekende tool | Draai `sudo apt install <package>`; check de log in `logs/setup_*.log`. |
| Mount mislukt / permission denied | Zorg voor sudo-rechten voor `mount`/`umount` en dat `/mnt/archive-ingest` bestaat. |
| Inhoud-zoeken geeft niets | Zorg dat de **index**-stap liep (Recoll); check `recoll-indexes/`. |
| Netwerk-USB-machine onbereikbaar | Controleer dat poort `3240` open is en de schijf `bind`-ed is op de exporter. |

Logs staan onder `logs/` (setup, scan, mount, index, checks). De web-UI toont ze ook onder
**Beheer → Logs**.

## Verwijderen

```bash
# service stoppen & verwijderen (indien geïnstalleerd)
sudo systemctl disable --now archive-search-workbench
sudo rm /etc/systemd/system/archive-search-workbench.service

# project verwijderen (inclusief gegenereerde data)
rm -rf /pad/naar/archive-search-workbench

# eventueel het ingest-mountpoint verwijderen
sudo rmdir /mnt/archive-ingest
```

Systeem-packages die via `apt` zijn geïnstalleerd, blijven staan; verwijder ze handmatig als
je ze niet meer nodig hebt.
