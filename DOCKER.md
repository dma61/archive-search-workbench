# Running in Docker — Archive Search Workbench

**🌐 Language:** **English** · [Nederlands](DOCKER.nl.md)

This project ships a `Dockerfile` and a `docker-compose.yml` with **two run modes**. Read
this first — one honest caveat decides which mode you need.

---

## The honest caveat

The workbench does two very different kinds of work:

1. **Searching** an already-built catalog + full-text index — pure application logic.
2. **Mounting** external drives, **attaching** them over USB/IP, and **building** the index —
   which needs to talk to the Linux kernel: mount block devices, load the `vhci-hcd` module,
   run `usbip`, use `sudo`.

Kind (1) runs fine in a normal, isolated container. Kind (2) **cannot** run in a clean
sandbox — a container that mounts real hardware needs host-level privileges. We do not fake
this: the two modes below make the trade-off explicit instead of pretending one image does
everything cleanly.

| Mode | Container | What works | What does NOT |
|---|---|---|---|
| **Full** | `--privileged`, host network, host `/dev` | Everything: register, mount (read-only), scan, **index**, USB/IP, search | It is not an isolated sandbox; Linux host only |
| **Web-only** | plain, unprivileged | Serve the UI, **search** an existing catalog/index | Mounting, indexing, USB/IP |

---

## Prerequisites

- Docker Engine + the Compose plugin (`docker compose`).
- **Full mode:** a **Linux host** (mounting real devices does not work on Docker Desktop for
  Mac/Windows, which run a Linux VM without your USB devices). The `vhci-hcd` kernel module
  must be available/loadable on the host for USB/IP.

## Full mode (Linux host — everything works)

```bash
# once, on the host:
sudo mkdir -p /mnt/archive-ingest

docker compose --profile full up -d --build
# open http://<host>:5059  (host network — the app binds the host's port 5059 directly)
```

This runs the container `--privileged` with `network_mode: host` and bind-mounts
`/mnt/archive-ingest` (shared propagation) so drives mounted inside are visible. Your data,
indexes and config live in host folders (`./data`, `./recoll-indexes`, `./config`) so they
survive rebuilds.

> Privileged + host network is powerful. Run it only on a trusted host on a trusted LAN —
> the same trust boundary the USB/IP feature already assumes.

## Web-only mode (clean, unprivileged — search only)

Use this to serve and search a catalog/index you built elsewhere (e.g. copied from a full
install into `./data` and `./recoll-indexes`):

```bash
docker compose --profile web up -d --build
# open http://localhost:5059
```

Mounting/indexing buttons will not be able to touch hardware in this mode — by design.

## Building the image directly (no Compose)

```bash
docker build -t archive-search-workbench .
docker run --rm -p 5059:5059 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/recoll-indexes:/app/recoll-indexes" \
  archive-search-workbench
```

(That single `docker run` is effectively web-only; add `--privileged --network host -v /mnt/archive-ingest:/mnt/archive-ingest:rshared` for full mode.)

## Data & persistence

| Host folder | Container path | Purpose |
|---|---|---|
| `./data` | `/app/data` | SQLite catalog (`archive_catalog.db`) |
| `./recoll-indexes` | `/app/recoll-indexes` | Full-text indexes |
| `./config` | `/app/config` | `config.yaml`, `remote_hosts.yaml` |
| `/mnt/archive-ingest` | `/mnt/archive-ingest` | Read-only mountpoint (full mode) |

## Limitations (no surprises)

- **Docker Desktop (Mac/Windows) cannot mount your USB drives** — the container runs in a
  Linux VM that does not see them. Use full mode on a real Linux host, or use web-only mode.
- **Kernel modules load on the host, not in the container.** Ensure `vhci-hcd` is available
  on the host for USB/IP.
- **RAR** metadata uses `unrar-free` (best-effort), not the non-free `unrar`.

---

## For functional / non-technical readers

**What is this, in plain terms?**

A "container" is a sealed box that holds the app and everything it needs to run, so it starts
the same way on any machine — no manual install of Python, Recoll, and a dozen tools. You
run one command and the search website is up.

**Why are there two versions of the box?**

Because the app does two jobs, and one of them needs special permission:

- **Looking things up** (searching what was already catalogued) is safe and simple. The
  *web-only* box does exactly this. Think of it as a read-only library desk: you can look up
  any card, but you cannot bring in new shelves.
- **Reading a physical drive** (plugging in an old disk, letting the app read it, and adding
  it to the catalogue) means the box must reach real hardware. That requires elevated
  permission on the host computer. The *full* box has that permission.

**Which one do I want?**

- Just need to **search** an archive that was already indexed → **web-only** box. Simplest and
  safest.
- Need to **add new drives** and index them → **full** box, on a Linux computer.

**Is the "full" box dangerous?**

It has broad access to the host computer (that is what lets it read drives). Treat it like a
trusted appliance: run it on a computer and network you control, not on a shared or public
machine. It never *writes* to your archive drives — they are always mounted read-only.

**Where does my data go?**

Into normal folders next to the app (`data`, `recoll-indexes`, `config`), not locked inside
the box. You can back them up, copy them, or move them to another machine.
