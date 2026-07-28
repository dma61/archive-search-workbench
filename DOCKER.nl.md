# Draaien in Docker — Archive Search Workbench

**🌐 Taal:** [English](DOCKER.md) · **Nederlands**

Dit project levert een `Dockerfile` en een `docker-compose.yml` met **twee draaimodi**. Lees
dit eerst — één eerlijke kanttekening bepaalt welke modus je nodig hebt.

---

## De eerlijke kanttekening

De workbench doet twee heel verschillende soorten werk:

1. **Zoeken** in een al opgebouwde catalogus + full-text index — pure applicatielogica.
2. **Aankoppelen** van externe schijven, ze **via USB/IP koppelen** en de index **bouwen** —
   dat moet met de Linux-kernel praten: block-devices mounten, de `vhci-hcd`-module laden,
   `usbip` draaien, `sudo` gebruiken.

Soort (1) draait prima in een normale, geïsoleerde container. Soort (2) **kan niet** in een
schone sandbox — een container die echte hardware mount, heeft host-privileges nodig. Dat
verdoezelen we niet: de twee modi hieronder maken de afweging expliciet in plaats van te doen
alsof één image alles schoon afhandelt.

| Modus | Container | Wat werkt | Wat NIET |
|---|---|---|---|
| **Full** | `--privileged`, host-netwerk, host `/dev` | Alles: registreren, mounten (read-only), scannen, **indexeren**, USB/IP, zoeken | Geen geïsoleerde sandbox; alleen Linux-host |
| **Web-only** | gewoon, onprivileged | UI serveren, **zoeken** in bestaande catalogus/index | Mounten, indexeren, USB/IP |

---

## Vereisten

- Docker Engine + de Compose-plugin (`docker compose`).
- **Full-modus:** een **Linux-host** (echte apparaten mounten werkt niet op Docker Desktop
  voor Mac/Windows — die draaien een Linux-VM zonder jouw USB-apparaten). De
  `vhci-hcd`-kernelmodule moet op de host beschikbaar/laadbaar zijn voor USB/IP.

## Full-modus (Linux-host — alles werkt)

```bash
# eenmalig, op de host:
sudo mkdir -p /mnt/archive-ingest

docker compose --profile full up -d --build
# open http://<host>:5059  (host-netwerk — de app bindt direct poort 5059 van de host)
```

Dit draait de container `--privileged` met `network_mode: host` en bind-mount
`/mnt/archive-ingest` (gedeelde propagatie) zodat schijven die binnenin gemount worden
zichtbaar zijn. Je data, indexes en config staan in host-mappen (`./data`, `./recoll-indexes`,
`./config`) en overleven zo een herbouw.

> Privileged + host-netwerk is krachtig. Draai het alleen op een vertrouwde host op een
> vertrouwd LAN — dezelfde vertrouwensgrens die de USB/IP-functie sowieso al aanneemt.

## Web-only-modus (schoon, onprivileged — alleen zoeken)

Gebruik dit om een catalogus/index te serveren en te doorzoeken die je elders bouwde (bijv.
gekopieerd vanuit een volledige installatie naar `./data` en `./recoll-indexes`):

```bash
docker compose --profile web up -d --build
# open http://localhost:5059
```

Mount-/indexeer-knoppen kunnen in deze modus geen hardware benaderen — met opzet.

## Image direct bouwen (zonder Compose)

```bash
docker build -t archive-search-workbench .
docker run --rm -p 5059:5059 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/recoll-indexes:/app/recoll-indexes" \
  archive-search-workbench
```

(Die enkele `docker run` is in de praktijk web-only; voeg
`--privileged --network host -v /mnt/archive-ingest:/mnt/archive-ingest:rshared` toe voor
full-modus.)

## Data & persistentie

| Host-map | Container-pad | Doel |
|---|---|---|
| `./data` | `/app/data` | SQLite-catalogus (`archive_catalog.db`) |
| `./recoll-indexes` | `/app/recoll-indexes` | Full-text indexes |
| `./config` | `/app/config` | `config.yaml`, `remote_hosts.yaml` |
| `/mnt/archive-ingest` | `/mnt/archive-ingest` | Read-only mountpoint (full-modus) |

## Beperkingen (geen verrassingen)

- **Docker Desktop (Mac/Windows) kan jouw USB-schijven niet mounten** — de container draait
  in een Linux-VM die ze niet ziet. Gebruik full-modus op een echte Linux-host, of web-only.
- **Kernelmodules laden op de host, niet in de container.** Zorg dat `vhci-hcd` op de host
  beschikbaar is voor USB/IP.
- **RAR**-metadata gebruikt `unrar-free` (best-effort), niet het non-free `unrar`.

---

## Voor functionele / niet-technische lezers

**Wat is dit, in gewone taal?**

Een "container" is een afgesloten doos met de app en alles wat die nodig heeft om te draaien,
zodat hij op elke machine hetzelfde start — zonder handmatig Python, Recoll en een tiental
tools te installeren. Je draait één commando en de zoek-website staat live.

**Waarom zijn er twee versies van de doos?**

Omdat de app twee taken doet, en één daarvan speciale toestemming nodig heeft:

- **Iets opzoeken** (zoeken in wat al gecatalogiseerd is) is veilig en simpel. De *web-only*
  doos doet precies dat. Zie het als een alleen-lezen bibliotheekbalie: je kunt elke kaart
  opzoeken, maar geen nieuwe kasten binnenbrengen.
- **Een fysieke schijf lezen** (een oude schijf aansluiten, de app hem laten lezen en aan de
  catalogus toevoegen) betekent dat de doos echte hardware moet bereiken. Dat vraagt
  verhoogde toestemming op de host-computer. De *full*-doos heeft die toestemming.

**Welke wil ik?**

- Alleen een al geïndexeerd archief **doorzoeken** → **web-only**-doos. Simpelst en veiligst.
- **Nieuwe schijven toevoegen** en indexeren → **full**-doos, op een Linux-computer.

**Is de "full"-doos gevaarlijk?**

Die heeft brede toegang tot de host-computer (dat is juist wat hem schijven laat lezen).
Behandel hem als een vertrouwd apparaat: draai hem op een computer en netwerk die je zelf
beheert, niet op een gedeelde of publieke machine. Hij schrijft nooit naar je archiefschijven
— die worden altijd read-only gekoppeld.

**Waar gaat mijn data heen?**

Naar gewone mappen naast de app (`data`, `recoll-indexes`, `config`), niet opgesloten in de
doos. Je kunt ze back-uppen, kopiëren of naar een andere machine verplaatsen.
