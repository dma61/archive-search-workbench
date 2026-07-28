#!/usr/bin/env python3
"""ArchSW-loc agent (Linux) - lokale agent voor de server Archive Search Workbench.

Draait op de machine WAAR DE SCHIJF HANGT. Kan (1) een bestand lokaal lezen van een in
Linux gemounte schijf (transparant, schijf blijft gewoon aangesloten) en (2) een schijf
via USB/IP delen (usbip bind/unbind) als de server blok-toegang nodig heeft.

Endpoints (JSON, token via header X-Agent-Token; /health mag zonder):
  GET  /health                    -> status
  GET  /disks                     -> deelbare USB-apparaten + schijf-identiteit
  GET  /read-file?volume=&path=   -> stream een bestand van de lokaal gemounte schijf
  POST /bind   {busid}            -> usbip bind -b <busid>
  POST /unbind {busid}            -> usbip unbind -b <busid>

Env: AGENT_PORT (default 5060), AGENT_TOKEN_FILE (default /etc/archsw-loc-agent/token).
"""
import json
import mimetypes
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

AGENT_VERSION = "0.2.0"
PORT = int(os.environ.get("AGENT_PORT", "5060"))
TOKEN_FILE = os.environ.get("AGENT_TOKEN_FILE", "/etc/archsw-loc-agent/token")
USBIP = "/usr/bin/usbip"
MAX_READ_BYTES = 300 * 1024 * 1024


def _token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def _bindable():
    _run(["sudo", "modprobe", "usbip-host"])
    rc, out, err = _run(["sudo", USBIP, "list", "-l"])
    devices = []
    if rc != 0:
        return devices
    cur = None
    for line in out.splitlines():
        m = re.search(r'busid\s+(\S+)\s+\(([0-9a-fA-F:]+)\)', line)
        if m:
            if cur:
                devices.append(cur)
            cur = {"busid": m.group(1), "vidpid": m.group(2), "device": "", "likely_disk": False}
        elif cur and line.strip() and not line.strip().startswith("-"):
            desc = line.strip()
            cur["device"] = desc
            cur["likely_disk"] = bool(re.search(r'Mass Storage|Disk|UAS|SCSI|Storage', desc, re.I))
    if cur:
        devices.append(cur)
    for d in devices:
        d.setdefault("state", "Not shared")
    return devices


def _lsblk():
    rc, out, err = _run(["lsblk", "-J", "-o",
                         "NAME,TYPE,TRAN,SERIAL,SIZE,MODEL,UUID,LABEL,FSTYPE,MOUNTPOINT"])
    if rc != 0:
        return {"blockdevices": []}
    try:
        return json.loads(out)
    except Exception:
        return {"blockdevices": []}


def _usbdisks():
    disks = []
    for dev in _lsblk().get("blockdevices", []):
        if dev.get("tran") != "usb" or dev.get("type") != "disk":
            continue
        vols = []
        for ch in dev.get("children", []) or []:
            vols.append({"name": ch.get("name"), "uuid": ch.get("uuid"),
                         "label": ch.get("label"), "fs": ch.get("fstype"),
                         "size": ch.get("size"), "mountpoint": ch.get("mountpoint")})
        disks.append({"name": dev.get("name"), "serial": dev.get("serial"),
                      "model": dev.get("model"), "size": dev.get("size"), "volumes": vols})
    return disks


def _mountpoint_for_label(volume):
    """Vind mountpoint van een gemount volume met dit label."""
    if not volume:
        return None

    def walk(node):
        if node.get("label") == volume and node.get("mountpoint"):
            return node["mountpoint"]
        for ch in node.get("children", []) or []:
            r = walk(ch)
            if r:
                return r
        return None

    for dev in _lsblk().get("blockdevices", []):
        r = walk(dev)
        if r:
            return r
    return None


def _usbip(verb, busid):
    if not re.match(r'^[0-9]+-[0-9.]+$', busid or ""):
        return {"ok": False, "message": f"ongeldige busid: {busid}"}
    _run(["sudo", "modprobe", "usbip-host"])
    rc, out, err = _run(["sudo", USBIP, verb, "-b", busid])
    if rc == 0:
        return {"ok": True, "message": f"{verb} {busid} ok", "output": out}
    return {"ok": False, "message": f"usbip {verb} mislukt: {(err or out).strip()[:200]}"}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self, path):
        return path == "/health" or self.headers.get("X-Agent-Token", "") == _token()

    def log_message(self, *a):
        pass

    def _read_file(self, qs):
        volume = (qs.get("volume", [""])[0]) or ""
        rel = (qs.get("path", [""])[0]) or ""
        if not rel:
            return self._json({"ok": False, "error": "path vereist"}, 400)
        base = _mountpoint_for_label(volume)
        if not base:
            return self._json({"ok": False, "reason": "not_here",
                               "error": f"schijf '{volume}' niet gemount"}, 404)
        full = os.path.realpath(os.path.join(base, rel.lstrip("/")))
        if not (full == os.path.realpath(base) or full.startswith(os.path.realpath(base) + os.sep)):
            return self._json({"ok": False, "error": "ongeldig pad"}, 403)
        if not os.path.isfile(full):
            return self._json({"ok": False, "reason": "not_found",
                               "error": "bestand niet gevonden op de schijf"}, 404)
        size = os.path.getsize(full)
        if size > MAX_READ_BYTES:
            return self._json({"ok": False, "error": f"bestand te groot ({size // (1024*1024)} MB)"}, 413)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._auth_ok(path):
            return self._json({"ok": False, "error": "ongeldig token"}, 401)
        if path == "/health":
            return self._json({"ok": True, "host": os.uname().nodename,
                               "os": "linux", "agent_version": AGENT_VERSION})
        if path == "/disks":
            return self._json({"ok": True, "host": os.uname().nodename,
                               "bindable": _bindable(), "usbdisks": _usbdisks()})
        if path == "/read-file":
            return self._read_file(parse_qs(parsed.query))
        return self._json({"ok": False, "error": f"onbekend: {path}"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if not self._auth_ok(path):
            return self._json({"ok": False, "error": "ongeldig token"}, 401)
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode())
            except Exception:
                body = {}
        if path == "/bind":
            return self._json(_usbip("bind", str(body.get("busid", ""))))
        if path == "/unbind":
            return self._json(_usbip("unbind", str(body.get("busid", ""))))
        return self._json({"ok": False, "error": f"onbekend: {path}"}, 404)


def main():
    if not _token():
        raise SystemExit(f"Geen token in {TOKEN_FILE}. Draai install-agent.sh eerst.")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"ArchSW-loc agent v{AGENT_VERSION} luistert op poort {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
