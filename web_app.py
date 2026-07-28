#!/usr/bin/env python3
"""Archive Search Workbench — Web Interface v2.

Flask app voor zoeken en beheer van de archive catalogus.
Poort: 5059

Wijzigingen v2:
- AND/OR zoek-operatoren + glob/extensie patronen
- Multi-disk selectie en batch verwerking
- Uitwerpen (unmount) na indexeren
- SQL query tab
- Verbeterde voortgang met fase-indicatie en huidige map/bestand
- Tussenverslagen per schijf na scan
- Logboek raadpleegbaar via web
- Sticker: read-only huidig label + nieuw label voorstel
- Scan historie met goed/fout status
"""

import sqlite3
import subprocess
import threading
import json
import os
import re
import shutil
import time
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, send_file, abort, Response

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "data" / "archive_catalog.db"
INDEX_BASE = PROJECT_DIR / "recoll-indexes"
MOUNT_BASE = Path("/mnt/archive-ingest")
PROGRESS_FILE = PROJECT_DIR / "data" / "progress.json"
AUTO_RESUME_ON_STARTUP = os.getenv("ARCHIVE_AUTO_RESUME_ON_STARTUP", "").strip().lower() in (
    '1', 'true', 'yes', 'on'
)
LOG_DIR = PROJECT_DIR / "logs"
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

# --- Netwerk-USB (USB/IP) ---
# De server is de "importer": een schijf die op een andere machine (de "exporter")
# hangt wordt via USB/IP doorgegeven en verschijnt hier als lokale /dev/sdX.
NETWORK_USB_DIR = PROJECT_DIR / "network-usb"
REMOTE_HOSTS_CONFIG = PROJECT_DIR / "config" / "remote_hosts.yaml"
# remote_hosts.yaml is per-deployment (lokaal, niet in git). Val terug op het meegeleverde
# voorbeeld als er nog geen lokale versie is (verse checkout / publieke repo).
if not REMOTE_HOSTS_CONFIG.exists():
    _rh_example = PROJECT_DIR / "config" / "remote_hosts.example.yaml"
    if _rh_example.exists():
        REMOTE_HOSTS_CONFIG = _rh_example
USBIP_CTL = NETWORK_USB_DIR / "usbip_ctl.sh"
REMOTE_STATE_FILE = PROJECT_DIR / "data" / "remote_usbip_state.json"
REMOTE_AGENTS_FILE = PROJECT_DIR / "data" / "remote_agents.json"
USBIP_PORT = 3240  # standaard usbipd-poort (exporter-zijde)
AGENT_DEFAULT_PORT = 5060  # archief-agent (exporter-zijde)

app = Flask(__name__)

# Thread lock voor progress file
_progress_lock = threading.Lock()


def _cleanup_stale_tasks():
    """Bij opstarten: markeer achtergebleven 'running' taken als 'interrupted'.

    Als de service herstart terwijl een scan liep, blijft progress.json
    op 'running' staan. Dit ruimt dat op zodat de GUI niet misleidt.
    """
    if not PROGRESS_FILE.exists():
        return
    try:
        with _progress_lock:
            progress = json.loads(PROGRESS_FILE.read_text())
            changed = False
            for task_id, info in progress.items():
                if info.get('status') == 'running':
                    info['status'] = 'interrupted'
                    info['message'] = f"Onderbroken (service herstart) — {info.get('message', '')}"
                    info['updated'] = datetime.now().isoformat()
                    changed = True
            # Verwijder oude afgeronde taken (alleen running/interrupted bewaren)
            progress = {k: v for k, v in progress.items()
                       if v.get('status') in ('running', 'interrupted')}
            PROGRESS_FILE.write_text(json.dumps(progress, indent=2))
        # Ook database opruimen
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute("""UPDATE scans SET status='interrupted', end_time=?
            WHERE status='running'""", (datetime.now().isoformat(),))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[STARTUP] Cleanup fout: {e}")


# Ruim stale taken op bij opstarten
_cleanup_stale_tasks()


# --- Donate-knop resolver (conform DEVs-Base .shared/donate contract) ---
_DONATE_CONTRACT_PATH = PROJECT_DIR / ".codex" / "contracts" / "donate-routes" / "donate-routes.v1.json"
_ARCHIVE_LABEL_RE = re.compile(r'^ARCHIVE-DISK-(\d{1,3})$', re.IGNORECASE)

def _resolve_donate_button():
    """Leest primaire donate-route uit donate-routes.v1.json (graceful fallback als afwezig).

    Retourneert {'url': str, 'label': str} of None.
    """
    try:
        if not _DONATE_CONTRACT_PATH.exists():
            return None
        data = json.loads(_DONATE_CONTRACT_PATH.read_text(encoding='utf-8'))
        routes = data.get('routes')
        if not isinstance(routes, list) or not routes:
            return None
        # Primaire route heeft voorkeur; anders eerste route
        candidates = [r for r in routes if isinstance(r, dict) and str(r.get('public_url', '')).strip()]
        if not candidates:
            return None
        route = next((r for r in candidates if r.get('is_primary') is True), candidates[0])
        url = str(route.get('public_url', '')).strip()
        if not url:
            return None
        # Lege label → JS gebruikt t('donate') voor taalafhankelijke knoptekst
        label = str(route.get('button_label', '')).strip()
        return {'url': url, 'label': label}
    except Exception:
        return None


def _canonicalize_archive_label(raw_label):
    """Normaliseer gebruikers- of volume-labels naar ARCHIVE-DISK-NNN.

    Voorbeelden:
    - ARCHIVE-DISK-7      -> ARCHIVE-DISK-007
    - archive disk 007    -> ARCHIVE-DISK-007
    - AD 007 WD2TB        -> ARCHIVE-DISK-007
    """
    if raw_label is None:
        return None
    label = str(raw_label).strip()
    if not label:
        return None

    match = _ARCHIVE_LABEL_RE.match(label)
    if match:
        return f"ARCHIVE-DISK-{int(match.group(1)):03d}"

    upper = label.upper()
    patterns = (
        r'\bARCHIVE[\s_-]*DISK[\s_-]*(\d{1,3})\b',
        r'\bAD[\s_-]*(\d{1,3})\b',
        r'\bARCHIEF[\s_-]*DISK[\s_-]*(\d{1,3})\b',
    )
    for pattern in patterns:
        match = re.search(pattern, upper)
        if match:
            return f"ARCHIVE-DISK-{int(match.group(1)):03d}"
    return None


def _lookup_archived_file_path(label, rel_path):
    """Zoek volledig pad en source_root op voor een gecatalogiseerd bestand."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    row = conn.execute(
        "SELECT source_root FROM files WHERE archive_label=? AND relative_path=? LIMIT 1",
        (label, rel_path)).fetchone()
    conn.close()
    if not row:
        return None, None
    source_root = row[0]
    return Path(source_root) / rel_path, source_root


def _path_within_archive_mount(path_value):
    """Controleer of een pad binnen het archief-mount-pad valt."""
    try:
        Path(path_value).relative_to(MOUNT_BASE)
        return True
    except ValueError:
        return False


def _open_with_default_app(target_path):
    """Probeer een bestand of map te openen met de standaard desktop-app."""
    commands = [
        ['xdg-open', str(target_path)],
        ['gio', 'open', str(target_path)],
    ]
    attempts = []
    for cmd in commands:
        if not shutil.which(cmd[0]):
            continue
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            clean = re.sub(r'\s+', ' ', f"{result.stdout} {result.stderr}").strip()
            if result.returncode == 0:
                return True, cmd[0], clean
            attempts.append(f"{cmd[0]}: {clean or f'rc={result.returncode}'}")
        except Exception as exc:
            attempts.append(f"{cmd[0]}: {exc}")
    if not attempts:
        attempts.append('geen desktop-open-commando beschikbaar')
    return False, None, '; '.join(attempts)


def _open_file_or_fallback_dir(full_path):
    """Open bestand direct; val terug op de map als dat niet lukt."""
    target = Path(full_path)
    if target.exists() and target.is_file():
        ok, launcher, detail = _open_with_default_app(target)
        if ok:
            return {
                'success': True,
                'opened': 'file',
                'launcher': launcher,
                'message': f'Bestand geopend via {launcher}.',
                'path': str(target),
            }
        parent = target.parent
        if parent.exists():
            ok_dir, launcher_dir, detail_dir = _open_with_default_app(parent)
            if ok_dir:
                return {
                    'success': True,
                    'opened': 'directory',
                    'launcher': launcher_dir,
                    'message': 'Geen directe standaard-app gevonden; map geopend als fallback.',
                    'path': str(parent),
                    'detail': detail or detail_dir,
                }
        return {
            'success': False,
            'message': detail or 'Openen met standaard-app mislukte.',
            'path': str(target),
        }
    if target.exists() and target.is_dir():
        ok, launcher, detail = _open_with_default_app(target)
        return {
            'success': ok,
            'opened': 'directory' if ok else None,
            'launcher': launcher,
            'message': f'Map geopend via {launcher}.' if ok else (detail or 'Map openen mislukt.'),
            'path': str(target),
        }
    return {'success': False, 'message': 'Pad bestaat niet.', 'path': str(target)}


def _find_partition_details(device):
    """Zoek lsblk-details op voor een specifieke partitie."""
    try:
        result = subprocess.run(
            ['lsblk', '-J', '-o',
             'NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,RM'],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        for dev in data.get('blockdevices', []):
            model = dev.get('model', '')
            serial = dev.get('serial', '')
            tran = dev.get('tran')
            dev_name = f"/dev/{dev.get('name')}"
            candidates = dev.get('children') or [dev]
            for child in candidates:
                child_name = f"/dev/{child.get('name')}"
                if child_name != device:
                    continue
                return {
                    'device': child_name,
                    'size': child.get('size', '0'),
                    'fstype': child.get('fstype'),
                    'label': child.get('label'),
                    'uuid': child.get('uuid'),
                    'mountpoint': child.get('mountpoint'),
                    'readonly': False,
                    'model': model,
                    'serial': serial,
                    'tran': tran,
                    'media_type': 'usb_hdd' if tran == 'usb' else 'unknown',
                    'parent_device': dev_name,
                }
    except Exception:
        return None
    return None


def _register_media_metadata(label, partition):
    """Leg volume-label/UUID/model ook vast als media nog niet gescand is."""
    if not label or not partition:
        return
    now = datetime.now().isoformat()
    vol_label = partition.get('label')
    filesystem_uuid = partition.get('uuid')
    device_model = partition.get('model')
    device_serial = partition.get('serial')
    filesystem_type = partition.get('fstype')
    media_type = partition.get('media_type') or 'unknown'
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        existing = conn.execute(
            "SELECT media_id FROM physical_media WHERE archive_label=?",
            (label,)).fetchone()
        if existing:
            conn.execute("""UPDATE physical_media
                SET last_seen=?,
                    media_type=COALESCE(NULLIF(media_type,''), ?),
                    volume_label=COALESCE(NULLIF(volume_label,''), ?),
                    filesystem_uuid=COALESCE(NULLIF(filesystem_uuid,''), ?),
                    device_model=COALESCE(NULLIF(device_model,''), ?),
                    device_serial=COALESCE(NULLIF(device_serial,''), ?),
                    filesystem_type=COALESCE(NULLIF(filesystem_type,''), ?)
                WHERE archive_label=?""",
                (now, media_type, vol_label, filesystem_uuid, device_model, device_serial,
                 filesystem_type, label))
        else:
            conn.execute("""INSERT INTO physical_media
                (archive_label, media_type, first_seen, last_seen, filesystem_uuid,
                 volume_label, device_model, device_serial, filesystem_type)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (label, media_type, now, now, filesystem_uuid, vol_label,
                 device_model, device_serial, filesystem_type))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _latest_source_root_for_label(label):
    """Pak het meest recente bekende source_root voor een archieflabel."""
    if not label:
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        row = conn.execute(
            "SELECT source_root FROM scans WHERE archive_label=? ORDER BY scan_id DESC LIMIT 1",
            (label,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT DISTINCT source_root FROM files WHERE archive_label=? LIMIT 1",
                (label,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _live_mountpoints_under(root_path):
    """Geef echte live mountpoints terug voor een archief-root of partitiepad."""
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        return []

    mounts = []
    try:
        if root.is_mount():
            return [str(root)]
    except OSError:
        return mounts

    # Alleen het canonieke disk-rootpad kan submounts van partities bevatten.
    if root.parent != MOUNT_BASE:
        return mounts

    try:
        for child in root.iterdir():
            try:
                if child.is_mount():
                    mounts.append(str(child))
            except OSError:
                continue
    except OSError:
        pass
    return mounts


def _archive_root_is_available(root_path):
    """Waar alleen als het pad echt een actieve mount is of actieve submounts bevat."""
    return bool(_live_mountpoints_under(root_path))


def _describe_scan_source(label, source_root):
    """Maak een mensvriendelijke bronbeschrijving voor scan-overzichten."""
    source_root = str(source_root or '').strip()
    if not source_root:
        return {
            'source_display': '-',
            'source_detail': '',
            'source_parts': [],
        }

    disk_root = MOUNT_BASE / str(label or '').strip()
    disk_root_str = str(disk_root)
    if source_root == disk_root_str:
        live_mounts = _live_mountpoints_under(disk_root)
        part_mounts = [p for p in live_mounts if p != disk_root_str]
        part_names = [Path(p).name for p in part_mounts]
        if part_names:
            return {
                'source_display': f'{label} ({len(part_names)} partities)',
                'source_detail': ', '.join(part_mounts),
                'source_parts': part_names,
            }
        return {
            'source_display': str(label or source_root),
            'source_detail': source_root,
            'source_parts': [],
        }

    prefix = disk_root_str + '/'
    if source_root.startswith(prefix):
        rel = source_root[len(prefix):]
        return {
            'source_display': f'{label}/{rel}',
            'source_detail': source_root,
            'source_parts': [rel],
        }

    return {
        'source_display': '/'.join(source_root.strip('/').split('/')[-2:]) or source_root,
        'source_detail': source_root,
        'source_parts': [],
    }


def _umount_mount_stack(path_value, max_attempts=8):
    """Unmount een pad volledig, ook als meerdere mounts op exact hetzelfde target liggen."""
    path = str(path_value)
    attempts = 0
    while attempts < max_attempts:
        try:
            if not Path(path).is_mount():
                return {'success': True, 'count': attempts}
        except OSError:
            return {'success': True, 'count': attempts}
        result = subprocess.run(
            ['sudo', 'umount', path],
            capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            clean = re.sub(r'\033\[[0-9;]*m', '', result.stdout + result.stderr).strip()
            return {
                'success': False,
                'count': attempts,
                'message': clean or f'umount mislukt voor {path}'
            }
        attempts += 1
    try:
        still_mounted = Path(path).is_mount()
    except OSError:
        still_mounted = False
    if still_mounted:
        return {
            'success': False,
            'count': attempts,
            'message': f'{path} bleef gemount na {attempts} umount-pogingen'
        }
    return {'success': True, 'count': attempts}


def _read_progress_state():
    if not PROGRESS_FILE.exists():
        return {}
    try:
        with _progress_lock:
            return json.loads(PROGRESS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _find_progress_task_for_label(label, prefixes=None, statuses=('running',)):
    """Vind de meest recente taak voor een label, optioneel gefilterd op prefix/status."""
    matches = []
    for task_id, info in _read_progress_state().items():
        if info.get('status') not in statuses:
            continue
        details = info.get('details') or {}
        if details.get('label') != label:
            continue
        if prefixes and not any(task_id.startswith(prefix) for prefix in prefixes):
            continue
        matches.append((task_id, info))
    if not matches:
        return None
    matches.sort(key=lambda item: item[1].get('updated', ''), reverse=True)
    task_id, info = matches[0]
    payload = dict(info)
    payload['task_id'] = task_id
    return payload


def _collect_connected_partitions_for_label(label):
    """Vind alle aangesloten ingestable partities die bij een archieflabel horen."""
    partitions = []
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        result = subprocess.run(
            ['lsblk', '-J', '-o',
             'NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,RM'],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            conn.close()
            return []
        data = json.loads(result.stdout)
        for dev in data.get('blockdevices', []):
            if dev.get('tran') != 'usb':
                continue
            model = dev.get('model', '')
            serial = dev.get('serial', '')
            for child in (dev.get('children') or [dev]):
                if child.get('type') not in ('part', 'disk') or not child.get('fstype'):
                    continue
                mp = child.get('mountpoint')
                readonly = False
                if mp:
                    try:
                        mnt_out = subprocess.run(
                            ['findmnt', '-n', '-o', 'OPTIONS', mp],
                            capture_output=True, text=True, timeout=5)
                        readonly = 'ro' in mnt_out.stdout.split(',') if mnt_out.returncode == 0 else False
                    except Exception:
                        pass
                part = {
                    'device': f"/dev/{child['name']}",
                    'size': child.get('size', '0'),
                    'fstype': child.get('fstype'),
                    'label': child.get('label'),
                    'uuid': child.get('uuid'),
                    'mountpoint': mp,
                    'readonly': readonly,
                    'model': model,
                    'serial': serial,
                    'media_type': 'usb_hdd',
                    'parent_device': f"/dev/{dev.get('name')}",
                }
                known = _lookup_known_label(conn, part, model)
                canonical_volume = _canonicalize_archive_label(part.get('label'))
                if known.get('known_label') == label or canonical_volume == label:
                    part.update(known)
                    partitions.append(part)
        conn.close()
    except Exception:
        return []
    return sorted(partitions, key=lambda part: part.get('device', ''))


def _expected_archive_mounts_for_label(label, partitions):
    """Bepaal de canonieke mountpaden per ingestable partitie voor dit label."""
    ingestable = [p for p in partitions if _is_ingestable_partition(p)]
    expected = {}
    disk_base = MOUNT_BASE / label
    used_names = set()
    multi_part = len(ingestable) > 1
    for part in sorted(ingestable, key=lambda p: p.get('device', '')):
        mount_path = disk_base if not multi_part else (disk_base / _partition_mount_name(part, used_names))
        expected[part['device']] = str(mount_path)
    return expected


def _known_label_mount_state(label):
    """Beschrijf of een bekende fysieke schijf logisch/correct op het archief-pad staat."""
    partitions = _collect_connected_partitions_for_label(label)
    expected_mounts = _expected_archive_mounts_for_label(label, partitions)
    problems = []
    valid = bool(expected_mounts)
    disk_root = MOUNT_BASE / label
    try:
        root_is_mount = disk_root.is_mount()
    except OSError:
        root_is_mount = False

    if len(expected_mounts) > 1 and root_is_mount:
        valid = False
        problems.append(f'{disk_root} is direct gemount terwijl submappen verwacht zijn')

    for part in partitions:
        if not _is_ingestable_partition(part):
            continue
        expected = expected_mounts.get(part['device'])
        actual = part.get('mountpoint')
        if not part.get('readonly') or actual != expected:
            valid = False
            problems.append(f"{part['device']} -> {actual or 'niet gemount'} (verwacht {expected})")

    if expected_mounts and not _archive_root_is_available(disk_root):
        valid = False
        problems.append(f'geen actieve mount onder {disk_root}')

    return {
        'label': label,
        'connected': bool(partitions),
        'partitions': partitions,
        'expected_mounts': expected_mounts,
        'valid': valid,
        'problems': problems,
    }


def _guess_parent_block_device(device):
    """Herleid een partitie-device naar het fysieke blokdevice."""
    if not device or not re.match(r'^/dev/[a-zA-Z0-9]+$', str(device)):
        return None
    dev = str(device).strip()
    try:
        pdev_out = subprocess.run(['lsblk', '-no', 'PKNAME', dev],
            capture_output=True, text=True, timeout=5)
        if pdev_out.returncode == 0 and pdev_out.stdout.strip():
            return '/dev/' + pdev_out.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    name = dev.split('/')[-1]
    match = re.match(r'^(?P<base>.+?)(?:p?\d+)?$', name)
    if not match:
        return dev
    return '/dev/' + match.group('base')


def _power_off_block_device(device):
    """Probeer een fysiek blokdevice echt uit te schakelen na unmount."""
    parent_device = _guess_parent_block_device(device)
    if not parent_device:
        return {
            'attempted': False,
            'success': False,
            'powered_off': False,
            'already_absent': False,
            'message': 'Geen parent-device beschikbaar'
        }
    if not Path(parent_device).exists():
        return {
            'attempted': True,
            'success': True,
            'powered_off': False,
            'already_absent': True,
            'device': parent_device,
            'message': f'{parent_device} is al niet meer aanwezig'
        }

    attempts = []
    commands = [
        ['sudo', 'udisksctl', 'power-off', '-b', parent_device],
        ['sudo', 'eject', parent_device],
    ]
    for cmd in commands:
        tool_name = cmd[1]
        if not shutil.which(tool_name):
            continue
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            clean = re.sub(r'\033\[[0-9;]*m', '', f"{result.stdout} {result.stderr}").strip()
            if result.returncode == 0:
                time.sleep(1.0)
                still_present = Path(parent_device).exists()
                return {
                    'attempted': True,
                    'success': not still_present,
                    'powered_off': not still_present,
                    'already_absent': False,
                    'device': parent_device,
                    'message': clean or f'{parent_device} uitgeschakeld via {tool_name}',
                }
            attempts.append(f'{tool_name}: {clean or f"rc={result.returncode}"}')
        except Exception as exc:
            attempts.append(f'{tool_name}: {exc}')

    if not Path(parent_device).exists():
        return {
            'attempted': True,
            'success': True,
            'powered_off': True,
            'already_absent': False,
            'device': parent_device,
            'message': f'{parent_device} is verdwenen na eject/power-off',
        }
    return {
        'attempted': bool(commands),
        'success': False,
        'powered_off': False,
        'already_absent': False,
        'device': parent_device,
        'message': '; '.join(attempts) or f'Power-off mislukt voor {parent_device}',
    }


def _prepare_connected_known_disk(label):
    """Koppel een bekende fysieke schijf opnieuw op de canonieke archief-layout."""
    state = _known_label_mount_state(label)
    expected_mounts = state['expected_mounts']
    if not expected_mounts:
        return {
            'success': False,
            'message': f'Geen aangesloten scanbare partities gevonden voor {label}.'
        }

    try:
        disk_root = MOUNT_BASE / label
        disk_root.mkdir(parents=True, exist_ok=True)

        # Eerst alle bestaande archief-mounts onder dit label losmaken.
        if disk_root.exists() and disk_root.is_dir():
            for sub in sorted(disk_root.iterdir(), reverse=True):
                try:
                    if sub.is_mount():
                        unmounted = _umount_mount_stack(sub)
                        if not unmounted.get('success'):
                            return {
                                'success': False,
                                'message': f'Unmount mislukt voor {sub}: {unmounted.get("message", "")}'
                            }
                except OSError:
                    pass
        try:
            if disk_root.is_mount():
                unmounted = _umount_mount_stack(disk_root)
                if not unmounted.get('success'):
                    return {
                        'success': False,
                        'message': f'Unmount mislukt voor {disk_root}: {unmounted.get("message", "")}'
                    }
        except OSError:
            pass

        mounted_paths = []
        for part in state['partitions']:
            if not _is_ingestable_partition(part):
                continue
            device = part['device']
            target = expected_mounts[device]
            current_mount = part.get('mountpoint')
            if current_mount and current_mount != target:
                unmounted = _umount_mount_stack(current_mount)
                if not unmounted.get('success'):
                    return {
                        'success': False,
                        'message': f'Unmount mislukt voor {current_mount}: {unmounted.get("message", "")}'
                    }
            mkdir_result = subprocess.run(['sudo', 'mkdir', '-p', target],
                capture_output=True, text=True, timeout=5)
            if mkdir_result.returncode != 0:
                clean = re.sub(r'\033\[[0-9;]*m', '', mkdir_result.stdout + mkdir_result.stderr).strip()
                return {
                    'success': False,
                    'message': f'Map maken mislukt voor {target}: {clean[-300:]}'
                }
            result = subprocess.run(
                ['sudo', 'mount', '-o', 'ro,noexec,nosuid,nodev', device, target],
                capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                clean = re.sub(r'\033\[[0-9;]*m', '', result.stdout + result.stderr).strip()
                return {
                    'success': False,
                    'message': f'Mount mislukt voor {device} -> {target}: {clean[-300:]}'
                }
            mounted_paths.append(target)
            _register_media_metadata(label, _find_partition_details(device) or part)

        final_state = _known_label_mount_state(label)
        if not final_state.get('valid'):
            return {
                'success': False,
                'message': f'{label} bleef inconsistent gekoppeld: {"; ".join(final_state.get("problems") or [])}'
            }
        return {
            'success': True,
            'message': f'{label} correct gekoppeld op {len(mounted_paths)} archiefpad(en).',
            'mounted_paths': mounted_paths,
        }
    except Exception as e:
        return {'success': False, 'message': str(e)}


def _ensure_archive_label_mounted(label, source_root=None):
    """Zorg dat een bekende aangesloten schijf op het canonieke archief-pad staat.

    Probeert ook self-healing vanaf /media/devmon/... of een niet-canonieke RO mount.
    """
    expected_root = Path(source_root) if source_root else (MOUNT_BASE / label)
    state = _known_label_mount_state(label)
    if state['connected']:
        if not state['valid']:
            prep = _prepare_connected_known_disk(label)
            if not prep.get('success'):
                return None
            state = _known_label_mount_state(label)
        if state['valid']:
            if _archive_root_is_available(expected_root):
                return str(expected_root)
            if _archive_root_is_available(MOUNT_BASE / label):
                return str(MOUNT_BASE / label)
    elif _archive_root_is_available(expected_root):
        return str(expected_root)

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        result = subprocess.run(
            ['lsblk', '-J', '-o',
             'NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,RM'],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            conn.close()
            return None
        data = json.loads(result.stdout)
        selected = None
        for dev in data.get('blockdevices', []):
            model = dev.get('model', '')
            if dev.get('tran') != 'usb':
                continue
            for child in (dev.get('children') or [dev]):
                if child.get('type') not in ('part', 'disk') or not child.get('fstype'):
                    continue
                mp = child.get('mountpoint')
                readonly = False
                if mp:
                    try:
                        mnt_out = subprocess.run(
                            ['findmnt', '-n', '-o', 'OPTIONS', mp],
                            capture_output=True, text=True, timeout=5)
                        readonly = 'ro' in mnt_out.stdout.split(',') if mnt_out.returncode == 0 else False
                    except Exception:
                        pass
                part = {
                    'device': f"/dev/{child['name']}",
                    'label': child.get('label'),
                    'uuid': child.get('uuid'),
                    'fstype': child.get('fstype'),
                    'mountpoint': mp,
                    'readonly': readonly,
                    'model': model,
                    'serial': dev.get('serial', ''),
                    'media_type': 'usb_hdd',
                }
                known = _lookup_known_label(conn, part, model)
                canonical_volume = _canonicalize_archive_label(part.get('label'))
                if known.get('known_label') == label or canonical_volume == label:
                    selected = part
                    break
            if selected:
                break
        conn.close()

        if not selected:
            return None

        expected_mount = MOUNT_BASE / label
        if str(selected.get('mountpoint') or '') != str(expected_mount) or not selected.get('readonly'):
            subprocess.run(
                [str(PROJECT_DIR / 'mount_readonly.sh'), selected['device'], label],
                capture_output=True, text=True, timeout=30)
        selected = _find_partition_details(selected['device']) or selected
        _register_media_metadata(label, selected)

        if _archive_root_is_available(expected_root):
            return str(expected_root)
        if expected_mount != expected_root and _archive_root_is_available(expected_mount):
            return str(expected_mount)
    except Exception as e:
        print(f"[STARTUP] Mount self-heal fout voor {label}: {e}")
    return None


def _startup_autoresume():
    """Hervat onderbroken scan/index taken als de schijf weer beschikbaar is."""
    if not AUTO_RESUME_ON_STARTUP:
        print("[STARTUP] Auto-resume staat uit; onderbroken taken wachten op handmatige hervatting.")
        return
    if not PROGRESS_FILE.exists():
        return
    try:
        with _progress_lock:
            progress = json.loads(PROGRESS_FILE.read_text())
        interrupted = []
        for task_id, info in progress.items():
            if info.get('status') != 'interrupted':
                continue
            details = info.get('details') or {}
            label = details.get('label')
            if not label:
                continue
            interrupted.append((task_id, info, details))
        interrupted.sort(key=lambda item: item[1].get('updated', ''), reverse=True)

        resumed_labels = set()
        for old_task_id, info, details in interrupted:
            label = details.get('label')
            if not label or label in resumed_labels:
                continue
            phase = (details.get('fase') or '').strip().lower()
            source_root = details.get('source_root') or _latest_source_root_for_label(label)
            mounted_root = _ensure_archive_label_mounted(label, source_root)
            if not mounted_root:
                continue

            timestamp = datetime.now().strftime('%H%M%S')
            if old_task_id.startswith('index_') or phase in ('indexeren', 'index_config', 'index_fout', 'index_timeout'):
                new_task_id = f"index_{label}_{timestamp}"
                print(f"[STARTUP] Hervat indexering voor {label} via {mounted_root}")
                threading.Thread(target=_run_index, args=(label, new_task_id), daemon=True).start()
            elif old_task_id.startswith('scan_') or phase in ('voorbereiding', 'tellen', 'tellen_klaar', 'resume', 'scannen', 'scan_klaar'):
                new_task_id = f"scan_{label}_{timestamp}"
                print(f"[STARTUP] Hervat scan voor {label} via {mounted_root}")
                threading.Thread(target=_run_scan, args=(label, mounted_root, new_task_id), daemon=True).start()
            else:
                continue
            resumed_labels.add(label)
    except Exception as e:
        print(f"[STARTUP] Auto-resume fout: {e}")



def _update_progress(task_id, status, message, percent=0, details=None):
    """Schrijf voortgang naar JSON bestand voor polling."""
    with _progress_lock:
        progress = {}
        if PROGRESS_FILE.exists():
            try:
                progress = json.loads(PROGRESS_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        existing = progress.get(task_id, {})
        log = existing.get('log', [])
        # Voeg significante berichten toe aan log
        if message and message != existing.get('message'):
            log.append({'time': datetime.now().strftime('%H:%M:%S'), 'msg': message})
            if len(log) > 200:
                log = log[-200:]
        progress[task_id] = {
            'status': status,
            'message': message,
            'percent': percent,
            'details': details or existing.get('details', {}),
            'log': log,
            'updated': datetime.now().isoformat(),
        }
        PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


def _write_scan_log(label, lines):
    """Schrijf scan log naar bestand."""
    log_file = LOG_DIR / f"scan_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file.write_text('\n'.join(lines))
    return str(log_file)


def _run_scan(label, source_dir, task_id):
    """Draai metadata scan in achtergrond-thread."""
    log_lines = [f"=== Scan gestart: {label} ===", f"Bron: {source_dir}", f"Start: {datetime.now().isoformat()}", ""]
    try:
        _update_progress(task_id, 'running', f'Fase: voorbereiding voor {label}...', 3,
                         {'fase': 'voorbereiding', 'label': label})

        import yaml
        with open(PROJECT_DIR / "config" / "config.yaml") as f:
            config = yaml.safe_load(f)

        _update_progress(task_id, 'running', f'Fase: bestanden tellen in {label}...', 5,
                         {'fase': 'tellen', 'label': label})

        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        exclude_dirs = set(config.get('exclude_dirs', []))
        follow_symlinks = config.get('follow_symlinks', False)

        all_extensions = set()
        for group_exts in config.get('include_extensions', {}).values():
            all_extensions.update(ext.lower() for ext in group_exts)

        # Registreer medium
        now = datetime.now().isoformat()
        existing = conn.execute(
            "SELECT media_id FROM physical_media WHERE archive_label = ?",
            (label,)).fetchone()
        if existing:
            media_id = existing[0]
            conn.execute("UPDATE physical_media SET last_seen = ? WHERE media_id = ?",
                         (now, media_id))
        else:
            conn.execute("""INSERT INTO physical_media
                (archive_label, media_type, first_seen, last_seen)
                VALUES (?, 'unknown', ?, ?)""", (label, now, now))
            media_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        # Maak scan record
        conn.execute("""INSERT INTO scans
            (media_id, archive_label, source_root, start_time, status)
            VALUES (?, ?, ?, ?, 'running')""",
            (media_id, label, source_dir, now))
        scan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        # Tel bestanden (met tussentijdse updates elke 5000 bestanden)
        total_files = 0
        total_dirs = 0
        last_count_update = time.time()
        for dirpath, dirnames, filenames in os.walk(source_dir, followlinks=follow_symlinks):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
            total_files += len(filenames)
            total_dirs += 1
            # Tussentijdse update zodat stale-detectie niet afgaat
            if time.time() - last_count_update > 10:
                _update_progress(task_id, 'running',
                    f'Fase: bestanden tellen — {total_files} bestanden in {total_dirs} mappen tot nu toe...',
                    5, {'fase': 'tellen', 'label': label,
                        'total_files_sofar': total_files, 'total_dirs_sofar': total_dirs})
                last_count_update = time.time()

        log_lines.append(f"Totaal: {total_files} bestanden in {total_dirs} mappen")
        _update_progress(task_id, 'running',
            f'Fase: tellen klaar — {total_files} bestanden in {total_dirs} mappen', 8,
            {'fase': 'tellen_klaar', 'label': label, 'total_files': total_files, 'total_dirs': total_dirs})

        if total_files == 0:
            log_lines.append("Geen bestanden gevonden.")
            log_file = _write_scan_log(label, log_lines)
            _update_progress(task_id, 'completed', f'{label}: geen bestanden gevonden', 100,
                             {'files_ok': 0, 'files_error': 0, 'log_file': log_file, 'label': label})
            conn.execute("UPDATE scans SET status='completed', end_time=? WHERE scan_id=?",
                         (datetime.now().isoformat(), scan_id))
            conn.commit()
            conn.close()
            return

        import sys
        sys.path.insert(0, str(PROJECT_DIR / "scripts"))
        from extract_metadata import (
            extract_pdf_metadata, extract_office_metadata, extract_image_metadata,
            determine_original_content_date, get_extension_group, human_size
        )

        files_ok = 0
        files_error = 0
        total_bytes = 0
        batch = 0
        root_path = Path(source_dir)
        seen_paths = set()
        error_files = []
        current_dir_name = ""

        # Resume: check welke bestanden al gescand zijn voor dit label+source
        already_scanned = set()
        existing_rows = conn.execute(
            "SELECT relative_path FROM files WHERE archive_label = ? AND source_root = ?",
            (label, source_dir)).fetchall()
        already_scanned = {row[0] for row in existing_rows}
        files_skipped = 0
        is_resume = len(already_scanned) > 0

        if is_resume:
            log_lines.append(f"Resume: {len(already_scanned)} bestanden al gescand, worden overgeslagen")
            _update_progress(task_id, 'running',
                f'Fase: resume — {len(already_scanned)} bestanden al gescand, starten waar gebleven...',
                2, {'fase': 'resume', 'label': label, 'already_scanned': len(already_scanned),
                    'total_files': total_files})

        _update_progress(task_id, 'running',
            f'Fase: metadata scannen — 0/{total_files} bestanden...', 0,
            {'fase': 'scannen', 'label': label, 'files_ok': 0, 'files_error': 0,
             'total_files': total_files, 'files_skipped': files_skipped,
             'current_dir': '', 'current_file': ''})

        for dirpath, dirnames, filenames in os.walk(source_dir, followlinks=follow_symlinks):
            current = Path(dirpath)
            real_path = current.resolve()
            if real_path in seen_paths:
                dirnames.clear()
                continue
            seen_paths.add(real_path)
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]

            try:
                current_dir_name = str(current.relative_to(root_path))
            except ValueError:
                current_dir_name = str(current)

            for filename in filenames:
                filepath = current / filename
                processed = files_ok + files_error + files_skipped

                try:
                    ext = filepath.suffix.lstrip('.').lower() if filepath.suffix else ''
                    relative = str(filepath.relative_to(root_path))

                    # Resume: sla al-gescande bestanden over
                    if relative in already_scanned:
                        files_skipped += 1
                        continue

                    try:
                        stat = filepath.stat()
                        size = stat.st_size
                    except OSError as e:
                        files_error += 1
                        error_files.append(f"STAT_FOUT: {relative} — {e}")
                        continue

                    total_bytes += size
                    ext_group = get_extension_group(ext)

                    fs_dates = {}
                    try:
                        fs_dates['filesystem_modified_time'] = datetime.fromtimestamp(stat.st_mtime).isoformat()
                        fs_dates['filesystem_accessed_time'] = datetime.fromtimestamp(stat.st_atime).isoformat()
                        fs_dates['filesystem_created_time'] = datetime.fromtimestamp(stat.st_ctime).isoformat()
                    except (OSError, OverflowError):
                        pass

                    file_meta = {}
                    if ext in all_extensions:
                        try:
                            if ext == 'pdf':
                                file_meta = extract_pdf_metadata(str(filepath))
                            elif ext in ('docx',):
                                file_meta = extract_office_metadata(str(filepath))
                            elif ext in ('xlsx', 'xlsm'):
                                file_meta = extract_office_metadata(str(filepath))
                            elif ext in ('jpg', 'jpeg', 'png', 'gif', 'tiff', 'bmp', 'heic', 'webp'):
                                file_meta = extract_image_metadata(str(filepath))
                        except Exception as e:
                            error_files.append(f"META_FOUT: {relative} — {e}")

                    combined = {**fs_dates, **file_meta, 'filename': filename}
                    content_date, date_source, date_confidence = determine_original_content_date(combined)

                    conn.execute("""INSERT INTO files (
                        scan_id, media_id, archive_label, source_root,
                        full_path, relative_path, parent_dir, filename,
                        extension, extension_group, size_bytes, human_size,
                        filesystem_created_time, filesystem_modified_time, filesystem_accessed_time,
                        document_created_time, document_modified_time, original_content_date,
                        author, creator, last_modified_by, company,
                        title, subject, keywords, producer, application,
                        date_source, metadata_source, date_confidence,
                        availability_status, original_device_label, original_mountpoint,
                        last_seen_online, readable, metadata_extracted,
                        error_message, scan_checkpoint_batch
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?,
                        1, ?, ?, ?
                    )""", (
                        scan_id, media_id, label, source_dir,
                        str(filepath), relative, str(current), filename,
                        ext, ext_group, size, human_size(size),
                        fs_dates.get('filesystem_created_time'),
                        fs_dates.get('filesystem_modified_time'),
                        fs_dates.get('filesystem_accessed_time'),
                        file_meta.get('document_created_time'),
                        file_meta.get('document_modified_time'),
                        content_date,
                        file_meta.get('author'), file_meta.get('creator'),
                        file_meta.get('last_modified_by'), file_meta.get('company'),
                        file_meta.get('title'), file_meta.get('subject'),
                        file_meta.get('keywords'), file_meta.get('producer'),
                        file_meta.get('application'),
                        date_source, file_meta.get('metadata_source'), date_confidence,
                        label, source_dir, datetime.now().isoformat(),
                        1 if file_meta and 'error_message' not in file_meta else 0,
                        file_meta.get('error_message'), batch
                    ))
                    files_ok += 1
                except Exception as e:
                    files_error += 1
                    error_files.append(f"INSERT_FOUT: {filename} — {e}")

                # Update voortgang elke 10 bestanden
                if (files_ok + files_error) % 10 == 0:
                    file_pct = round((processed / max(total_files, 1)) * 100, 1)
                    resume_msg = f' (hervatting, {files_skipped} overgeslagen)' if files_skipped > 0 else ''
                    _update_progress(task_id, 'running',
                        f'Fase: scannen — {processed}/{total_files} bestanden{resume_msg} | map: {current_dir_name}',
                        file_pct, {'fase': 'scannen', 'label': label,
                              'files_ok': files_ok, 'files_error': files_error,
                              'files_skipped': files_skipped,
                              'total_files': total_files, 'total_bytes': total_bytes,
                              'current_dir': current_dir_name, 'current_file': filename})

            batch += 1
            if batch % 10 == 0:
                conn.commit()

        # Afronden
        conn.execute("""UPDATE scans SET
            end_time=?, number_of_files=?, total_bytes=?,
            files_ok=?, files_error=?, status='completed'
            WHERE scan_id=?""",
            (datetime.now().isoformat(), files_ok + files_error,
             total_bytes, files_ok, files_error, scan_id))
        conn.commit()
        conn.close()

        log_lines.append(f"Resultaat: {files_ok} OK, {files_error} fouten")
        log_lines.append(f"Totaal: {human_size(total_bytes)}")
        log_lines.append(f"Einde: {datetime.now().isoformat()}")
        if error_files:
            log_lines.append("")
            log_lines.append("=== Fouten ===")
            log_lines.extend(error_files)
        log_file = _write_scan_log(label, log_lines)

        _update_progress(task_id, 'completed',
            f'{label}: {files_ok} bestanden gescand, {files_error} fouten', 100,
            {'fase': 'scan_klaar', 'label': label,
             'files_ok': files_ok, 'files_error': files_error,
             'total_bytes': total_bytes, 'log_file': log_file})

    except Exception as e:
        log_lines.append(f"FATALE FOUT: {e}")
        log_file = _write_scan_log(label, log_lines)
        _update_progress(task_id, 'failed', f'Fout bij {label}: {e}', 0,
                         {'log_file': log_file, 'label': label})


def _run_index(label, task_id):
    """Draai Recoll indexering in achtergrond-thread."""
    try:
        source_dir = MOUNT_BASE / label
        index_dir = INDEX_BASE / label
        index_dir.mkdir(parents=True, exist_ok=True)

        _update_progress(task_id, 'running',
            f'Fase: Recoll config genereren voor {label}...', 10,
            {'fase': 'index_config', 'label': label})

        conf = index_dir / "recoll.conf"
        conf.write_text(
            f"topdirs = {source_dir}\n"
            f"zipSkippedNames =\n"
            f"zipMaxMBs = 500\n"
            f"compressedfilemaxkbs = 500000\n"
            f"skippedNames = .git node_modules $RECYCLE.BIN RECYCLER System Volume Information\n"
            f"indexallfilenames = 1\n"
            f"loglevel = 3\n"
            f"logfilename = {index_dir}/recoll.log\n"
            f"dbdir = {index_dir}/xapiandb\n"
        )

        _update_progress(task_id, 'running',
            f'Fase: Recoll full-text index bouwen voor {label} (dit kan lang duren bij grote schijven)...', 15,
            {'fase': 'indexeren', 'label': label})

        result = subprocess.run(
            ['nice', '-n', '19', 'ionice', '-c', '3', 'recollindex', '-c', str(index_dir)],
            capture_output=True, text=True, timeout=7200
        )

        if result.returncode == 0:
            _update_progress(task_id, 'completed',
                f'{label}: full-text index gebouwd', 100,
                {'fase': 'index_klaar', 'label': label})
        else:
            _update_progress(task_id, 'failed',
                f'Indexering {label} mislukt: {result.stderr[:300]}', 0,
                {'fase': 'index_fout', 'label': label})

    except subprocess.TimeoutExpired:
        _update_progress(task_id, 'failed',
            f'{label}: indexering timeout (>2 uur)', 0,
            {'fase': 'index_timeout', 'label': label})
    except Exception as e:
        _update_progress(task_id, 'failed', f'{label}: fout: {e}', 0,
                         {'fase': 'index_fout', 'label': label})


_NON_INGESTABLE_FSTYPES = {
    'swap', 'linux-swap', 'linux-swap(v1)', 'crypto_luks', 'lvm2_member'
}


def _is_ingestable_partition(part):
    """Bepaal of een partitie zinvol scan/index-bronmateriaal kan bevatten."""
    fstype = str((part or {}).get('fstype') or '').strip().lower()
    return bool(fstype) and fstype not in _NON_INGESTABLE_FSTYPES


def _sanitize_mount_component(value):
    value = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value or '').strip())
    return value.strip('._-') or 'partitie'


def _partition_mount_name(part, used_names):
    """Kies een unieke submapnaam per partitie onder een fysiek disklabel."""
    device_name = str(part.get('device') or 'part').split('/')[-1]
    label_name = _sanitize_mount_component(part.get('label') or '')
    candidates = []
    if label_name:
        candidates.append(label_name)
        candidates.append(f'{label_name}__{device_name}')
    candidates.append(device_name)
    for candidate in candidates:
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
    base = candidates[0]
    suffix = 2
    while f'{base}-{suffix}' in used_names:
        suffix += 1
    final_name = f'{base}-{suffix}'
    used_names.add(final_name)
    return final_name


def _run_batch_ingest(disks, task_id):
    """Verwerk fysieke schijven (elk met meerdere partities) met tussenverslagen.

    Elke disk krijgt EEN label. Partities worden gemount als subdirectories:
    /mnt/archive-ingest/{LABEL}/{partitie_label}/
    """
    total = len(disks)
    results = []

    for i, disk in enumerate(disks, 1):
        label = disk['label']
        media_type = disk.get('media_type', 'usb_hdd')
        raw_partitions = disk.get('partitions', [])
        partitions = [p for p in raw_partitions if _is_ingestable_partition(p)]
        disk_pct_base = int(((i - 1) / total) * 100)
        disk_pct_end = int((i / total) * 100)

        _update_progress(task_id, 'running',
            f'Schijf {i}/{total}: {label} — {len(partitions)} partities mounten...',
            disk_pct_base + 1,
            {'fase': 'mount', 'label': label, 'disk_nr': i, 'disk_total': total,
             'results': results})

        if not partitions:
            results.append({
                'label': label,
                'status': 'fout',
                'fase': 'mount',
                'message': 'Geen scanbare partities gevonden; technische partities zoals swap worden overgeslagen.'
            })
            _update_progress(task_id, 'running',
                f'Schijf {i}/{total}: {label} heeft geen scanbare partities',
                disk_pct_end,
                {'fase': 'mount', 'label': label, 'disk_nr': i, 'disk_total': total,
                 'results': results})
            continue

        skipped_count = len(raw_partitions) - len(partitions)
        if skipped_count > 0:
            results.append({
                'label': label,
                'status': 'ok',
                'fase': 'mount',
                'message': f'{skipped_count} technische partitie(s) overgeslagen (bijv. swap).'
            })

        # Registreer media
        conn = sqlite3.connect(str(DB_PATH))
        now = datetime.now().isoformat()
        existing = conn.execute("SELECT media_id FROM physical_media WHERE archive_label=?",
                                (label,)).fetchone()
        model = disk.get('model', '')
        # Verzamel volume-labels en UUIDs van alle partities
        part_labels = [p.get('label') for p in partitions if p.get('label')]
        part_uuids  = [p.get('uuid')  for p in partitions if p.get('uuid')]
        vol_label_str = ','.join(part_labels) if part_labels else None
        uuid_str      = ','.join(part_uuids)  if part_uuids  else None
        if existing:
            conn.execute("""UPDATE physical_media
                SET media_type=?, last_seen=?, device_model=?,
                    volume_label=COALESCE(NULLIF(volume_label,''), ?),
                    filesystem_uuid=COALESCE(NULLIF(filesystem_uuid,''), ?)
                WHERE archive_label=?""",
                (media_type, now, model, vol_label_str, uuid_str, label))
        else:
            conn.execute("""INSERT INTO physical_media
                (archive_label, media_type, first_seen, last_seen, device_model, volume_label, filesystem_uuid)
                VALUES (?,?,?,?,?,?,?)""", (label, media_type, now, now, model, vol_label_str, uuid_str))
        conn.commit()
        conn.close()

        # Stap 1: Mount alle partities
        disk_base = MOUNT_BASE / label
        mount_ok = 0
        mount_err = 0
        source_dirs = []
        used_mount_names = set()

        for part in partitions:
            device = part['device']
            part_label = part.get('label') or part['device'].split('/')[-1]
            part_mount = str(disk_base / _partition_mount_name(part, used_mount_names))

            # Skip als al gemount op de juiste plek
            if part.get('mountpoint') == part_mount and part.get('readonly'):
                source_dirs.append(part_mount)
                mount_ok += 1
                continue

            try:
                subprocess.run(['sudo', 'mkdir', '-p', part_mount],
                    capture_output=True, timeout=5)

                # Detecteer fstype
                fstype_out = subprocess.run(['sudo', 'blkid', '-s', 'TYPE', '-o', 'value', device],
                    capture_output=True, text=True, timeout=5)
                fstype = fstype_out.stdout.strip() if fstype_out.returncode == 0 else ''
                if not fstype:
                    # Check of het een heel device is i.p.v. een partitie
                    lsblk_out = subprocess.run(['lsblk', '-n', '-o', 'TYPE', device],
                        capture_output=True, text=True, timeout=5)
                    dev_type = lsblk_out.stdout.strip().split('\n')[0].strip() if lsblk_out.returncode == 0 else ''
                    if dev_type == 'disk':
                        mount_err += 1
                        results.append({'label': f'{label}/{part_label}', 'status': 'fout',
                            'fase': 'mount', 'message': f'{device} is een heel device, geen partitie. Gebruik bijv. {device}1'})
                        continue
                    fstype = 'auto'

                # Unmount als al ergens anders gemount
                existing_mp = subprocess.run(['findmnt', '-n', '-o', 'TARGET', device],
                    capture_output=True, text=True, timeout=5)
                if existing_mp.returncode == 0 and existing_mp.stdout.strip():
                    for mp in existing_mp.stdout.strip().split('\n'):
                        mp = mp.strip()
                        if mp and mp != part_mount:
                            subprocess.run(['sudo', 'umount', mp],
                                capture_output=True, timeout=10)

                uid_out = subprocess.run(['id', '-u'], capture_output=True, text=True, timeout=3)
                gid_out = subprocess.run(['id', '-g'], capture_output=True, text=True, timeout=3)
                uid = uid_out.stdout.strip()
                gid = gid_out.stdout.strip()

                if fstype in ('ntfs', 'fuseblk'):
                    mount_cmd = ['sudo', 'mount', '-t', 'ntfs-3g', '-o',
                        f'ro,noexec,nosuid,nodev,uid={uid},gid={gid}', device, part_mount]
                elif fstype in ('exfat', 'vfat'):
                    mount_cmd = ['sudo', 'mount', '-t', fstype, '-o',
                        f'ro,noexec,nosuid,nodev,uid={uid},gid={gid}', device, part_mount]
                else:
                    mount_cmd = ['sudo', 'mount', '-o', 'ro,noexec,nosuid,nodev', device, part_mount]

                mr = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
                if mr.returncode == 0:
                    source_dirs.append(part_mount)
                    mount_ok += 1
                else:
                    mount_err += 1
                    clean = re.sub(r'\033\[[0-9;]*m', '', mr.stderr + mr.stdout)
                    results.append({'label': f'{label}/{part_label}', 'status': 'fout',
                                    'fase': 'mount', 'message': clean[:200]})
            except Exception as e:
                mount_err += 1
                results.append({'label': f'{label}/{part_label}', 'status': 'fout',
                                'fase': 'mount', 'message': str(e)[:200]})

        _update_progress(task_id, 'running',
            f'Schijf {i}/{total}: {label} — {mount_ok} partities gemount, {mount_err} fouten',
            disk_pct_base + 5,
            {'fase': 'mount_klaar', 'label': label, 'results': results,
             'disk_nr': i, 'disk_total': total, 'mount_ok': mount_ok, 'mount_err': mount_err})

        if not source_dirs:
            results.append({'label': label, 'status': 'fout', 'fase': 'mount',
                            'message': 'Geen partities gemount'})
            continue

        # Stap 2: Scan alle partities
        total_files_ok = 0
        total_files_err = 0
        total_bytes = 0
        all_log_files = []

        for j, source_dir in enumerate(source_dirs, 1):
            part_name = Path(source_dir).name
            _update_progress(task_id, 'running',
                f'Schijf {i}/{total}: {label} — partitie {j}/{len(source_dirs)} ({part_name}) scannen...',
                disk_pct_base + 5 + int(j / len(source_dirs) * 70 * (1/total)),
                {'fase': 'scannen', 'label': label, 'current_partition': part_name,
                 'disk_nr': i, 'disk_total': total, 'results': results})

            _run_scan(label, source_dir, task_id)

            with _progress_lock:
                progress = json.loads(PROGRESS_FILE.read_text())
            scan_info = progress.get(task_id, {})
            d = scan_info.get('details', {})
            total_files_ok += d.get('files_ok', 0)
            total_files_err += d.get('files_error', 0)
            total_bytes += d.get('total_bytes', 0)
            if d.get('log_file'):
                all_log_files.append(d['log_file'])

            results.append({
                'label': f'{label}/{part_name}',
                'status': 'ok' if scan_info.get('status') != 'failed' else 'fout',
                'fase': 'scan_klaar',
                'files_ok': d.get('files_ok', 0),
                'files_error': d.get('files_error', 0),
                'log_file': d.get('log_file'),
                'message': f"{d.get('files_ok', 0)} ok, {d.get('files_error', 0)} fouten"
            })

        # Stap 3: Recoll index over de hele schijf
        _update_progress(task_id, 'running',
            f'Schijf {i}/{total}: {label} — full-text index bouwen over {len(source_dirs)} partities...',
            disk_pct_base + int((disk_pct_end - disk_pct_base) * 0.85),
            {'fase': 'indexeren', 'label': label, 'results': results,
             'disk_nr': i, 'disk_total': total})

        index_dir = INDEX_BASE / label
        index_dir.mkdir(parents=True, exist_ok=True)
        conf = index_dir / "recoll.conf"
        topdirs = ' '.join(source_dirs)
        conf.write_text(
            f"topdirs = {topdirs}\n"
            f"zipSkippedNames =\nzipMaxMBs = 500\n"
            f"compressedfilemaxkbs = 500000\n"
            f"skippedNames = .git node_modules $RECYCLE.BIN RECYCLER System Volume Information\n"
            f"indexallfilenames = 1\nloglevel = 3\n"
            f"logfilename = {index_dir}/recoll.log\n"
            f"dbdir = {index_dir}/xapiandb\n")
        idx_result = subprocess.run(
            ['nice', '-n', '19', 'ionice', '-c', '3', 'recollindex', '-c', str(index_dir)],
            capture_output=True, text=True, timeout=7200)

        results.append({
            'label': label,
            'status': 'ok' if idx_result.returncode == 0 else 'waarschuwing',
            'fase': 'klaar',
            'files_ok': total_files_ok, 'files_error': total_files_err,
            'total_bytes': total_bytes,
            'message': f'TOTAAL: {total_files_ok} bestanden, {total_files_err} fouten, index {"OK" if idx_result.returncode == 0 else "waarschuwing"}'
        })

        _update_progress(task_id, 'running',
            f'Schijf {i}/{total}: {label} KLAAR — {total_files_ok} bestanden',
            disk_pct_end,
            {'fase': 'tussenverslag', 'label': label, 'results': results,
             'disk_nr': i, 'disk_total': total})

    # Eindverslag
    disk_results = [r for r in results if r.get('fase') == 'klaar']
    ok_count = sum(1 for r in disk_results if r['status'] == 'ok')
    err_count = sum(1 for r in disk_results if r['status'] != 'ok')
    _update_progress(task_id, 'completed',
        f'Batch klaar: {ok_count} schijven gelukt, {err_count} mislukt', 100,
        {'fase': 'batch_klaar', 'results': results})


def _parse_search_query(query_str):
    """Vertaal zoekquery met AND/OR naar SQL condities.

    Ondersteunt:
    - AND (default): 'rapport budget' → beide moeten voorkomen
    - OR: 'rapport OR budget' → een van beide
    - Glob: '*.pdf' → extensie = pdf
    - Extensie: '.pdf' → extensie = pdf
    - Quotes: '"exacte zin"' → exacte match
    """
    query_str = query_str.strip()
    if not query_str:
        return None, []

    # Glob/extensie shortcuts
    if query_str.startswith('*.') and ' ' not in query_str:
        return "extension = ?", [query_str[2:].lower()]
    if query_str.startswith('.') and ' ' not in query_str:
        return "extension = ?", [query_str[1:].lower()]

    # OR splitting
    if ' OR ' in query_str:
        parts = [p.strip() for p in query_str.split(' OR ') if p.strip()]
        or_conditions = []
        or_params = []
        for part in parts:
            cond, params = _single_term_condition(part)
            or_conditions.append(cond)
            or_params.extend(params)
        return '(' + ' OR '.join(or_conditions) + ')', or_params

    # AND (spatie-gescheiden termen, tenzij in quotes)
    import shlex
    try:
        terms = shlex.split(query_str)
    except ValueError:
        terms = query_str.split()

    if len(terms) == 1:
        return _single_term_condition(terms[0])

    and_conditions = []
    and_params = []
    for term in terms:
        if term.upper() == 'AND':
            continue
        cond, params = _single_term_condition(term)
        and_conditions.append(cond)
        and_params.extend(params)
    return ' AND '.join(and_conditions), and_params


def _single_term_condition(term):
    """Maak SQL conditie voor een enkele zoekterm."""
    like = f"%{term}%"
    return ("(filename LIKE ? OR title LIKE ? OR full_path LIKE ? OR keywords LIKE ?)",
            [like, like, like, like])


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="nl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Archive Search Workbench</title>
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%2312806a'/%3E%3Cpath d='M3 4.6h10M3 8h10M3 11.4h10' stroke='white' stroke-width='1.3' stroke-linecap='round'/%3E%3C/svg%3E">
    <script>(function(){try{var t=localStorage.getItem('asw-theme')||'light';document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','light');}})();</script>
    <style>
        /* DEVs-Base GUI-canon (Mantis #733): licht standaard + warm vlak, 1 accent per
           service (hier teal), vaste statuskleuren, alles als CSS-variabelen op :root,
           door de gebruiker om te schakelen naar donker. */
        :root {
            --bg: #f4efe4; --panel: #fffaf1; --panel-2: #f7efdd;
            --text: #24303a; --muted: #64707a; --muted-dim: #8c96a0;
            --line: #ddd1bc; --field: #fffdf8; --field-border: #cdbfa6; --field-ro: #efe7d6;
            --accent: #12806a; --accent-hover: #0e6d5a; --accent-ink: #ffffff; --accent-soft: #e2f0ea;
            --accent2: #14618f;
            --hover: #efe7d6;
            --guide: #4a5b66; --guide-border: #ddd1bc; --guide-bg: #f2ead9;
            --code-bg: #f3ecdd; --code-text: #3b3b3b;
            /* Vaste statuskleuren (canon) */
            --ok: #4caf50; --warn: #ff9800; --err: #f44336; --offline: #888;
            --ok-bg: #e6f4e7; --warn-bg: #fbeed6; --err-bg: #fce4e2;
        }
        :root[data-theme="dark"] {
            --bg: #1a1a2e; --panel: #16213e; --panel-2: #0f3460;
            --text: #e0e0e0; --muted: #8a98a8; --muted-dim: #66707c;
            --line: #0f3460; --field: #0f3460; --field-border: #1a4080; --field-ro: #0a1e3d;
            --accent: #00d4aa; --accent-hover: #00f0c0; --accent-ink: #0b1622; --accent-soft: #12352c;
            --accent2: #4fc3f7;
            --hover: #1a2a4e;
            --guide: #9bb5d1; --guide-border: #21486d; --guide-bg: rgba(10,30,61,0.55);
            --code-bg: #0a1020; --code-text: #aab;
            --ok: #4caf50; --warn: #ff9800; --err: #ef5350; --offline: #888;
            --ok-bg: #16351f; --warn-bg: #2a2416; --err-bg: #3a1a18;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg); color: var(--text); padding: 12px 16px;
            max-width: 1400px; margin: 0 auto; font-size: 14px;
        }
        h1 { color: var(--accent); margin-bottom: 2px; font-size: 1.4em; }
        h2 { color: var(--accent2); margin: 0 0 6px; font-size: 1.05em; }
        h3 { color: var(--accent); margin: 6px 0 4px; font-size: 0.95em; }
        .subtitle { color: var(--muted); margin-bottom: 10px; font-size: 0.85em; }
        .stats { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
        .stat-card {
            background: var(--panel); padding: 6px 12px; border-radius: 6px;
            border: 1px solid var(--line);
        }
        .stat-card .number { font-size: 1.2em; color: var(--accent); font-weight: bold; }
        .stat-card .label { font-size: 0.78em; color: var(--muted); }
        .panel {
            background: var(--panel); padding: 12px 14px; border-radius: 8px;
            border: 1px solid var(--line); margin-bottom: 10px;
        }
        .search-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
        input, select, textarea {
            background: var(--field); border: 1px solid var(--field-border); color: var(--text);
            padding: 6px 10px; border-radius: 5px; font-size: 0.88em;
            font-family: inherit;
        }
        input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
        input[type="text"], input[type="search"] { flex: 1; min-width: 160px; }
        input[type="date"] { width: 140px; }
        input[readonly] { background: var(--field-ro); color: var(--muted-dim); cursor: not-allowed; }
        select { min-width: 120px; }
        textarea { width: 100%; resize: vertical; font-family: 'Consolas', 'Courier New', monospace; }
        button, .btn {
            background: var(--accent); color: var(--accent-ink); border: none; padding: 6px 16px;
            border-radius: 5px; font-weight: bold; cursor: pointer; font-size: 0.88em;
        }
        button:hover, .btn:hover { background: var(--accent-hover); }
        button:disabled { background: var(--line); color: var(--muted-dim); cursor: not-allowed; }
        .btn-secondary { background: var(--field); color: var(--accent2); border: 1px solid var(--field-border); }
        .btn-secondary:hover { background: var(--hover); }
        .btn-danger { background: var(--err); color: white; }
        .btn-danger:hover { filter: brightness(1.1); }
        .btn-warn { background: var(--warn); color: #1a1a2e; }
        .btn-small { padding: 3px 8px; font-size: 0.78em; }
        .tabs { display: flex; gap: 3px; margin-bottom: 8px; }
        .tab {
            padding: 5px 12px; border-radius: 5px 5px 0 0; cursor: pointer;
            background: var(--panel-2); color: var(--muted); border: 1px solid var(--line);
            border-bottom: none; font-size: 0.88em;
        }
        .tab.active { background: var(--panel); color: var(--accent); }
        .results {
            background: var(--panel); border-radius: 8px; border: 1px solid var(--line);
            overflow: hidden; max-height: 70vh; overflow-y: auto;
        }
        .result-item {
            padding: 7px 12px; border-bottom: 1px solid var(--line);
            transition: background 0.1s;
        }
        .result-item:hover { background: var(--hover); }
        .result-item:last-child { border-bottom: none; }
        .filename { color: var(--accent2); font-weight: 500; }
        .meta { color: var(--muted); font-size: 0.8em; margin-top: 1px; }
        .meta span { margin-right: 10px; }
        .label-badge {
            background: var(--accent-soft); color: var(--accent); padding: 1px 6px;
            border-radius: 3px; font-size: 0.76em;
        }
        .offline-warning { color: var(--warn); font-size: 0.8em; margin-top: 1px; }
        .count { color: var(--muted); padding: 6px 12px; font-size: 0.82em; }
        .empty { color: var(--muted-dim); padding: 20px; text-align: center; }
        .help-text { color: var(--muted-dim); font-size: 0.78em; margin-top: 3px; }
        .guide-text { color: var(--guide); font-size: 0.8em; line-height: 1.45; }
        .action-guide {
            margin: 8px 0 0;
            padding: 8px 10px;
            border-radius: 6px;
            border: 1px solid var(--guide-border);
            background: var(--guide-bg);
        }
        .action-guide strong { color: var(--accent2); }
        .action-guide span {
            display: inline-block;
            margin-right: 14px;
            margin-top: 4px;
        }
        .inline-note {
            display: inline-block;
            color: var(--guide);
            font-size: 0.78em;
            margin-top: 2px;
        }

        .progress-container {
            background: var(--panel-2); border-radius: 6px; overflow: hidden;
            height: 26px; margin: 6px 0; position: relative;
        }
        .progress-bar {
            background: linear-gradient(90deg, var(--accent), var(--accent2));
            height: 100%; transition: width 0.5s ease; border-radius: 6px;
        }
        .progress-text {
            position: absolute; top: 0; left: 0; right: 0; bottom: 0;
            display: flex; align-items: center; justify-content: center;
            font-size: 0.82em; font-weight: bold; color: white;
            text-shadow: 0 1px 2px rgba(0,0,0,0.5);
        }
        .status-msg { color: var(--muted); font-size: 0.82em; margin: 2px 0; }
        .status-msg.error { color: var(--err); }
        .status-msg.success { color: var(--ok); }
        .status-msg.interrupted { color: var(--warn); font-weight: bold; }

        .disk-table { width: 100%; border-collapse: collapse; margin: 4px 0; font-size: 0.84em; }
        .disk-table th {
            text-align: left; padding: 4px 8px; color: var(--accent);
            border-bottom: 2px solid var(--line); font-size: 0.8em;
        }
        .disk-table td { padding: 4px 8px; border-bottom: 1px solid var(--line); }
        .disk-table tr:hover { background: var(--hover); }
        .badge-online { color: var(--ok); }
        .badge-offline { color: var(--warn); }
        .badge-rw { color: var(--err); font-weight: bold; }
        .badge-ro { color: var(--ok); }
        .badge-ok { background: var(--ok-bg); color: var(--ok); padding: 1px 6px; border-radius: 3px; font-size: 0.8em; }
        .badge-err { background: var(--err-bg); color: var(--err); padding: 1px 6px; border-radius: 3px; font-size: 0.8em; }

        .action-row { display: flex; gap: 6px; align-items: center; margin: 6px 0; flex-wrap: wrap; }
        .action-row input { flex: 1; min-width: 130px; }

        .log-viewer {
            background: var(--code-bg); color: var(--code-text); font-family: 'Consolas', monospace;
            font-size: 0.78em; padding: 10px; border-radius: 6px; max-height: 350px;
            overflow-y: auto; white-space: pre-wrap; line-height: 1.35;
        }

        .tussenverslag {
            background: var(--panel-2); border: 1px solid var(--field-border); border-radius: 6px;
            padding: 6px 10px; margin: 4px 0;
        }
        .tussenverslag .tv-label { font-weight: bold; color: var(--accent2); }

        .sql-results {
            overflow-x: auto; max-height: 50vh; overflow-y: auto;
        }
        .sql-results table { border-collapse: collapse; width: 100%; font-size: 0.82em; }
        .sql-results th { background: var(--panel-2); color: var(--accent); padding: 4px 6px; text-align: left;
            position: sticky; top: 0; }
        .sql-results td { padding: 3px 6px; border-bottom: 1px solid var(--line); }
        .sql-results tr:hover { background: var(--hover); }

        input[type="checkbox"] { width: 15px; height: 15px; accent-color: var(--accent); }

        .badge-mounted { background: var(--ok-bg); color: var(--ok); padding: 1px 6px; border-radius: 3px; font-size: 0.8em; font-weight: bold; }
        .badge-unmounted { background: var(--err-bg); color: var(--err); padding: 1px 6px; border-radius: 3px; font-size: 0.8em; }
        .service-status {
            display: inline-block; padding: 3px 10px; border-radius: 5px;
            font-weight: bold; font-size: 0.85em; margin: 2px 0;
        }
        .service-active { background: var(--ok-bg); color: var(--ok); }
        .service-inactive { background: var(--err-bg); color: var(--err); }
        .service-unknown { background: var(--warn-bg); color: var(--warn); }

        #theme-toggle {
            background: var(--field); color: var(--text); border: 1px solid var(--field-border);
            border-radius: 4px; padding: 4px 9px; cursor: pointer; font-size: 0.95em; font-weight: normal;
        }
        #theme-toggle:hover { background: var(--hover); }

        .hidden { display: none; }

        @media (max-width: 600px) {
            .search-row, .action-row { flex-direction: column; }
            input[type="text"], input[type="search"], select, .action-row input { min-width: 100%; }
        }
    </style>
</head>
<body>
    <div style="display:flex; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; gap:6px; margin-bottom:4px">
        <div>
            <h1 style="margin:0" id="app-title">Archive Search Workbench</h1>
            <p class="subtitle" style="margin:2px 0 0" id="app-subtitle">Doorzoek oude opslagmedia &mdash; metadata &amp; full-text</p>
        </div>
        <nav style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding-top:4px">
            <button id="theme-toggle" onclick="toggleTheme()" title="Wissel licht/donker" aria-label="Wissel licht/donker">&#127769;</button>
            <a id="donate-link" href="#" target="_blank" rel="noreferrer"
               style="font-size:0.82em; padding:4px 12px; border-radius:4px; border:1px solid var(--field-border);
                      color:var(--muted); text-decoration:none; cursor:default"
               aria-disabled="true" title="">Doneer niet gepubliceerd</a>
            <select id="language-select" aria-label="Taal"
                style="background:var(--field); border:1px solid var(--field-border); color:var(--text); padding:4px 8px;
                       border-radius:4px; font-size:0.82em; cursor:pointer; min-width:auto">
                <option value="nl" selected>Nederlands</option>
                <option value="en">English</option>
            </select>
        </nav>
    </div>
    <p class="help-text" style="margin:0 0 8px 0; color:var(--muted-dim)">
        Server: <span id="srv-host">deze host</span> &bull; Data: ~/archive-search-workbench/data/ &bull; Mount: /mnt/archive-ingest/
    </p>

    <div class="stats" id="stats"></div>

    <div class="tabs" id="main-tabs">
        <div class="tab active" onclick="switchView('search')" data-i18n="tabSearch">Zoeken</div>
        <div class="tab" onclick="switchView('manage')" data-i18n="tabManage">Beheer</div>
        <div class="tab" onclick="switchView('media')" data-i18n="tabMedia">Media</div>
        <div class="tab" onclick="switchView('sql')">SQL</div>
        <div class="tab" onclick="switchView('logs')" data-i18n="tabLogs">Logboek</div>
    </div>

    <!-- ZOEKEN -->
    <div id="view-search">
        <div class="panel">
            <form id="searchForm" onsubmit="doSearch(event)">
                <div class="search-row">
                    <input type="search" id="query" name="query"
                        data-i18n-placeholder="searchPlaceholder"
                        placeholder="Zoek op naam, titel, pad... (AND/OR, *.pdf, .docx)">
                    <button type="submit" data-i18n="searchBtn">Zoeken</button>
                </div>
                <div class="search-row">
                    <select id="group" name="group">
                        <option value="" data-i18n="allGroups">Alle groepen</option>
                        <option value="documenten" data-i18n="groupDocumenten">Documenten</option>
                        <option value="spreadsheets" data-i18n="groupSpreadsheets">Spreadsheets</option>
                        <option value="databases" data-i18n="groupDatabases">Databases</option>
                        <option value="afbeeldingen" data-i18n="groupAfbeeldingen">Afbeeldingen</option>
                        <option value="archieven" data-i18n="groupArchieven">Archieven</option>
                        <option value="code_kennis" data-i18n="groupCodeKennis">Code/Kennis</option>
                    </select>
                    <select id="label" name="label">
                        <option value="">Alle media</option>
                    </select>
                    <input type="date" id="date_from" name="date_from" title="Datum vanaf">
                    <input type="date" id="date_to" name="date_to" title="Datum tot">
                </div>
                <p class="help-text" style="cursor:pointer" onclick="document.getElementById('search-help-detail').classList.toggle('hidden')">
                    &#9432; AND/spatie = alle termen &bull; OR = een-van &bull; *.pdf = extensie &bull; "exacte zin"
                    <span style="color:var(--accent2)">[meer]</span>
                </p>
                <div id="search-help-detail" class="hidden" style="background:var(--panel-2); border:1px solid var(--field-border); border-radius:6px; padding:8px 12px; margin-top:4px; font-size:0.82em; color:var(--muted); line-height:1.5">
                    <strong style="color:var(--accent2)">Hoe zoeken werkt:</strong><br>
                    <b>Meerdere woorden</b> (spatie) = alle woorden moeten voorkomen (AND)<br>
                    <b>OR</b> tussen woorden = minimaal een van de woorden<br>
                    <b>*.pdf</b> of <b>.pdf</b> = zoek op extensie<br>
                    <b>"exacte tekst"</b> = zoek letterlijk deze combinatie<br>
                    <b>Filters</b> combineer je met de dropdowns (groep, medium, datum)<br>
                    <b>Metadata</b> = zoekt op bestandsnaam, titel, pad, trefwoorden<br>
                    <b>Inhoud</b> = full-text zoeken in de inhoud van documenten (Recoll)
                </div>
            </form>
        </div>

        <div class="tabs">
            <div class="tab active" id="tab-metadata" onclick="switchSearchTab('metadata')" data-i18n="tabMetadata">Metadata</div>
            <div class="tab" id="tab-content" onclick="switchSearchTab('content')" data-i18n="tabContent">Inhoud (full-text)</div>
        </div>
        <div class="results" id="results">
            <div class="empty" data-i18n="searchHint">Voer een zoekterm of filter in om te beginnen</div>
        </div>
    </div>

    <!-- BEHEER -->
    <div id="view-manage" class="hidden">

        <!-- Netwerk-USB: schijf op een andere machine (USB/IP) -->
        <div class="panel" id="remote-usb-panel">
            <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px; flex-wrap:wrap">
                <h2 style="margin:0" data-i18n="remoteUsbTitle">&#127760; Netwerk-USB — schijf op andere machine</h2>
                <button onclick="loadRemoteHosts()" class="btn-secondary btn-small">&#8635; <span data-i18n="refresh">Vernieuwen</span></button>
                <a href="/netwerk-usb" target="_blank" class="btn-secondary btn-small" style="text-decoration:none" data-i18n="remoteUsbOnboard">&#128295; Nieuwe machine instellen</a>
            </div>
            <p class="guide-text" data-i18n="remoteUsbIntro">
                Hangt de schijf aan een andere machine? Geef hem via USB/IP door aan deze server: kies de machine, koppel de schijf, en hij verschijnt daarna hieronder bij "Aangesloten schijven" om te labelen, mounten en scannen.
            </p>
            <div class="action-row" style="align-items:center; flex-wrap:wrap">
                <select id="remote-host-select" style="min-width:240px"></select>
                <button onclick="loadRemoteDevices()" class="btn-secondary btn-small" data-i18n="remoteShowDisks">Toon schijven op deze machine</button>
            </div>
            <div id="remote-status" class="meta" style="margin-top:6px; color:var(--muted)"></div>
            <div id="remote-device-list" style="margin-top:8px"></div>
            <div id="remote-ports" style="margin-top:12px"></div>
        </div>

        <!-- Centraal overzicht aangesloten schijven -->
        <div class="panel">
            <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px">
                <h2 style="margin:0" data-i18n="connectedDisks">Aangesloten schijven</h2>
                <button onclick="loadConnectedDisks()" class="btn-secondary btn-small">&#8635; <span data-i18n="refresh">Vernieuwen</span></button>
                <button onclick="startBatchIngest()" id="btn-batch" class="btn-small" style="display:none">&#9654; <span data-i18n="processNew">Nieuwe schijven</span></button>
            </div>
            <p class="guide-text" data-i18n="manageIntro">
                Werk van boven naar beneden: kies een schijfkaart, voer daar de hoofdactie uit en gebruik het noodpaneel alleen als de kaart het niet redt.
            </p>
            <div id="disk-list"><div class="status-msg">Laden...</div></div>
        </div>

        <!-- Voortgang -->
        <div class="panel" id="progress-panel" style="display:none">
            <h2><span data-i18n="progress">Voortgang</span> <span id="progress-task" class="meta" style="font-size:0.75em; font-weight:normal; color:var(--muted)"></span></h2>
            <div class="progress-container">
                <div class="progress-bar" id="progress-bar" style="width:0%"></div>
                <div class="progress-text" id="progress-text">0%</div>
            </div>
            <div class="status-msg" id="progress-msg">Wachten...</div>
            <div class="meta" id="progress-details"></div>
            <div id="tussenverslagen"></div>
        </div>

        <!-- Handmatig + Webservice -->
        <div style="display:flex; gap:10px; flex-wrap:wrap">
            <div class="panel" style="flex:1; min-width:320px">
                <h2 data-i18n="manualPanel">Handmatig (enkele partitie)</h2>
                <p class="guide-text" data-i18n="manualHelp">Gebruik dit alleen als de knoppen hierboven niet werken. Voor schijven met meerdere partities gebruik je de schijfkaart hierboven.</p>
                <div id="manual-prefill-status" class="meta" style="margin:0 0 8px 0; color:var(--muted)">
                    Nog niets gekozen. Kies bij voorkeur een knop op de schijfkaart hierboven; dit noodpaneel vult zich dan vanzelf.
                </div>
                <div class="action-row">
                    <input type="text" id="new-device" placeholder="/dev/sda1 (partitie, niet /dev/sda)">
                    <input type="text" id="new-label" placeholder="ARCHIVE-DISK-NNN">
                    <select id="new-type" style="min-width:100px">
                        <option value="usb_hdd">USB HDD</option>
                        <option value="usb_ssd">USB SSD</option>
                        <option value="usb_flash">USB Stick</option>
                        <option value="sd_card">SD Kaart</option>
                        <option value="external_sata_usb">SATA/USB</option>
                    </select>
                </div>
                <div style="display:flex; gap:6px; flex-wrap:wrap">
                    <button onclick="mountDisk()" class="btn-secondary btn-small">1. Mount RO</button>
                    <button onclick="startScan()" class="btn-secondary btn-small">2. Scan</button>
                    <button onclick="startIndex()" class="btn-secondary btn-small">3. Index</button>
                    <button onclick="runFullIngest()" class="btn-small" style="background:var(--accent2)" data-i18n="stepAll">Alles</button>
                </div>
                <div class="action-guide guide-text">
                    <span data-i18n="manualStepMount"><strong>1. Mount RO</strong>: koppel alleen-lezen op het archief-pad.</span>
                    <span data-i18n="manualStepScan"><strong>2. Scan</strong>: lees bestanden en metadata in.</span>
                    <span data-i18n="manualStepIndex"><strong>3. Index</strong>: bouw full-text zoekindex.</span>
                    <span data-i18n="manualStepAll"><strong>Alles</strong>: voer mount, scan en index direct achter elkaar uit.</span>
                </div>
            </div>
            <div class="panel" style="flex:1; min-width:280px">
                <h2 data-i18n="webservice">Webservice</h2>
                <p class="guide-text" data-i18n="serviceHelp">Gebruik herstarten alleen als de interface of achtergrondtaak echt vastzit; een lopende scan wordt dan onderbroken en later hervat.</p>
                <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap">
                    <span id="service-status"><span class="service-status service-unknown">...</span></span>
                    <button onclick="serviceAction('status')" class="btn-secondary btn-small" data-i18n="serviceStatus">Status</button>
                    <button onclick="serviceAction('restart')" class="btn-warn btn-small" data-i18n="restart">Herstarten</button>
                    <button onclick="serviceAction('stop')" class="btn-danger btn-small" data-i18n="stop">Stoppen</button>
                    <button onclick="serviceAction('start')" class="btn-small" data-i18n="start">Starten</button>
                </div>
                <div id="service-output" class="log-viewer" style="display:none; max-height:150px; margin-top:6px"></div>
            </div>
        </div>

        <!-- Geavanceerd: verborgen dropdowns voor sticker/check op niet-aangesloten media -->
        <details id="geavanceerd-panel" style="margin-top:8px">
            <summary class="meta" style="cursor:pointer; padding:8px 0; color:var(--muted)">&#9660; Geavanceerd: sticker &amp; archief-check op niet-aangesloten media</summary>
            <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:8px">
                <div class="panel" style="flex:1; min-width:280px">
                    <h2 data-i18n="eject">Uitwerpen</h2>
                    <div class="action-row">
                        <select id="eject-label"></select>
                        <button onclick="ejectDisk()" class="btn-warn" data-i18n="eject">Uitwerpen</button>
                    </div>
                    <div id="eject-msg"></div>
                </div>
                <div class="panel" style="flex:1; min-width:280px">
                    <h2 data-i18n="stickerPanel">Sticker bevestigen</h2>
                    <div class="action-row">
                        <select id="sticker-select" onchange="updateStickerInfo()"></select>
                        <button onclick="confirmSticker()" class="btn-secondary" data-i18n="confirmedBtn">Bevestigd</button>
                    </div>
                    <div id="sticker-info" style="margin:4px 0"></div>
                </div>
            </div>
            <div class="panel" style="margin-top:8px">
                <h2 data-i18n="checkPanel">Check archive-disk</h2>
                <p class="help-text">Vergelijk een aangesloten schijf met het archief in de database.</p>
                <div class="action-row">
                    <select id="check-label"></select>
                    <button onclick="checkDiskQuick()" class="btn-secondary" data-i18n="quickCheck">► Snelle check (aantallen)</button>
                    <button onclick="checkDiskFull()" data-i18n="fullCheck">🔍 Volledige check</button>
                </div>
                <div id="check-result"></div>
            </div>
            <!-- #211: Pad-migratie voor schijven met foutief prefix in DB-paden -->
            <div class="panel" style="margin-top:8px; border-color:var(--warn)">
                <h2 style="color:var(--warn)">&#9889; Pad-migratie (databeheer)</h2>
                <p class="help-text">
                    <strong>Wanneer gebruiken:</strong> als een check "alleen DB"-bestanden toont terwijl de schijf wél gemount is.
                    Oorzaak: bij de scan is een foutief pad-prefix (bijv. <code>Elements/</code>) opgeslagen. De bestanden staan
                    er nog, maar de paden in de database kloppen niet.<br>
                    <strong>Hoe prefix vinden:</strong> kijk bij de "alleen DB"-voorbeelden in het check-resultaat — het prefix is
                    het stuk vóór de eigenlijke bestandsnaam (bijv. <code>Elements/</code> als paden beginnen met
                    <code>Elements/map/bestand.ext</code>).<br>
                    <strong>Wat het doet:</strong> verwijdert het opgegeven prefix uit alle DB-paden voor dit medium.<br>
                    <strong style="color:var(--warn)">⚠ Let op:</strong> deze actie is onomkeerbaar. Test eerst met een kleine bekende prefix.
                </p>
                <div class="action-row" style="flex-wrap:wrap; gap:6px">
                    <select id="migrate-label"></select>
                    <input type="text" id="migrate-prefix" placeholder='bijv. Elements/' style="width:140px"
                        title="Het te verwijderen prefix (relatief pad, eindigt op /)">
                    <button onclick="runPathMigration()" class="btn-warn">&#9889; Migratie uitvoeren</button>
                </div>
                <div id="migrate-result"></div>
            </div>
        </details>
    </div>

    <!-- MEDIA OVERZICHT -->
    <div id="view-media" class="hidden">
        <div class="panel">
            <h2 data-i18n="registeredMedia">Geregistreerde media</h2>
            <div id="media-table"></div>
        </div>
    </div>

    <!-- SQL QUERY -->
    <div id="view-sql" class="hidden">
        <div class="panel">
            <h2>SQL Query <span class="help-text" data-i18n="sqlSubtitle">(alleen SELECT — tabellen: files, physical_media, scans)</span></h2>
            <textarea id="sql-input" rows="3" placeholder="SELECT filename, extension, human_size FROM files WHERE extension = 'pdf' LIMIT 20"></textarea>
            <div style="display:flex; gap:6px; margin:4px 0; flex-wrap:wrap">
                <button onclick="runSQL()" data-i18n="sqlRun">Uitvoeren</button>
                <button onclick="document.getElementById('sql-input').value='SELECT extension, COUNT(*) as aantal, SUM(size_bytes) as totaal_bytes FROM files GROUP BY extension ORDER BY aantal DESC'" class="btn-secondary btn-small" data-i18n="sqlExtensions">Extensies</button>
                <button onclick="document.getElementById('sql-input').value='SELECT archive_label, COUNT(*) as bestanden, SUM(size_bytes) as bytes FROM files GROUP BY archive_label'" class="btn-secondary btn-small" data-i18n="sqlPerMedia">Per medium</button>
                <button onclick="document.getElementById('sql-input').value='SELECT filename, human_size, original_content_date, archive_label FROM files ORDER BY size_bytes DESC LIMIT 50'" class="btn-secondary btn-small" data-i18n="sqlLargest">Grootste</button>
            </div>
            <div id="sql-results" class="sql-results"></div>
        </div>
    </div>

    <!-- LOGBOEK -->
    <div id="view-logs" class="hidden">
        <div class="panel">
            <div style="display:flex; gap:8px; align-items:center; margin-bottom:6px">
                <h2 style="margin:0" data-i18n="scanOverview">Scan-overzicht</h2>
                <button onclick="loadScans()" class="btn-secondary btn-small" data-i18n="refresh">Vernieuwen</button>
            </div>
            <p class="guide-text" data-i18n="scanOverviewHelp">Elke regel is één scanronde. Bij een schijf met meerdere partities zie je één regel per ARCHIVE-DISK; de bron-kolom laat de gebruikte partities zien.</p>
            <div id="scan-overview"></div>
        </div>
        <div class="panel">
            <div style="display:flex; gap:8px; align-items:center; margin-bottom:6px">
                <h2 style="margin:0" data-i18n="logFiles">Logbestanden</h2>
                <button onclick="loadLogs()" class="btn-secondary btn-small" data-i18n="refresh">Vernieuwen</button>
            </div>
            <p class="guide-text" data-i18n="logFilesHelp">Logbestanden horen bij scan-, mount-, index- en controletaken. Gebruik Bekijken voor de details van een ronde.</p>
            <div id="log-list"></div>
            <div id="log-viewer" class="log-viewer" style="display:none"></div>
        </div>
    </div>

<script>
let currentView = 'search';
let currentSearchTab = 'metadata';
let pollInterval = null;
let detectedDisks = [];
let _selected = new Map();   // geselecteerde bestanden voor bulk-download (key: label|path)
let _availableLabels = new Set();   // schijven die nu echt bereikbaar zijn (aanvinken alleen dan)
const ARCHIVE_MOUNT_BASE = '/mnt/archive-ingest';

function toggleSel(cb, label, path, filename) {
    const k = label + '|' + path;
    if (cb.checked) { _selected.set(k, {label, path, filename}); }
    else { _selected.delete(k); }
    updateSelBar();
}
function updateSelBar() {
    const bar = document.getElementById('sel-bar');
    const c = document.getElementById('sel-count');
    if (c) c.textContent = _selected.size;
    if (bar) bar.style.display = _selected.size > 0 ? 'flex' : 'none';
}
function clearSel() {
    _selected.clear();
    document.querySelectorAll('input.sel-file').forEach(cb => { cb.checked = false; });
    updateSelBar();
}
async function downloadSelected() {
    if (_selected.size === 0) return;
    const btn = document.getElementById('sel-dl-btn');
    const files = [..._selected.values()].map(v => ({label: v.label, path: v.path}));
    const orig = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.innerHTML = 'Bezig met ophalen...'; }
    try {
        const resp = await fetch('/api/download-zip', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({files})
        });
        if (!resp.ok) {
            let msg = 'HTTP ' + resp.status;
            try { const j = await resp.json(); msg = j.error || msg; } catch(_) {}
            alert('Download mislukt: ' + msg);
            return;
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'archief-selectie.zip';
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
    } catch(e) {
        alert('Download mislukt: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = orig; }
    }
}

// === i18n + Donate ===
const I18N = {
    nl: {
        tabSearch: 'Zoeken', tabManage: 'Beheer', tabMedia: 'Media', tabLogs: 'Logboek',
        appSubtitle: 'Doorzoek oude opslagmedia — metadata & full-text',
        donate: 'Doneer', donateUnavailable: 'Doneer niet gepubliceerd',
        donateUnavailableTitle: 'Geen donate-route gepubliceerd voor deze workspace.',
        statFiles: 'Bestanden', statMedia: 'Media', statTotal: 'Totaal', statScans: 'Scans',
        allMedia: 'Alle media', searchBtn: 'Zoeken',
        searchPlaceholder: 'Zoek op naam, titel, pad... (AND/OR, *.pdf, .docx)',
        allGroups: 'Alle groepen',
        groupDocumenten: 'Documenten', groupSpreadsheets: 'Spreadsheets',
        groupDatabases: 'Databases', groupAfbeeldingen: 'Afbeeldingen',
        groupArchieven: 'Archieven', groupCodeKennis: 'Code/Kennis',
        tabMetadata: 'Metadata', tabContent: 'Inhoud (full-text)',
        searchHint: 'Voer een zoekterm of filter in om te beginnen',
        copyPath: 'Kopieer pad', showFile: 'Toon bestand', openDir: 'Open map',
        openExternal: 'Open extern',
        statusOnline: 'Online', statusOffline: 'Offline',
        nResults: 'resultaten gevonden', noResults: 'Geen resultaten gevonden',
        sqlInTab: 'Toon in SQL-tab',
        connectedDisks: 'Aangesloten schijven', viewLog: 'Bekijken',
        remoteUsbTitle: '\u{1F310} Netwerk-USB — schijf op andere machine',
        remoteUsbIntro: 'Hangt de schijf aan een andere machine? Geef hem via USB/IP door aan deze server: kies de machine, koppel de schijf, en hij verschijnt daarna hieronder bij "Aangesloten schijven" om te labelen, mounten en scannen.',
        remoteShowDisks: 'Toon schijven op deze machine',
        remoteUsbOnboard: '\u{1F527} Nieuwe machine instellen',
        coupleDisk: 'Koppel schijf',
        statusReachable: 'Beschikbaar', statusNotConnected: 'Niet aangesloten',
        tickToDownload: 'vink aangesloten schijven aan om samen te downloaden',
        selectForDownload: 'Selecteer voor download',
        notConnectedTip: 'Schijf niet aangesloten — sluit hem aan om te kunnen aanvinken',
        connectAndOpen: 'Aansluiten & openen',
        progress: 'Voortgang', manualPanel: 'Noodgreep: losse partitie handmatig',
        manageIntro: 'Werk van boven naar beneden: kies een schijfkaart, voer daar de hoofdactie uit en gebruik het noodpaneel alleen als de kaart het niet redt.',
        manualHelp: 'Gebruik dit alleen als de knoppen hierboven niet werken. Voor schijven met meerdere partities gebruik je de schijfkaart hierboven.',
        manualStepMount: '1. Mount RO: koppel alleen-lezen op het archief-pad.',
        manualStepScan: '2. Scan: lees bestanden en metadata in.',
        manualStepIndex: '3. Index: bouw full-text zoekindex.',
        manualStepAll: 'Alles: voer mount, scan en index direct achter elkaar uit.',
        webservice: 'Webservice', restart: 'Herstarten', stop: 'Stoppen', start: 'Starten',
        serviceHelp: 'Gebruik herstarten alleen als de interface of achtergrondtaak echt vastzit; een lopende scan wordt dan onderbroken en later hervat.',
        processNew: 'Nieuwe schijven',
        serviceStatus: 'Status', eject: 'Uitwerpen',
        stickerPanel: 'Sticker bevestigen', confirmedBtn: 'Bevestigd',
        markSticker: 'Sticker geplakt',
        checkPanel: 'Check archive-disk', quickCheck: '► Snelle check (aantallen)',
        fullCheck: '\U0001F50D Volledige check', stepAll: 'Alles',
        registeredMedia: 'Geregistreerde media', noMedia: 'Nog geen media geregistreerd',
        sqlSubtitle: '(alleen SELECT — tabellen: files, physical_media, scans)',
        sqlRun: 'Uitvoeren', sqlExtensions: 'Extensies', sqlPerMedia: 'Per medium', sqlLargest: 'Grootste',
        sqlNoResults: 'Geen resultaten', sqlRows: 'rijen',
        scanOverview: 'Scan-overzicht', logFiles: 'Logbestanden', refresh: 'Vernieuwen',
        scanOverviewHelp: 'Elke regel is één scanronde. Bij een schijf met meerdere partities zie je één regel per ARCHIVE-DISK; de bron-kolom laat de gebruikte partities zien.',
        logFilesHelp: 'Logbestanden horen bij scan-, mount-, index- en controletaken. Gebruik Bekijken voor de details van een ronde.',
        noScans: 'Nog geen scans uitgevoerd',
        colLabel: 'Label', colType: 'Type', colVolume: 'Volume', colModel: 'Model',
        colSticker: 'Sticker', colFirstSeen: 'Eerste gezien',
        colSource: 'Bron', colStart: 'Start', colEnd: 'Einde', colStatus: 'Status', colErrors: 'Fouten',
        yes: 'Ja', no: 'Nee', scanDone: 'Klaar', scanRunning: 'Loopt...',
        webserviceRunning: 'Webservice draait', webserviceStopped: 'Webservice gestopt',
    },
    en: {
        tabSearch: 'Search', tabManage: 'Manage', tabMedia: 'Media', tabLogs: 'Log',
        appSubtitle: 'Search old storage media — metadata & full-text',
        donate: 'Donate', donateUnavailable: 'Donate not published',
        donateUnavailableTitle: 'No donate route has been published for this workspace.',
        statFiles: 'Files', statMedia: 'Media', statTotal: 'Total', statScans: 'Scans',
        allMedia: 'All media', searchBtn: 'Search',
        searchPlaceholder: 'Search by name, title, path... (AND/OR, *.pdf, .docx)',
        allGroups: 'All groups',
        groupDocumenten: 'Documents', groupSpreadsheets: 'Spreadsheets',
        groupDatabases: 'Databases', groupAfbeeldingen: 'Images',
        groupArchieven: 'Archives', groupCodeKennis: 'Code/Knowledge',
        tabMetadata: 'Metadata', tabContent: 'Content (full-text)',
        searchHint: 'Enter a search term or filter to begin',
        copyPath: 'Copy path', showFile: 'Show file', openDir: 'Open folder',
        openExternal: 'Open externally',
        statusOnline: 'Online', statusOffline: 'Offline',
        nResults: 'results found', noResults: 'No results found',
        sqlInTab: 'Show in SQL tab',
        connectedDisks: 'Connected disks', viewLog: 'View',
        remoteUsbTitle: '\u{1F310} Network USB — disk on another machine',
        remoteUsbIntro: 'Is the disk attached to another machine? Forward it to this server over USB/IP: pick the machine, connect the disk, and it will appear below under "Connected disks" to label, mount and scan.',
        remoteShowDisks: 'Show disks on this machine',
        remoteUsbOnboard: '\u{1F527} Set up new machine',
        coupleDisk: 'Connect disk',
        statusReachable: 'Available', statusNotConnected: 'Not connected',
        tickToDownload: 'tick connected disks to download together',
        selectForDownload: 'Select for download',
        notConnectedTip: 'Disk not connected — connect it to select',
        connectAndOpen: 'Connect & open',
        progress: 'Progress', manualPanel: 'Fallback: manual single partition',
        manageIntro: 'Work top to bottom: pick a disk card, run the main action there, and only use the fallback panel if the card cannot handle it.',
        manualHelp: 'Use this only when the buttons above do not work. For disks with multiple partitions, use the disk card above.',
        manualStepMount: '1. Mount RO: mount read-only on the archive path.',
        manualStepScan: '2. Scan: read files and metadata into the catalog.',
        manualStepIndex: '3. Index: build the full-text index.',
        manualStepAll: 'All: run mount, scan and index directly after each other.',
        webservice: 'Web service', restart: 'Restart', stop: 'Stop', start: 'Start',
        serviceHelp: 'Only restart when the interface or background task is really stuck; a running scan will be interrupted and resumed later.',
        processNew: 'New disks',
        serviceStatus: 'Status', eject: 'Eject',
        stickerPanel: 'Confirm sticker', confirmedBtn: 'Confirmed',
        markSticker: 'Sticker applied',
        checkPanel: 'Check archive disk', quickCheck: '► Quick check (counts)',
        fullCheck: '\U0001F50D Full check', stepAll: 'All',
        registeredMedia: 'Registered media', noMedia: 'No media registered yet',
        sqlSubtitle: '(SELECT only — tables: files, physical_media, scans)',
        sqlRun: 'Run', sqlExtensions: 'Extensions', sqlPerMedia: 'Per media', sqlLargest: 'Largest',
        sqlNoResults: 'No results', sqlRows: 'rows',
        scanOverview: 'Scan overview', logFiles: 'Log files', refresh: 'Refresh',
        scanOverviewHelp: 'Each row is one scan run. For disks with multiple partitions you will see one row per ARCHIVE-DISK; the source column shows the partitions used.',
        logFilesHelp: 'Log files belong to scan, mount, index and check tasks. Use View for the details of a run.',
        noScans: 'No scans performed yet',
        colLabel: 'Label', colType: 'Type', colVolume: 'Volume', colModel: 'Model',
        colSticker: 'Sticker', colFirstSeen: 'First seen',
        colSource: 'Source', colStart: 'Start', colEnd: 'End', colStatus: 'Status', colErrors: 'Errors',
        yes: 'Yes', no: 'No', scanDone: 'Done', scanRunning: 'Running...',
        webserviceRunning: 'Web service running', webserviceStopped: 'Web service stopped',
    }
};
let currentLanguage = 'nl';

function t(key) {
    return (I18N[currentLanguage] && I18N[currentLanguage][key]) || I18N.nl[key] || key;
}

function applyLanguage() {
    document.documentElement.lang = currentLanguage;
    const sub = document.getElementById('app-subtitle');
    if (sub) sub.innerHTML = t('appSubtitle');
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (key) el.textContent = t(key);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        if (key) el.placeholder = t(key);
    });
    _updateDonateLink();
    // Ververs stat-labels en media-select met nieuwe taal
    loadStats();
    // Ververs dynamische tabellen als het betreffende tabblad actief is
    if (currentView === 'media') loadMedia();
    if (currentView === 'logs') { loadScans(); }
}

function _updateDonateLink(buttonInfo) {
    const link = document.getElementById('donate-link');
    if (!link) return;
    if (buttonInfo !== undefined) link._donateButton = buttonInfo;
    const btn = link._donateButton || null;
    if (!btn || !btn.url) {
        link.href = '#';
        link.textContent = t('donateUnavailable');
        link.title = t('donateUnavailableTitle');
        link.setAttribute('aria-disabled', 'true');
        link.style.color = 'var(--muted-dim)';
        link.style.borderColor = '#333';
        link.style.cursor = 'default';
        link.onclick = e => e.preventDefault();
    } else {
        link.href = btn.url;
        link.textContent = btn.label || t('donate');
        link.title = btn.url;
        link.setAttribute('aria-disabled', 'false');
        link.style.color = 'var(--accent)';
        link.style.borderColor = 'var(--accent)';
        link.style.cursor = 'pointer';
        link.onclick = null;
    }
}

async function initDonateAndLanguage() {
    // Taal ophalen uit localStorage
    try { currentLanguage = localStorage.getItem('asw-language') || 'nl'; } catch(_) {}
    if (!I18N[currentLanguage]) currentLanguage = 'nl';
    const sel = document.getElementById('language-select');
    if (sel) {
        sel.value = currentLanguage;
        sel.addEventListener('change', () => {
            currentLanguage = sel.value || 'nl';
            try { localStorage.setItem('asw-language', currentLanguage); } catch(_) {}
            applyLanguage();
        });
    }
    // Donate button ophalen
    try {
        const resp = await fetch('/api/donate_button');
        const data = await resp.json();
        _updateDonateLink(data.donate_button || null);
    } catch(_) { _updateDonateLink(null); }
    applyLanguage();
}

async function loadStats() {
    const resp = await fetch('/api/stats');
    const data = await resp.json();
    document.getElementById('stats').innerHTML = `
        <div class="stat-card"><div class="number">${data.total_files}</div><div class="label">${t('statFiles')}</div></div>
        <div class="stat-card"><div class="number">${data.total_media}</div><div class="label">${t('statMedia')}</div></div>
        <div class="stat-card"><div class="number">${data.total_gb} GB</div><div class="label">${t('statTotal')}</div></div>
        <div class="stat-card"><div class="number">${data.total_scans}</div><div class="label">${t('statScans')}</div></div>
    `;
    // Vul media-selects
    ['label','sticker-select','eject-label','check-label','migrate-label'].forEach(id => {
        const sel = document.getElementById(id);
        if (!sel) return;
        const isLabel = id === 'label';
        // Bewaar huidige selectie
        const prevVal = sel.value;
        sel.innerHTML = isLabel ? `<option value="">${t('allMedia')}</option>` : '';
        data.media_labels.forEach(l => {
            sel.innerHTML += `<option value="${l}">${l}</option>`;
        });
        // Herstel selectie na herlaad
        if (prevVal) sel.value = prevVal;
    });
}

function switchView(view) {
    currentView = view;
    const tabs = document.getElementById('main-tabs').querySelectorAll('.tab');
    tabs.forEach((t, i) => {
        const views = ['search','manage','media','sql','logs'];
        t.classList.toggle('active', views[i] === view);
    });
    ['search','manage','media','sql','logs'].forEach(v => {
        const el = document.getElementById('view-' + v);
        if (el) el.classList.toggle('hidden', v !== view);
    });
    if (view === 'media') loadMedia();
    if (view === 'logs') { loadScans(); loadLogs(); }
    if (view === 'manage') { loadRemoteHosts(); loadRemotePorts(); loadConnectedDisks(); loadMountedDisks(); loadServiceStatus(); checkActiveTask(); }
}

// === BEHEER: Netwerk-USB (USB/IP) ===

async function loadRemoteHosts() {
    const sel = document.getElementById('remote-host-select');
    if (!sel) return;
    try {
        const data = await fetch('/api/remote/hosts').then(r => r.json());
        const hosts = data.hosts || [];
        if (hosts.length === 0) {
            sel.innerHTML = '<option value="">(geen hosts geconfigureerd)</option>';
            return;
        }
        sel.innerHTML = hosts.map(h => {
            const dot = h.reachable ? '\u{1F7E2}' : '\u{26AA}';
            return `<option value="${escHtml(h.host)}">${dot} ${escHtml(h.name)} (${escHtml(h.host)})</option>`;
        }).join('');
    } catch(e) {
        sel.innerHTML = '<option value="">(fout bij laden hosts)</option>';
    }
    loadRemotePorts();
}

async function loadRemoteDevices() {
    const host = document.getElementById('remote-host-select').value;
    const listEl = document.getElementById('remote-device-list');
    const statusEl = document.getElementById('remote-status');
    if (!host) { statusEl.textContent = 'Kies eerst een machine.'; return; }
    statusEl.textContent = `Schijven ophalen van ${host}...`;
    listEl.innerHTML = '';
    try {
        const data = await fetch('/api/remote/devices?host=' + encodeURIComponent(host)).then(r => r.json());
        if (data.error) {
            statusEl.innerHTML = `<span style="color:var(--warn)">${escHtml(data.error)}</span>`;
        } else {
            statusEl.textContent = '';
        }
        const devs = data.devices || [];
        if (devs.length === 0) {
            listEl.innerHTML = data.error ? '' :
                '<div class="empty">Geen exporteerbare USB-apparaten. Is de schijf gebonden (usbipd bind / usbip bind)?</div>';
            return;
        }
        listEl.innerHTML = devs.map(d => `
            <div class="result-item" style="display:flex; align-items:center; justify-content:space-between; gap:10px">
                <div>
                    <span class="filename">${escHtml(d.description)}</span>
                    <div class="meta"><span class="label-badge">busid ${escHtml(d.busid)}</span></div>
                </div>
                <button class="btn-small" onclick="attachRemote('${escHtml(host)}','${escHtml(d.busid)}', this)">&#128268; Koppel aan server</button>
            </div>`).join('');
    } catch(e) {
        statusEl.innerHTML = `<span style="color:var(--err)">Fout: ${escHtml(e.message)}</span>`;
    }
}

async function attachRemote(host, busid, btn) {
    const statusEl = document.getElementById('remote-status');
    if (btn) { btn.disabled = true; btn.textContent = 'Koppelen...'; }
    statusEl.textContent = `Schijf ${busid} koppelen vanaf ${host}...`;
    try {
        const data = await fetch('/api/remote/attach', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({host, busid})
        }).then(r => r.json());
        if (data.success) {
            statusEl.innerHTML = `<span style="color:var(--accent)">&#10003; ${escHtml(data.message)}</span>`;
            loadRemotePorts();
            loadConnectedDisks();
        } else {
            statusEl.innerHTML = `<span style="color:var(--err)">${escHtml(data.message)}</span>`;
        }
    } catch(e) {
        statusEl.innerHTML = `<span style="color:var(--err)">Fout: ${escHtml(e.message)}</span>`;
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '&#128268; Koppel aan server'; }
    }
}

async function loadRemotePorts() {
    const el = document.getElementById('remote-ports');
    if (!el) return;
    try {
        const data = await fetch('/api/remote/ports').then(r => r.json());
        const ports = data.ports || [];
        if (ports.length === 0) {
            el.innerHTML = data.error
                ? `<div class="meta" style="color:var(--warn)">${escHtml(data.error)}</div>`
                : '';
            return;
        }
        el.innerHTML = '<h3 style="margin:6px 0 4px">Actieve netwerk-koppelingen</h3>' +
            ports.map(p => `
            <div class="result-item" style="display:flex; align-items:center; justify-content:space-between; gap:10px">
                <div>
                    <span class="filename">${escHtml(p.host_name || p.host)}</span>
                    <div class="meta">
                        <span class="label-badge">poort ${escHtml(p.port)}</span>
                        <span>busid ${escHtml(p.busid)}</span>
                        ${p.dev ? `<span>${escHtml(p.dev)}</span>` : ''}
                    </div>
                </div>
                <button class="btn-danger btn-small" onclick="detachRemote('${escHtml(p.port)}', this)">&#9195; Loskoppelen</button>
            </div>`).join('');
    } catch(e) {
        el.innerHTML = `<div class="meta" style="color:var(--err)">Fout bij ophalen koppelingen: ${escHtml(e.message)}</div>`;
    }
}

async function detachRemote(port, btn) {
    if (!confirm('Netwerk-koppeling op poort ' + port + ' loskoppelen? Zorg dat de schijf eerst is uitgeworpen/ge-unmount.')) return;
    if (btn) { btn.disabled = true; btn.textContent = 'Loskoppelen...'; }
    try {
        const data = await fetch('/api/remote/detach', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({port})
        }).then(r => r.json());
        const statusEl = document.getElementById('remote-status');
        statusEl.innerHTML = data.success
            ? `<span style="color:var(--accent)">&#10003; ${escHtml(data.message)}</span>`
            : `<span style="color:var(--err)">${escHtml(data.message)}</span>`;
        loadRemotePorts();
        loadConnectedDisks();
    } catch(e) {
        if (btn) { btn.disabled = false; btn.innerHTML = '&#9195; Loskoppelen'; }
    }
}

function switchSearchTab(tab) {
    currentSearchTab = tab;
    document.getElementById('tab-metadata').classList.toggle('active', tab === 'metadata');
    document.getElementById('tab-content').classList.toggle('active', tab === 'content');
    const q = document.getElementById('query').value;
    if (q) doSearch(new Event('submit'));
}

async function doSearch(e) {
    e.preventDefault();
    const query = document.getElementById('query').value;
    const group = document.getElementById('group').value;
    const label = document.getElementById('label').value;
    const dateFrom = document.getElementById('date_from').value;
    const dateTo = document.getElementById('date_to').value;
    if (!query && !group && !label && !dateFrom) {
        document.getElementById('results').innerHTML = '<div class="empty">' + t('searchHint') + '</div>';
        return;
    }
    if (currentSearchTab === 'content') {
        const resp = await fetch(`/api/search/content?query=${encodeURIComponent(query)}&label=${encodeURIComponent(label)}`);
        const data = await resp.json();
        renderContentResults(data);
    } else {
        const params = new URLSearchParams({query, group, label, date_from: dateFrom, date_to: dateTo});
        const [data, avail] = await Promise.all([
            fetch(`/api/search/metadata?${params}`).then(r => r.json()),
            fetch('/api/available-labels').then(r => r.json()).catch(() => ({labels: []})),
        ]);
        _availableLabels = new Set(avail.labels || []);
        renderMetadataResults(data);
    }
}

function renderMetadataResults(data) {
    if (!data.results || data.results.length === 0) {
        document.getElementById('results').innerHTML = '<div class="empty">' + t('noResults') + '</div>';
        return;
    }
    _selected.clear();
    let html = `<div id="sel-bar" style="display:none; position:sticky; top:0; gap:8px; align-items:center; background:var(--panel); padding:8px 10px; border:1px solid var(--accent); border-radius:6px; margin-bottom:8px; z-index:5">
        <button id="sel-dl-btn" class="btn-small" style="background:var(--accent);color:var(--accent-ink);font-weight:bold" onclick="downloadSelected()">&#11015; Download geselecteerde (<span id="sel-count">0</span>) als ZIP</button>
        <button class="btn-secondary btn-small" onclick="clearSel()">Wis selectie</button>
    </div>`;
    html += `<div class="count">${data.results.length} ${t('nResults')} &mdash; <span class="meta">${t('tickToDownload')}</span></div>`;
    data.results.forEach((r, i) => {
        let meta = [];
        if (r.original_content_date) meta.push(`<span>&#128197; ${r.original_content_date.substring(0,10)}</span>`);
        if (r.author) meta.push(`<span>&#128100; ${escHtml(r.author)}</span>`);
        if (r.human_size) meta.push(`<span>&#128230; ${r.human_size}</span>`);
        if (r.extension_group) meta.push(`<span>${escHtml(r.extension_group)}</span>`);
        // Bereikbaar = schijf nu echt aangesloten (de server-gemount of lokaal op jouw machine).
        const avail = _availableLabels.has(r.archive_label);
        const dirPath = r.relative_path.includes('/') ? r.relative_path.substring(0, r.relative_path.lastIndexOf('/')) : '';
        const fullPath = r.source_root ? `${r.source_root}/${r.relative_path}` : `/mnt/archive-ingest/${r.archive_label}/${r.relative_path}`;
        const L = escHtml(r.archive_label).replace(/'/g,"\\'");
        const P = escHtml(r.relative_path).replace(/'/g,"\\'");
        const F = escHtml(r.filename).replace(/'/g,"\\'");
        const D = escHtml(dirPath).replace(/'/g,"\\'");
        const FP = escHtml(fullPath).replace(/'/g,"\\'");
        const statusBadge = avail
            ? `<span class="badge-mounted">&#9679; ${t('statusReachable')}</span>`
            : `<span class="offline-warning" style="margin:0">&#9888; ${t('statusNotConnected')}</span>`;
        // Aanvinken kan ALLEEN bij een aangesloten schijf.
        const checkbox = avail
            ? `<input type="checkbox" class="sel-file" onchange="toggleSel(this,'${L}','${P}','${F}')" title="${t('selectForDownload')}" style="margin-right:7px;transform:scale(1.15);vertical-align:middle;cursor:pointer">`
            : `<input type="checkbox" disabled title="${t('notConnectedTip')}" style="margin-right:7px;transform:scale(1.15);vertical-align:middle;opacity:0.35;cursor:not-allowed">`;
        let actions;
        if (avail) {
            actions = `
                <button class="btn-small" style="background:var(--accent);color:var(--accent-ink);font-weight:bold" onclick="showFile('${L}','${P}','${F}')">&#128065; Bekijken / downloaden</button>
                ${dirPath !== '' ? `<button class="btn-secondary btn-small" onclick="openDir('${L}','${D}')">&#128193; Map openen</button>` : ''}
                <button class="btn-secondary btn-small" onclick="copyPad('${FP}',this)">&#128203; ${t('copyPath')}</button>`;
        } else {
            actions = `
                <button class="btn-small" style="background:var(--accent2);color:var(--accent-ink);font-weight:bold" onclick="showFile('${L}','${P}','${F}')">&#128268; ${t('connectAndOpen')}</button>`;
        }
        const offlineHint = avail ? '' :
            `<div class="offline-warning" style="margin-top:4px">Sluit schijf <strong>${escHtml(r.archive_label)}</strong> aan op je machine; dan kun je 'm aanvinken en openen.</div>`;
        html += `<div class="result-item">
            <div class="filename">${checkbox}${escHtml(r.filename)} <span class="label-badge">${escHtml(r.archive_label)}</span> ${statusBadge}</div>
            ${r.title ? `<div class="meta"><span>&#128196; ${escHtml(r.title)}</span></div>` : ''}
            <div class="meta">${meta.join('')}</div>
            <div class="meta" style="margin-top:3px">&#128193; ${escHtml(r.relative_path)}</div>
            <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-top:6px">${actions}</div>
            ${offlineHint}
        </div>`;
    });
    document.getElementById('results').innerHTML = html;
}

function renderContentResults(data) {
    if (!data.results || data.results.length === 0) {
        document.getElementById('results').innerHTML = '<div class="empty">Geen resultaten in documentinhoud</div>';
        return;
    }
    let html = `<div class="count">${data.results.length} resultaten</div>`;
    data.results.forEach(r => {
        // Recoll geeft volledig pad — zoek relative_path via /api/file-info
        const dirPath = r.path.includes('/') ? r.path.substring(0, r.path.lastIndexOf('/')) : '';
        html += `<div class="result-item">
            <div class="filename">${escHtml(r.filename)} <span class="label-badge">${escHtml(r.label)}</span></div>
            <div class="meta" style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-top:4px">
                <span>&#128193; ${escHtml(r.path)}</span>
                <span>&#128230; ${r.size}</span>
                <button class="btn-secondary btn-small" onclick="showFileByPath('${escHtml(r.label).replace(/'/g,"\\'")}','${escHtml(r.path).replace(/'/g,"\\'")}','${escHtml(r.filename).replace(/'/g,"\\'")}')">&#128065; Toon bestand</button>
                <button class="btn-secondary btn-small" onclick="openExternalByPath('${escHtml(r.label).replace(/'/g,"\\'")}','${escHtml(r.path).replace(/'/g,"\\'")}','${escHtml(r.filename).replace(/'/g,"\\'")}')">&#11016; ${t('openExternal')}</button>
            </div>
        </div>`;
    });
    document.getElementById('results').innerHTML = html;
}

function escHtml(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// === KLEMBORD ===

function copyPad(pad, btn) {
    // navigator.clipboard werkt alleen op HTTPS of localhost — gebruik fallback op HTTP
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(pad).then(() => {
            btn.textContent = '✓ Gekopieerd';
            setTimeout(() => btn.textContent = 'Kopieer pad', 1500);
        }).catch(() => _showCopyFallback(pad));
    } else {
        _showCopyFallback(pad);
    }
}

function _showCopyFallback(pad) {
    _openModal('Pad kopiëren', `
        <p class="meta" style="margin-bottom:8px">Selecteer het pad en kopieer het (Ctrl+C / Cmd+C):</p>
        <input type="text" value="${escHtml(pad)}" readonly onclick="this.select()"
            style="width:100%;background:var(--code-bg);color:var(--text);border:1px solid var(--accent);
                   padding:8px 10px;border-radius:4px;font-family:monospace;font-size:0.9em">
    `);
    // Selecteer automatisch na render
    setTimeout(() => { const inp = document.querySelector('#file-modal input[type=text]'); if (inp) inp.select(); }, 50);
}

// === BESTAND TONEN ===

function _openModal(title, content) {
    let modal = document.getElementById('file-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'file-modal';
        modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);z-index:1000;display:flex;align-items:flex-start;justify-content:center;padding-top:40px;overflow-y:auto';
        modal.onclick = function(e) { if (e.target === modal) { if (typeof _stopEnsurePoll==='function') _stopEnsurePoll(); modal.remove(); } };
        document.body.appendChild(modal);
    }
    modal.innerHTML = `<div style="background:var(--panel);border:1px solid var(--accent);border-radius:10px;max-width:900px;width:95%;max-height:80vh;overflow:auto;padding:20px;position:relative">
        <button onclick="if(typeof _stopEnsurePoll==='function')_stopEnsurePoll();document.getElementById('file-modal').remove()" style="position:absolute;top:10px;right:14px;background:none;border:none;color:var(--text);font-size:1.4em;cursor:pointer">&#x2715;</button>
        <h3 style="color:var(--accent);margin:0 0 12px">${escHtml(title)}</h3>
        <div id="file-modal-body">${content}</div>
    </div>`;
    modal.style.display = 'flex';
}

let _ensurePollTimer = null;
let _openTries = {};   // per bestand: aantal open-pogingen (voorkomt oneindige lus)

function _stopEnsurePoll() {
    if (_ensurePollTimer) { clearTimeout(_ensurePollTimer); _ensurePollTimer = null; }
}

function _offlineShell(inner) {
    const bodyEl = document.getElementById('file-modal-body');
    if (bodyEl) bodyEl.innerHTML = `<div style="text-align:center;padding:22px 16px">${inner}</div>`;
}

function _manageFallbackButtons() {
    return `<div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:14px">
        <button class="btn-secondary btn-small" onclick="switchView('manage');document.getElementById('file-modal')&&document.getElementById('file-modal').remove()">&#9881; Handmatig via Beheer</button>
        <button class="btn-secondary btn-small" onclick="_stopEnsurePoll();document.getElementById('file-modal').remove()">Sluiten</button>
    </div>`;
}

// Offline: transparante flow. Bij een specifiek bestand (relPath) wachten we tot de schijf
// LOKAAL leesbaar is en lezen hem dan rechtstreeks (schijf blijft bij jou). USB/IP is een
// expliciete keuze (knop), niet meer automatisch.
async function _showDiskOfflinePanel(label, context, retryFn, relPath) {
    _stopEnsurePoll();
    if (!label) {
        _offlineShell(`<div style="font-size:2.2em">&#128268;</div>
            <div style="color:var(--warn);font-weight:bold;margin:8px 0">Schijf niet aangesloten</div>
            <div style="color:var(--text)">${escHtml(context || '')}</div>${_manageFallbackButtons()}`);
        return;
    }
    if (relPath) {
        const L = escHtml(label).replace(/'/g,"\\'");
        _offlineShell(`<div style="font-size:2.2em">&#128190;</div>
            <div style="color:var(--warn);font-weight:bold;margin:8px 0">Sluit schijf <span style="color:var(--accent)">${escHtml(label)}</span> aan op je machine</div>
            <div style="color:var(--text)">Zodra hij zichtbaar is, lees ik het bestand er rechtstreeks van &mdash; de schijf blijft gewoon bij jou.</div>
            <div class="meta" style="margin-top:8px">&#128260; Ik kijk automatisch mee...</div>
            <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:14px">
                <button class="btn-secondary btn-small" onclick="coupleDisk('${L}')">&#128268; Toch via USB/IP koppelen</button>
                <button class="btn-secondary btn-small" onclick="_stopEnsurePoll();document.getElementById('file-modal').remove()">Sluiten</button>
            </div>`);
        _pollLocalAvailable(label, relPath, retryFn);
        return;
    }
    // Geen specifiek bestand (bv. map openen) -> USB/IP koppel-flow.
    _offlineShell(`<div style="font-size:2.2em">&#128268;</div>
        <div style="color:var(--accent2);font-weight:bold;margin:8px 0">Schijf <span style="color:var(--accent)">${escHtml(label)}</span> koppelen...</div>
        <div class="meta">Even geduld — ik zoek de schijf en koppel hem.</div>`);
    _ensureDiskAndRetry(label, retryFn, false);
}

async function _pollLocalAvailable(label, relPath, retryFn) {
    let data;
    try {
        data = await fetch(`/api/remote/file-available?label=${encodeURIComponent(label)}&path=${encodeURIComponent(relPath)}`).then(r => r.json());
    } catch(e) {
        _ensurePollTimer = setTimeout(() => _pollLocalAvailable(label, relPath, retryFn), 3000);
        return;
    }
    if (!document.getElementById('file-modal-body')) { _stopEnsurePoll(); return; }
    if (data.available) {
        _stopEnsurePoll();
        const key = label + '|' + relPath;
        _openTries[key] = (_openTries[key] || 0) + 1;
        if (_openTries[key] > 2) {
            // Schijf is zichtbaar, maar openen lukt herhaaldelijk niet -> stop (geen lus).
            const L = escHtml(label).replace(/'/g,"\\'");
            _offlineShell(`<div style="font-size:2.2em">&#9888;</div>
                <div style="color:var(--warn);font-weight:bold;margin:8px 0">Schijf <span style="color:var(--accent)">${escHtml(label)}</span> is zichtbaar, maar het bestand kon niet gelezen worden</div>
                <div style="color:var(--text)">Draait op deze machine de ArchSW-loc agent <strong>v0.2.0</strong> (met lokaal lezen)? Herinstalleer hem anders via de knop hieronder.</div>
                <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:14px">
                    <a href="/netwerk-usb" target="_blank" class="btn-secondary btn-small" style="text-decoration:none">&#128295; Agent (her)installeren</a>
                    <button class="btn-secondary btn-small" onclick="coupleDisk('${L}')">&#128268; Via USB/IP koppelen</button>
                    <button class="btn-secondary btn-small" onclick="_stopEnsurePoll();document.getElementById('file-modal').remove()">Sluiten</button>
                </div>`);
            return;
        }
        _offlineShell(`<div style="font-size:2.2em">&#9989;</div>
            <div style="color:var(--accent);font-weight:bold;margin:8px 0">Schijf gevonden</div>
            <div class="meta">Bestand openen...</div>`);
        if (typeof retryFn === 'function') { setTimeout(retryFn, 500); }
        return;
    }
    _ensurePollTimer = setTimeout(() => _pollLocalAvailable(label, relPath, retryFn), 3000);
}

async function _ensureDiskAndRetry(label, retryFn, polling) {
    let data;
    try {
        data = await fetch('/api/remote/ensure-disk', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({label})
        }).then(r => r.json());
    } catch(e) {
        _offlineShell(`<div style="color:var(--err)">Fout bij koppelen: ${escHtml(e.message)}</div>${_manageFallbackButtons()}`);
        return;
    }
    // Modal ondertussen gesloten? Stop.
    if (!document.getElementById('file-modal-body')) { _stopEnsurePoll(); return; }

    if (data.status === 'coupled' || data.status === 'already') {
        _stopEnsurePoll();
        _offlineShell(`<div style="font-size:2.2em">&#9989;</div>
            <div style="color:var(--accent);font-weight:bold;margin:8px 0">${escHtml(data.message || 'Gekoppeld')}</div>
            <div class="meta">Bestand openen...</div>`);
        if (typeof retryFn === 'function') { setTimeout(retryFn, 700); }
        return;
    }
    if (data.status === 'not_present') {
        // Vraag de schijf aan te sluiten en blijf pollen (auto-detect).
        _offlineShell(`<div style="font-size:2.2em">&#128190;</div>
            <div style="color:var(--warn);font-weight:bold;margin:8px 0">Sluit schijf <span style="color:var(--accent)">${escHtml(label)}</span> aan</div>
            <div style="color:var(--text)">op <strong>${escHtml(data.host || 'deze machine')}</strong> (kijk naar het stickerlabel).</div>
            <div class="meta" style="margin-top:10px">&#128260; Ik kijk automatisch mee en open het bestand zodra de schijf verschijnt...</div>
            ${_manageFallbackButtons()}`);
        _ensurePollTimer = setTimeout(() => _ensureDiskAndRetry(label, retryFn, true), 3000);
        return;
    }
    if (data.status === 'no_agent') {
        _stopEnsurePoll();
        _offlineShell(`<div style="font-size:2.2em">&#129302;</div>
            <div style="color:var(--warn);font-weight:bold;margin:8px 0">Automatisch koppelen nog niet ingesteld</div>
            <div style="color:var(--text)">${escHtml(data.message || '')}</div>
            <div style="margin-top:12px"><a href="/netwerk-usb" target="_blank" class="btn-secondary btn-small" style="text-decoration:none">&#128295; Archief-agent installeren</a></div>
            ${_manageFallbackButtons()}`);
        return;
    }
    // wrong_disk / ambiguous / error
    _offlineShell(`<div style="font-size:2.2em">&#9888;</div>
        <div style="color:var(--warn);font-weight:bold;margin:8px 0">${escHtml(data.message || 'Kon niet automatisch koppelen')}</div>
        <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:12px">
            <button class="btn-secondary btn-small" onclick="_ensureDiskAndRetry('${escHtml(label).replace(/'/g,"\\'")}', null, false)">&#8635; Opnieuw proberen</button>
        </div>${_manageFallbackButtons()}`);
    if (polling) { _ensurePollTimer = setTimeout(() => _ensureDiskAndRetry(label, retryFn, true), 4000); }
}

// Koppel een schijf zonder een specifiek bestand te openen (knop bij offline resultaten).
function coupleDisk(label) {
    _openModal('Schijf koppelen', '<div class="status-msg">Even kijken...</div>');
    _ensureDiskAndRetry(label, () => {
        const m = document.getElementById('file-modal'); if (m) m.remove();
        if (typeof doSearch === 'function') { try { doSearch(new Event('submit')); } catch(_) {} }
    }, false);
}

async function showFile(label, relPath, filename, isRetry=false) {
    // Toon bestand via /api/smart-serve — detecteer type op extensie
    if (!isRetry) { _openTries[label + '|' + relPath] = 0; }  // gebruikersklik reset de teller
    const ext = filename.split('.').pop().toLowerCase();
    const imageExts = ['jpg','jpeg','png','gif','bmp','webp','tiff','svg'];
    const textExts = ['txt','log','md','csv','ini','cfg','bat','sh','py','js','html','css','xml','json','yaml','yml','sql'];
    const url = `/api/smart-serve?label=${encodeURIComponent(label)}&path=${encodeURIComponent(relPath)}`;

    if (imageExts.includes(ext)) {
        _openModal(filename, `<img src="${url}" style="max-width:100%;border-radius:6px" onerror="this.parentNode.innerHTML='<div class=\\'status-msg error\\'>Kan afbeelding niet laden — schijf gemount?</div>'">`);
    } else if (textExts.includes(ext)) {
        _openModal(filename, '<div class="status-msg">Laden...</div>');
        try {
            const resp = await fetch(url);
            if (!resp.ok) {
                // Probeer JSON-fout te lezen (endpoints geven JSON terug bij 404)
                let errMsg = `HTTP ${resp.status}`;
                let notMounted = false, errLabel = label;
                try { const j = await resp.json(); errMsg = j.error || errMsg; notMounted = !!j.not_mounted; errLabel = j.label || label; } catch(_) {}
                if (notMounted) { _showDiskOfflinePanel(errLabel, errMsg, () => showFile(label, relPath, filename, true), relPath); return; }
                document.getElementById('file-modal-body').innerHTML =
                    `<div class="status-msg error">&#9888; ${escHtml(errMsg)}</div>`;
                return;
            }
            const text = await resp.text();
            const lines = text.split('\n').length;
            document.getElementById('file-modal-body').innerHTML =
                `<div class="meta" style="margin-bottom:6px">${lines} regels</div>` +
                `<pre style="white-space:pre-wrap;word-break:break-all;color:var(--text);font-size:0.85em;max-height:60vh;overflow:auto;background:var(--code-bg);padding:12px;border-radius:6px">${escHtml(text.substring(0,50000))}` +
                (text.length > 50000 ? '\n\n[... bestand te groot, eerste 50.000 tekens getoond]' : '') +
                `</pre>`;
        } catch(e) {
            document.getElementById('file-modal-body').innerHTML = `<div class="status-msg error">Kan bestand niet laden: ${escHtml(e.message)}</div>`;
        }
    } else {
        // Overige types: controleer eerst of bestand beschikbaar is
        _openModal(filename, '<div class="status-msg">Controleren...</div>');
        try {
            const check = await fetch(url, {method: 'HEAD'});
            if (!check.ok) {
                let errMsg = `Bestand niet beschikbaar (HTTP ${check.status})`;
                let notMounted = false, errLabel = label;
                try { const j = await (await fetch(url)).json(); errMsg = j.error || errMsg; notMounted = !!j.not_mounted; errLabel = j.label || label; } catch(_) {}
                if (notMounted) { _showDiskOfflinePanel(errLabel, errMsg, () => showFile(label, relPath, filename, true), relPath); return; }
                document.getElementById('file-modal-body').innerHTML = `<div class="status-msg error">&#9888; ${escHtml(errMsg)}</div>`;
                return;
            }
        } catch(_) {}
        document.getElementById('file-modal-body').innerHTML = `<div style="text-align:center;padding:20px">
            <div style="font-size:3em">&#128196;</div>
            <div style="color:var(--text);margin:10px 0">Dit bestandstype kan niet direct worden weergegeven</div>
            <a href="${url}" download="${escHtml(filename)}" style="display:inline-block;padding:10px 20px;background:var(--accent);color:#000;border-radius:6px;text-decoration:none;font-weight:bold">&#11015; Download ${escHtml(filename)}</a>
        </div>`;
    }
}

async function showFileByPath(label, fullPath, filename) {
    // Voor content-zoekresultaten: Recoll geeft volledig pad terug
    // Controleer of het bestand bestaat door direct op te vragen
    const ext = filename.split('.').pop().toLowerCase();
    const imageExts = ['jpg','jpeg','png','gif','bmp','webp','tiff','svg'];
    const textExts = ['txt','log','md','csv','ini','cfg','bat','sh','py','js','html','css','xml','json','yaml','yml','sql'];
    const url = `/api/file-serve-path?fullpath=${encodeURIComponent(fullPath)}`;

    if (imageExts.includes(ext)) {
        _openModal(filename, `<img src="${url}" style="max-width:100%;border-radius:6px" onerror="this.parentNode.innerHTML='<div class=\\'status-msg error\\'>Kan afbeelding niet laden</div>'">`);
    } else if (textExts.includes(ext)) {
        _openModal(filename, '<div class="status-msg">Laden...</div>');
        try {
            const resp = await fetch(url);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const text = await resp.text();
            document.getElementById('file-modal-body').innerHTML =
                `<pre style="white-space:pre-wrap;word-break:break-all;color:var(--text);font-size:0.85em;max-height:60vh;overflow:auto;background:var(--code-bg);padding:12px;border-radius:6px">${escHtml(text.substring(0,50000))}` +
                (text.length > 50000 ? '\n\n[... eerste 50.000 tekens getoond]' : '') + '</pre>';
        } catch(e) {
            document.getElementById('file-modal-body').innerHTML = `<div class="status-msg error">Kan bestand niet laden: ${escHtml(e.message)}</div>`;
        }
    } else {
        _openModal(filename, `<div style="text-align:center;padding:20px">
            <div style="font-size:3em">&#128196;</div>
            <div style="color:var(--text);margin:10px 0">Dit bestandstype kan niet direct worden weergegeven</div>
            <a href="${url}" download="${escHtml(filename)}" style="display:inline-block;padding:10px 20px;background:var(--accent);color:#000;border-radius:6px;text-decoration:none;font-weight:bold">&#11015; Download ${escHtml(filename)}</a>
        </div>`);
    }
}

async function openExternal(label, relPath, filename='') {
    try {
        const resp = await fetch('/api/file-open', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({label, path: relPath}),
        });
        const data = await resp.json();
        if (data.not_mounted) {
            _openModal(filename || 'Bestand openen', '<div class="status-msg">Schijfcontrole...</div>');
            _showDiskOfflinePanel(data.label || label, data.error || data.message || 'Schijf niet beschikbaar', () => openExternal(label, relPath, filename));
            return;
        }
        _openModal(filename || 'Bestand openen', `
            <div class="status-msg ${data.success ? 'success' : 'error'}">${escHtml(data.message || (data.success ? 'Openen gestart' : 'Openen mislukt'))}</div>
            ${data.path ? `<div class="meta" style="margin-top:8px">${escHtml(data.path)}</div>` : ''}
        `);
    } catch (e) {
        _openModal(filename || 'Bestand openen', `<div class="status-msg error">Openen mislukt: ${escHtml(e.message)}</div>`);
    }
}

async function openExternalByPath(label, fullPath, filename='') {
    try {
        const resp = await fetch('/api/file-open-path', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({label, fullpath: fullPath}),
        });
        const data = await resp.json();
        if (data.not_mounted) {
            _openModal(filename || 'Bestand openen', '<div class="status-msg">Schijfcontrole...</div>');
            _showDiskOfflinePanel(data.label || label, data.error || data.message || 'Schijf niet beschikbaar', () => openExternalByPath(label, fullPath, filename));
            return;
        }
        _openModal(filename || 'Bestand openen', `
            <div class="status-msg ${data.success ? 'success' : 'error'}">${escHtml(data.message || (data.success ? 'Openen gestart' : 'Openen mislukt'))}</div>
            ${data.path ? `<div class="meta" style="margin-top:8px">${escHtml(data.path)}</div>` : ''}
        `);
    } catch (e) {
        _openModal(filename || 'Bestand openen', `<div class="status-msg error">Openen mislukt: ${escHtml(e.message)}</div>`);
    }
}

async function openDir(label, dirPath) {
    _openModal(`Map: ${dirPath || label}`, '<div class="status-msg">Laden...</div>');
    await _loadDir(label, dirPath);
}

async function _loadDir(label, dirPath) {
    try {
        const url = `/api/dir-listing?label=${encodeURIComponent(label)}&path=${encodeURIComponent(dirPath)}`;
        const resp = await fetch(url);
        const data = await resp.json();
        if (data.error) {
            if (data.not_mounted) {
                _showDiskOfflinePanel(data.label || label, data.error, () => _loadDir(label, dirPath));
            } else {
                document.getElementById('file-modal-body').innerHTML = `<div class="status-msg error">${escHtml(data.error)}</div>`;
            }
            return;
        }
        let html = `<div class="meta" style="margin-bottom:8px;font-size:0.85em">${escHtml(data.source_root)} / ${escHtml(data.path)}</div>`;
        // Bovenliggende map navigatie
        if (data.path) {
            const parentPath = data.path.includes('/') ? data.path.substring(0, data.path.lastIndexOf('/')) : '';
            html += `<div style="margin-bottom:6px"><button class="btn-secondary btn-small" onclick="_loadDir('${escHtml(label).replace(/'/g,"\\'")}','${escHtml(parentPath).replace(/'/g,"\\'")}')">&#11014; Bovenliggende map</button></div>`;
        }
        html += `<table style="width:100%;border-collapse:collapse">`;
        html += `<tr style="color:var(--muted);font-size:0.85em"><th style="text-align:left;padding:4px">Naam</th><th style="text-align:right;padding:4px">Grootte</th><th style="text-align:left;padding:4px">Gewijzigd</th></tr>`;
        data.entries.forEach(e => {
            if (e.is_dir) {
                html += `<tr style="border-top:1px solid var(--hover)">
                    <td style="padding:5px"><button class="btn-secondary btn-small" style="text-align:left;width:100%" onclick="_loadDir('${escHtml(label).replace(/'/g,"\\'")}','${escHtml(e.rel_path).replace(/'/g,"\\'")}')">&#128193; ${escHtml(e.name)}/</button></td>
                    <td></td><td style="color:var(--muted);font-size:0.85em;padding:5px">${escHtml(e.modified)}</td>
                </tr>`;
            } else {
                const size = e.size ? (e.size > 1048576 ? (e.size/1048576).toFixed(1)+' MB' : e.size > 1024 ? (e.size/1024).toFixed(0)+' KB' : e.size+' B') : '';
                const canView = /\.(jpg|jpeg|png|gif|txt|log|md|csv|ini|py|js|html|css|xml|json|bat|sh)$/i.test(e.name);
                html += `<tr style="border-top:1px solid var(--hover)">
                    <td style="padding:5px">&#128196; ${escHtml(e.name)}${canView ? ` <button class="btn-secondary btn-small" onclick="showFile('${escHtml(label).replace(/'/g,"\\'")}','${escHtml(e.rel_path).replace(/'/g,"\\'")}','${escHtml(e.name).replace(/'/g,"\\'")}')">&#128065; Toon</button>` : ''} <button class="btn-secondary btn-small" onclick="openExternal('${escHtml(label).replace(/'/g,"\\'")}','${escHtml(e.rel_path).replace(/'/g,"\\'")}','${escHtml(e.name).replace(/'/g,"\\'")}')">&#11016; ${t('openExternal')}</button></td>
                    <td style="text-align:right;color:var(--muted);font-size:0.85em;padding:5px">${size}</td>
                    <td style="color:var(--muted);font-size:0.85em;padding:5px">${escHtml(e.modified)}</td>
                </tr>`;
            }
        });
        html += '</table>';
        if (data.truncated) html += `<div class="meta" style="margin-top:8px">Eerste 500 items getoond</div>`;
        document.getElementById('file-modal-body').innerHTML = html;
        // Update modal titel
        document.querySelector('#file-modal h3').textContent = `Map: ${data.path || label}`;
    } catch(e) {
        document.getElementById('file-modal-body').innerHTML = `<div class="status-msg error">Fout: ${escHtml(e.message)}</div>`;
    }
}

// === BEHEER: Aangesloten schijven ===

function detectManualMediaType(disk, part) {
    return (disk && disk.media_type) || (part && part.media_type) || 'usb_hdd';
}

function isTechnicalPartition(part) {
    const fstype = String(part?.fstype || '').trim().toLowerCase();
    return ['swap', 'linux-swap', 'linux-swap(v1)', 'crypto_luks', 'lvm2_member'].includes(fstype);
}

function isIngestablePartition(part) {
    const fstype = String(part?.fstype || '').trim();
    return !!fstype && !isTechnicalPartition(part);
}

function getUnknownIngestablePartitions(disk) {
    return (disk?.partitions || []).filter(p => !p.known_label && isIngestablePartition(p));
}

function shouldTreatAsWholeDiskCandidate(disk) {
    const parts = disk?.partitions || [];
    const hasKnown = parts.some(p => p.known_label);
    const unknownIngestable = getUnknownIngestablePartitions(disk);
    return !hasKnown && unknownIngestable.length > 0 && parts.length > 1;
}

function sanitizeMountComponent(value) {
    const cleaned = String(value || '').trim().replace(/[^A-Za-z0-9._-]+/g, '_');
    return cleaned.replace(/^[._-]+|[._-]+$/g, '') || 'partitie';
}

function partitionMountName(part, usedNames) {
    const deviceName = String(part?.device || 'part').split('/').pop();
    const labelName = sanitizeMountComponent(part?.label || '');
    const candidates = [];
    if (labelName) {
        candidates.push(labelName);
        candidates.push(`${labelName}__${deviceName}`);
    }
    candidates.push(deviceName);
    for (const candidate of candidates) {
        if (!usedNames.has(candidate)) {
            usedNames.add(candidate);
            return candidate;
        }
    }
    let suffix = 2;
    let finalName = `${candidates[0]}-${suffix}`;
    while (usedNames.has(finalName)) {
        suffix += 1;
        finalName = `${candidates[0]}-${suffix}`;
    }
    usedNames.add(finalName);
    return finalName;
}

function getDiskKnownLabel(disk) {
    const labels = [...new Set((disk?.partitions || [])
        .filter(p => p.known_label && isIngestablePartition(p))
        .map(p => p.known_label))];
    return labels.length === 1 ? labels[0] : '';
}

function getKnownIngestableParts(disk, label) {
    return (disk?.partitions || [])
        .filter(p => p.known_label === label && isIngestablePartition(p))
        .slice()
        .sort((a, b) => String(a.device || '').localeCompare(String(b.device || '')));
}

function getExpectedMountpointForPart(disk, part, label) {
    const parts = getKnownIngestableParts(disk, label);
    if (parts.length <= 1) return `${ARCHIVE_MOUNT_BASE}/${label}`;
    const usedNames = new Set();
    for (const current of parts) {
        const target = `${ARCHIVE_MOUNT_BASE}/${label}/${partitionMountName(current, usedNames)}`;
        if (current.device === part.device) return target;
    }
    return `${ARCHIVE_MOUNT_BASE}/${label}`;
}

function getKnownDiskState(disk) {
    const label = getDiskKnownLabel(disk);
    if (!label) return null;
    const ingestable = getKnownIngestableParts(disk, label);
    const technical = (disk?.partitions || []).filter(p => isTechnicalPartition(p));
    const wrongMounts = ingestable.filter(p => p.mount_state !== 'ro_archive' || p.mountpoint !== getExpectedMountpointForPart(disk, p, label));
    const running = ingestable.some(p => p.mount_state === 'rw');
    const allUnmounted = ingestable.length > 0 && ingestable.every(p => p.mount_state === 'not_mounted');
    const hasMounted = ingestable.some(p => p.mount_state !== 'not_mounted');
    return {
        label,
        ingestable,
        technicalCount: technical.length,
        ready: ingestable.length > 0 && wrongMounts.length === 0 && !running && !allUnmounted,
        allUnmounted,
        hasMounted,
        needsRepair: ingestable.length > 0 && !allUnmounted && (wrongMounts.length > 0 || running),
        wrongMounts,
    };
}

function setManualSelection(device, label, mediaType, sourceText='') {
    const deviceEl = document.getElementById('new-device');
    const labelEl = document.getElementById('new-label');
    const typeEl = document.getElementById('new-type');
    const statusEl = document.getElementById('manual-prefill-status');
    if (deviceEl) deviceEl.value = device || '';
    if (labelEl) labelEl.value = label || '';
    if (typeEl) typeEl.value = mediaType || 'usb_hdd';
    if (statusEl) {
        if (device && label) {
            const base = `Voorgevuld: ${label} via ${device}`;
            statusEl.textContent = sourceText ? `${base} (${sourceText})` : base;
        } else {
            statusEl.textContent = 'Nog niets gekozen. Gebruik bij voorkeur een knop op een schijfkaart hierboven.';
        }
    }
}

function prefillManualFromPartition(device, label, mediaType, sourceText='', scrollIntoView=false) {
    setManualSelection(device, label, mediaType, sourceText);
    if (scrollIntoView) {
        const panel = document.getElementById('new-device');
        if (panel) panel.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
}

function getBestKnownPartition(disks) {
    const known = [];
    (disks || []).forEach(disk => {
        const diskState = getKnownDiskState(disk);
        if (diskState && diskState.ingestable.length > 1) return;
        (disk.partitions || []).forEach(part => {
            if (!part.known_label) return;
            known.push({
                device: part.device,
                label: part.known_label,
                mediaType: detectManualMediaType(disk, part),
                sourceText: part.mount_state === 'ro_archive'
                    ? 'aangesloten archief-pad'
                    : part.mount_state === 'rw'
                        ? 'schrijfbaar gemount'
                        : part.mount_state === 'ro_other'
                            ? 'RO op andere locatie'
                            : 'bekende disk',
                rank: part.mount_state === 'ro_archive'
                    ? 0
                    : part.mount_state === 'rw'
                        ? 1
                        : part.mount_state === 'ro_other'
                            ? 2
                            : 3,
            });
        });
    });
    known.sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label));
    return known[0] || null;
}

async function prefillAndRun(device, label, mediaType, action) {
    prefillManualFromPartition(device, label, mediaType, 'gekozen in aangesloten schijven', true);
    if (action === 'scan') return startScan();
    if (action === 'index') return startIndex();
    if (action === 'all') return runFullIngest();
}

async function prepareKnownDisk(label) {
    showProgress(`Koppeling van ${label} herstellen...`);
    const resp = await fetch('/api/prepare-label', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({label})
    });
    const data = await resp.json();
    setProgress(100, data.message || 'Klaar', data.success ? 'success' : 'error');
    if (data.success) {
        await loadConnectedDisks();
        await loadMountedDisks();
        loadStats();
    }
}

function startScanForLabel(label) {
    document.getElementById('new-label').value = label;
    return startScan();
}

function startIndexForLabel(label) {
    document.getElementById('new-label').value = label;
    return startIndex();
}

async function loadConnectedDisks() {
    document.getElementById('disk-list').innerHTML = '<div class="status-msg">Schijven detecteren...</div>';
    let data, stats, activeTask;
    try {
        [data, stats, activeTask] = await Promise.all([
            fetch('/api/disks').then(r => r.json()),
            fetch('/api/stats').then(r => r.json()),
            fetch('/api/progress/active').then(r => r.json()).catch(() => null),
        ]);
    } catch(e) {
        document.getElementById('disk-list').innerHTML = `<div class="status-msg error">Fout bij detectie: ${escHtml(e.message)}</div>`;
        return;
    }
    detectedDisks = data.disks || [];

    if (detectedDisks.length === 0) {
        document.getElementById('disk-list').innerHTML =
            '<div class="empty">Geen externe schijven gevonden. Sluit een USB-schijf aan en klik Vernieuwen.</div>';
        document.getElementById('btn-batch').style.display = 'none';
        setManualSelection('', '', 'usb_hdd');
        return;
    }

    // Volgende vrije labelnummer bepalen
    let maxNum = 0;
    (stats.media_labels || []).forEach(l => {
        const m = l.match(/ARCHIVE-DISK-(\d+)/);
        if (m) maxNum = Math.max(maxNum, parseInt(m[1]));
    });

    let hasNewDisks = false;
    let html = '';

    detectedDisks.forEach((disk, diskIdx) => {
        const relevantParts = (disk.partitions || []).filter(p => p.known_label || isIngestablePartition(p));
        const stateParts = relevantParts.length > 0 ? relevantParts : (disk.partitions || []);
        const wholeDiskCandidate = shouldTreatAsWholeDiskCandidate(disk);
        const knownDiskState = getKnownDiskState(disk);
        const unknownIngestable = getUnknownIngestablePartitions(disk);
        const technicalCount = (disk.partitions || []).filter(p => !p.known_label && isTechnicalPartition(p)).length;
        const activeForDisk = !!(activeTask && activeTask.task_id && activeTask.status === 'running'
            && knownDiskState && (activeTask.details || {}).label === knownDiskState.label);

        // Kleur op basis van disk-status: groen als alles OK, oranje als actie nodig, blauw als nieuw
        const hasKnown = stateParts.some(p => p.known_label);
        const hasUnknown = stateParts.some(p => !p.known_label);
        const allRoArchive = stateParts.length > 0 && stateParts.every(p => p.mount_state === 'ro_archive');
        const anyRw = stateParts.some(p => p.mount_state === 'rw');
        const anyNotMounted = stateParts.some(p => p.mount_state === 'not_mounted');
        const anyRoOther = stateParts.some(p => p.mount_state === 'ro_other');
        const partSummary = `${disk.partitions.length} partities`;
        const ingestableSummary = unknownIngestable.length > 0 ? `${unknownIngestable.length} scanbaar` : '';

        let diskBorderColor = 'var(--accent)'; // groen = alles goed
        if (hasUnknown) diskBorderColor = 'var(--accent2)';  // blauw = nieuwe schijf
        if (anyRw || anyRoOther) diskBorderColor = 'var(--warn)'; // oranje = actie nodig

        html += `<div style="border:1px solid ${diskBorderColor}; border-radius:8px; padding:12px; margin:8px 0; background:rgba(0,0,0,0.2)">`;

        // Disk-header
        html += `<div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:8px">`;
        html += `<strong style="color:var(--accent2)">${escHtml(disk.parent_device)}</strong>`;
        html += `<span class="meta">${escHtml(disk.total_size)}</span>`;
        html += `<span class="meta">${escHtml(disk.model || 'Onbekend model')}</span>`;
        html += `<span class="meta">${escHtml(partSummary)}${ingestableSummary ? ' • ' + escHtml(ingestableSummary) : ''}</span>`;
        if (knownDiskState) {
            html += `<span style="background:var(--ok-bg); border:1px solid var(--accent); border-radius:4px; padding:2px 8px; color:var(--accent); font-weight:bold">&#128269; ${escHtml(knownDiskState.label)}</span>`;
            if (activeForDisk) {
                html += `<span class="badge-mounted">Scan loopt al</span>`;
            } else if (knownDiskState.ready) {
                html += `<span class="badge-mounted">&#10003; Klaar om te scannen</span>`;
            } else if (knownDiskState.allUnmounted) {
                html += `<span class="badge-unmounted">Niet gekoppeld</span>`;
            } else {
                html += `<span class="badge-rw">&#9888; Koppeling herstellen</span>`;
            }
            if (knownDiskState.technicalCount > 0) {
                html += `<span class="meta">${knownDiskState.technicalCount} technische partitie(s) worden overgeslagen</span>`;
                html += `<span class="inline-note">Technisch = hulppartitie van de schijf; gewone archiefbestanden staan hier meestal niet op.</span>`;
            }
            if (!activeForDisk && knownDiskState.allUnmounted) {
                html += `<span class="inline-note">Softwarematig losgekoppeld. Je kunt de schijf veilig loshalen of opnieuw koppelen.</span>`;
                html += `<button class="btn-small" onclick="prepareKnownDisk('${escHtml(knownDiskState.label).replace(/'/g,"\\'")}')" style="background:var(--accent2);color:#000">&#128279; Koppel opnieuw</button>`;
            } else if (!activeForDisk && knownDiskState.needsRepair) {
                html += `<button class="btn-small" onclick="prepareKnownDisk('${escHtml(knownDiskState.label).replace(/'/g,"\\'")}')" style="background:var(--accent2);color:#000">&#128295; Koppel correct</button>`;
            }
            if (!activeForDisk && knownDiskState.ready) {
                html += `<button class="btn-secondary btn-small" onclick="startScanForLabel('${escHtml(knownDiskState.label).replace(/'/g,"\\'")}')">&#9654; Hervat scan</button>`;
                html += `<button class="btn-secondary btn-small" onclick="startIndexForLabel('${escHtml(knownDiskState.label).replace(/'/g,"\\'")}')">&#128269; Index</button>`;
            }
            html += `<button class="btn-secondary btn-small" onclick="openDir('${escHtml(knownDiskState.label).replace(/'/g,"\\'")}','')">&#128193; Bladeren</button>`;
            const stickerUnknown = knownDiskState.ingestable.some(p => !p.sticker_confirmed);
            if (stickerUnknown) {
                html += `<button class="btn-secondary btn-small" onclick="confirmStickerForLabel('${escHtml(knownDiskState.label).replace(/'/g,"\\'")}')">&#x1F3F7; Sticker OK</button>`;
            }
            if (knownDiskState.hasMounted) {
                html += `<button class="btn-warn btn-small" onclick="ejectPartition('${escHtml(disk.parent_device).replace(/'/g,"\\'")}','${escHtml(knownDiskState.label).replace(/'/g,"\\'")}')">&#9167; Uitwerpen</button>`;
            }
        } else if (hasKnown && !hasUnknown && allRoArchive) {
            html += `<span class="badge-mounted">&#10003; Gereed voor archief</span>`;
        } else if (anyRw) {
            html += `<span class="badge-rw">&#9888; Schrijfbaar gemount</span>`;
        } else if (anyRoOther) {
            html += `<span class="badge-rw">&#9888; Gemount op verkeerde locatie</span>`;
        } else if (anyNotMounted) {
            html += `<span class="badge-unmounted">Niet gemount</span>`;
        }
        if (wholeDiskCandidate) {
            hasNewDisks = true;
            const newNum = maxNum + 1;
            maxNum++;
            const suggestedLabel = `ARCHIVE-DISK-${String(newNum).padStart(3,'0')}`;
            html += `<span style="background:var(--field-ro); border:1px solid var(--accent2); border-radius:4px; padding:2px 8px; color:var(--accent2)">1 fysieke schijf</span>`;
            if (technicalCount > 0) {
                html += `<span class="meta">${technicalCount} technische partitie(s) overgeslagen</span>`;
            }
            html += `<input type="text" class="disk-label-input" data-diskidx="${diskIdx}"
                value="${escHtml(suggestedLabel)}"
                style="width:200px; font-weight:bold; color:var(--accent); background:var(--field-ro); border:1px dashed var(--accent); padding:3px 6px; border-radius:4px"
                title="Eén archief-label voor deze fysieke schijf">`;
            html += `<button class="btn-small" onclick="ingestWholeDisk(${diskIdx})" style="background:var(--accent2);color:#000">&#9654; Verwerk hele schijf</button>`;
        }
        html += `</div>`;

        // Partitie-rijen
        disk.partitions.forEach((p, partIdx) => {
            const safeLabel = (p.known_label || '').replace(/'/g, "\\'");
            const safeDevice = p.device.replace(/'/g, "\\'");
            const safeMediaType = detectManualMediaType(disk, p).replace(/'/g, "\\'");
            const technical = isTechnicalPartition(p);
            const unknownWholeDiskMember = wholeDiskCandidate && !p.known_label;
            const knownDiskMember = knownDiskState && p.known_label === knownDiskState.label && isIngestablePartition(p);
            const expectedMountpoint = knownDiskMember ? getExpectedMountpointForPart(disk, p, knownDiskState.label) : '';

            html += `<div style="border-top:1px solid var(--hover); padding:8px 0; display:flex; align-items:center; gap:8px; flex-wrap:wrap">`;

            // Volume label badge
            html += `<span style="min-width:120px; color:var(--text)">&#128194; ${escHtml(p.label || p.device)}</span>`;
            html += `<span class="meta">${escHtml(p.fstype || '?')} &bull; ${escHtml(p.size)}</span>`;
            html += `<span class="meta">${escHtml(p.device)}</span>`;

            // Bekend/onbekend badge
            if (technical) {
                html += `<span style="background:var(--warn-bg); border:1px solid #aa8844; border-radius:4px; padding:2px 8px; color:var(--warn)">Technische partitie</span>`;
                html += `<span class="meta">Wordt niet gescand of geïndexeerd</span>`;
                html += `<span class="inline-note">Dit is meestal een hulp-, herstel- of swap-partitie en geen archiefdeel met gewone bestanden.</span>`;
            } else if (p.known_label) {
                const matchIcon = p.match_method === 'uuid' ? '&#128273;' : p.match_method === 'volume_label' ? '&#127991;' : '&#128269;';
                html += `<span style="background:var(--ok-bg); border:1px solid var(--accent); border-radius:4px; padding:2px 8px; color:var(--accent); font-weight:bold">${matchIcon} ${escHtml(p.known_label)}</span>`;
                if (p.sticker_confirmed) {
                    html += `<span style="color:var(--muted); font-size:0.8em">&#x1F3F7; sticker OK</span>`;
                } else {
                    html += `<span style="color:var(--warn); font-size:0.8em">&#x1F3F7; sticker nog niet bevestigd</span>`;
                }
                // Scan-status badge
                if (p.last_scan) {
                    const d = p.last_scan.split('-');
                    const datumNl = d[2] + '-' + d[1] + '-' + d[0];
                    html += `<span style="color:var(--accent); font-size:0.78em" title="${(p.last_scan_files||0).toLocaleString('nl-NL')} bestanden gescand">&#10003; Gescand op ${datumNl}</span>`;
                } else {
                    html += `<span style="color:var(--muted); font-size:0.78em">&#9675; Nog niet gescand</span>`;
                }
            } else if (unknownWholeDiskMember) {
                html += `<span style="background:var(--field-ro); border:1px solid var(--accent2); border-radius:4px; padding:2px 8px; color:var(--accent2)">Onderdeel van dezelfde fysieke schijf</span>`;
            } else {
                hasNewDisks = true;
                const newNum = maxNum + 1;
                maxNum++;
                const suggestedLabel = `ARCHIVE-DISK-${String(newNum).padStart(3,'0')}`;
                html += `<span style="background:var(--field-ro); border:1px solid var(--accent2); border-radius:4px; padding:2px 8px; color:var(--accent2)">&#10024; Nieuwe schijf</span>`;
                html += `<input type="text" class="disk-label-input" data-diskidx="${diskIdx}" data-partidx="${partIdx}"
                    value="${escHtml(suggestedLabel)}"
                    style="width:180px; font-weight:bold; color:var(--accent); background:var(--field-ro); border:1px dashed var(--accent); padding:3px 6px; border-radius:4px"
                    title="Pas het label aan als dit medium al een naam had">`;
            }

            // Mount-status badge + actieknoppen
            if (knownDiskMember) {
                if (p.mount_state === 'ro_archive' && p.mountpoint === expectedMountpoint) {
                    html += `<span class="badge-mounted">&#128274; RO &bull; ${escHtml(expectedMountpoint)}</span>`;
                } else if (p.mount_state === 'not_mounted') {
                    html += `<span class="badge-unmounted">Niet gekoppeld</span>`;
                    html += `<span class="meta">Verwacht: ${escHtml(expectedMountpoint)}</span>`;
                } else {
                    html += `<span class="badge-rw">&#9888; Verkeerd gekoppeld</span>`;
                    html += `<span class="meta">Huidig: ${escHtml(p.mountpoint || '-')} • Verwacht: ${escHtml(expectedMountpoint)}</span>`;
                }
            } else {
                switch(p.mount_state) {
                case 'not_mounted':
                    html += `<span class="badge-unmounted">Niet gemount</span>`;
                    if (p.known_label) {
                        html += `<button class="btn-secondary btn-small" onclick="prefillManualFromPartition('${safeDevice}','${safeLabel}','${safeMediaType}','bekende niet-gemounte disk',true)">&darr; Gebruik hieronder</button>`;
                        html += `<button class="btn-secondary btn-small" onclick="mountPartitionRO('${safeDevice}','${safeLabel}')">&#128275; Mount RO</button>`;
                    } else if (!technical && !unknownWholeDiskMember) {
                        html += `<button class="btn-small" onclick="ingestNewPartition(${diskIdx},${partIdx})" style="background:var(--accent2);color:#000">&#9654; Verwerken (nieuw)</button>`;
                    }
                    break;
                case 'rw':
                    html += `<span class="badge-rw">&#9888; RW: ${escHtml(p.mountpoint)}</span>`;
                    if (p.known_label) {
                        html += `<button class="btn-secondary btn-small" onclick="prefillManualFromPartition('${safeDevice}','${safeLabel}','${safeMediaType}','schrijfbaar gemounte disk',true)">&darr; Gebruik hieronder</button>`;
                    }
                    html += `<button class="btn-warn btn-small" onclick="remountRO('${safeDevice}','${escHtml(p.known_label || '').replace(/'/g,"\\'")}')">&#128275; Hermount RO</button>`;
                    html += `<button class="btn-warn btn-small" onclick="ejectPartition('${safeDevice}','${safeLabel}')">&#9167; Uitwerpen</button>`;
                    break;
                case 'ro_archive':
                    html += `<span class="badge-mounted">&#128274; RO &bull; ${escHtml(p.mountpoint)}</span>`;
                    if (p.known_label) {
                        html += `<button class="btn-secondary btn-small" onclick="prefillManualFromPartition('${safeDevice}','${safeLabel}','${safeMediaType}','aangesloten archief-disk',true)">&darr; Gebruik hieronder</button>`;
                        html += `<button class="btn-secondary btn-small" onclick="prefillAndRun('${safeDevice}','${safeLabel}','${safeMediaType}','scan')">&#9654; Scan</button>`;
                        html += `<button class="btn-secondary btn-small" onclick="prefillAndRun('${safeDevice}','${safeLabel}','${safeMediaType}','index')">&#128269; Index</button>`;
                        html += `<button class="btn-secondary btn-small" onclick="openDir('${safeLabel}','')">&#128193; Bladeren</button>`;
                        if (!p.sticker_confirmed) {
                            html += `<button class="btn-secondary btn-small" onclick="confirmStickerForLabel('${safeLabel}')">&#x1F3F7; Sticker OK</button>`;
                        }
                        html += `<button class="btn-warn btn-small" onclick="ejectPartition('${safeDevice}','${safeLabel}')">&#9167; Uitwerpen</button>`;
                    }
                    break;
                case 'ro_other':
                    html += `<span class="badge-rw">RO elders: ${escHtml(p.mountpoint)}</span>`;
                    if (p.known_label) {
                        html += `<button class="btn-secondary btn-small" onclick="prefillManualFromPartition('${safeDevice}','${safeLabel}','${safeMediaType}','RO op andere locatie',true)">&darr; Gebruik hieronder</button>`;
                        html += `<button class="btn-secondary btn-small" onclick="mountPartitionRO('${safeDevice}','${safeLabel}')">&#128275; Hermount naar archief-pad</button>`;
                    }
                    html += `<button class="btn-warn btn-small" onclick="ejectPartition('${safeDevice}','${safeLabel || p.mountpoint.split('/').pop()}')">&#9167; Uitwerpen</button>`;
                    break;
                }
            }
            html += `</div>`;
        });

        html += `</div>`;
    });

    // Knop voor nieuwe schijven via batch-ingest
    if (hasNewDisks) {
        document.getElementById('btn-batch').style.display = '';
    } else {
        document.getElementById('btn-batch').style.display = 'none';
    }

    document.getElementById('disk-list').innerHTML = html;

    const bestKnown = getBestKnownPartition(detectedDisks);
    if (bestKnown) {
        setManualSelection(bestKnown.device, bestKnown.label, bestKnown.mediaType, bestKnown.sourceText);
    } else if (detectedDisks.some(d => {
        const state = getKnownDiskState(d);
        return state && state.ingestable.length > 1;
    })) {
        setManualSelection('', '', 'usb_hdd', 'Gebruik bij meerpartitie-schijven de schijfkaart hierboven; dit noodpaneel is alleen voor losse partities');
    }

    // #209: pre-selecteer check-label dropdown met de aangesloten archief-disk
    // Voorkeur: gemount (ro_archive/rw) → daarna niet-gemount maar bekend → anders huidige waarde
    const checkSel = document.getElementById('check-label');
    if (checkSel) {
        const allKnown = detectedDisks.flatMap(d => d.partitions).filter(p => p.known_label);
        const mounted  = allKnown.find(p => p.mount_state === 'ro_archive' || p.mount_state === 'rw');
        const best     = mounted || allKnown[0];
        if (best) checkSel.value = best.known_label;
    }
}

async function mountPartitionRO(device, label) {
    const disk = detectedDisks.find(d => (d.partitions || []).some(p => p.known_label === label));
    const diskState = disk ? getKnownDiskState(disk) : null;
    const url = diskState && diskState.ingestable.length > 1 ? '/api/prepare-label' : '/api/mount';
    showProgress(url === '/api/prepare-label'
        ? `Koppeling van ${label} herstellen...`
        : `Mounten ${device} als ${label}...`);
    const resp = await fetch(url, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(url === '/api/prepare-label' ? {label} : {device, label})
    });
    const data = await resp.json();
    const type = data.success ? 'success' : 'error';
    setProgress(100, data.message, type);
    if (data.success) { loadConnectedDisks(); loadStats(); }
}

async function remountRO(device, label) {
    if (!label) { label = prompt('Archief-label voor deze schijf (bijv. ARCHIVE-DISK-003):'); }
    if (!label) return;
    const disk = detectedDisks.find(d => (d.partitions || []).some(p => p.known_label === label));
    const diskState = disk ? getKnownDiskState(disk) : null;
    const url = diskState && diskState.ingestable.length > 1 ? '/api/prepare-label' : '/api/mount';
    showProgress(url === '/api/prepare-label'
        ? `Koppeling van ${label} herstellen...`
        : `Hermounten ${device} als RO voor ${label}...`);
    const resp = await fetch(url, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(url === '/api/prepare-label' ? {label} : {device, label})
    });
    const data = await resp.json();
    const type = data.success ? 'success' : 'error';
    setProgress(100, data.message, type);
    if (data.success) { loadConnectedDisks(); }
}

async function ejectPartition(device, label) {
    if (!confirm(`Schijf ${label || device} uitwerpen?`)) return;
    showProgress(`Uitwerpen ${label || device}...`);
    try {
        const resp = await fetch('/api/eject', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({label, device})
        });
        const data = await resp.json();
        const type = data.success ? 'success' : 'error';
        setProgress(100, data.message, type);
        if (data.success) { loadConnectedDisks(); loadStats(); }
    } catch(e) {
        setProgress(0, `Fout: ${e.message}`, 'error');
    }
}

async function confirmStickerForLabel(label) {
    await fetch('/api/media/confirm-sticker', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({label})
    });
    loadConnectedDisks();
    loadStats();
    loadMedia();
}

async function ingestNewPartition(diskIdx, partIdx) {
    const disk = detectedDisks[diskIdx];
    const part = disk.partitions[partIdx];
    // Zoek het label-invoerveld voor deze partitie
    const inp = document.querySelector(`.disk-label-input[data-diskidx="${diskIdx}"][data-partidx="${partIdx}"]`);
    const label = inp ? inp.value.trim() : '';
    if (!label) { alert('Vul een archief-label in voor deze schijf'); return; }
    if (!confirm(`Nieuwe schijf verwerken als ${label}?\n\nDit monteert de schijf RO, scant alle bestanden en indexeert ze.`)) return;
    showProgress(`Verwerken ${part.device} als ${label}...`);
    const resp = await fetch('/api/ingest/batch', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({disks: [{label, media_type: disk.media_type || 'usb_hdd', model: disk.model || '', partitions: [part]}]})
    });
    const data = await resp.json();
    if (data.task_id) { startPolling(data.task_id); }
    else { setProgress(0, data.message || 'Fout', 'error'); }
}

async function ingestWholeDisk(diskIdx) {
    const disk = detectedDisks[diskIdx];
    const inp = document.querySelector(`.disk-label-input[data-diskidx="${diskIdx}"]:not([data-partidx])`);
    const label = inp ? inp.value.trim() : '';
    const partitions = getUnknownIngestablePartitions(disk);
    if (!label) { alert('Vul een archief-label in voor deze fysieke schijf'); return; }
    if (partitions.length === 0) {
        alert('Geen scanbare partities gevonden voor deze fysieke schijf');
        return;
    }
    if (!confirm(`Fysieke schijf ${disk.parent_device} verwerken als ${label}?\n\nDit gebruikt ${partitions.length} scanbare partitie(s), slaat technische partities over, mount read-only, scant en indexeert.`)) return;
    showProgress(`Verwerken ${disk.parent_device} als ${label}...`);
    const resp = await fetch('/api/ingest/batch', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({disks: [{label, media_type: disk.media_type || 'usb_hdd', model: disk.model || '', partitions}]})
    });
    const data = await resp.json();
    if (data.task_id) { startPolling(data.task_id); }
    else { setProgress(0, data.message || 'Fout', 'error'); }
}

async function startBatchIngest() {
    // Verwerk alle nieuwe schijven (met label-invoerveld) in één batch
    const inputs = document.querySelectorAll('.disk-label-input');
    if (inputs.length === 0) { alert('Geen nieuwe schijven geselecteerd'); return; }
    const disks = [];
    inputs.forEach(inp => {
        const label = inp.value.trim();
        const diskIdx = parseInt(inp.dataset.diskidx);
        const partIdxRaw = inp.dataset.partidx;
        if (!label || isNaN(diskIdx)) return;
        const disk = detectedDisks[diskIdx];
        let parts = [];
        if (partIdxRaw === undefined || partIdxRaw === '') {
            parts = getUnknownIngestablePartitions(disk);
        } else {
            const partIdx = parseInt(partIdxRaw);
            const part = disk?.partitions?.[partIdx];
            if (part && isIngestablePartition(part)) parts = [part];
        }
        if (parts.length === 0) return;
        // Groepeer partities per fysieke schijf en label
        const key = `${diskIdx}::${label}`;
        let existing = disks.find(d => d._key === key);
        if (existing) { existing.partitions.push(...parts); }
        else { disks.push({_key: key, label, media_type: disk.media_type || 'usb_hdd', model: disk.model || '', partitions: [...parts]}); }
    });
    if (disks.length === 0) { alert('Vul labelnamen in voor de nieuwe schijven'); return; }
    const labelList = disks.map(d => d.label).join(', ');
    if (!confirm(`Nieuwe schijven verwerken: ${labelList}?\n\nDit monteert ze RO, scant en indexeert alle bestanden.`)) return;
    showProgress(`Batch ingest: ${disks.length} schijven...`);
    const resp = await fetch('/api/ingest/batch', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({disks: disks.map(({_key, ...rest}) => rest)})
    });
    const data = await resp.json();
    if (data.task_id) { startPolling(data.task_id); }
    else { setProgress(0, data.message || 'Fout', 'error'); }
}

async function mountDisk() {
    const device = document.getElementById('new-device').value;
    const label = document.getElementById('new-label').value;
    if (!device || !label) { alert('Vul device en label in'); return; }
    showProgress('Mounten...');
    const resp = await fetch('/api/mount', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({device, label})
    });
    const data = await resp.json();
    if (data.success) { setProgress(100, data.message, 'success'); loadMountedDisks(); }
    else { setProgress(0, data.message, 'error'); }
}

async function startScan() {
    const label = document.getElementById('new-label').value;
    if (!label) { alert('Vul een label in'); return; }
    showProgress('Scan starten...');
    const resp = await fetch('/api/scan/start', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({label})
    });
    const data = await resp.json();
    if (data.task_id) {
        setProgress(5, data.message || 'Scan gestart', null);
        startPolling(data.task_id);
    }
    else { setProgress(0, data.message || 'Fout bij starten scan', 'error'); }
}

async function startIndex() {
    const label = document.getElementById('new-label').value;
    if (!label) { alert('Vul een label in'); return; }
    showProgress('Indexering starten...');
    const resp = await fetch('/api/index/start', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({label})
    });
    const data = await resp.json();
    if (data.task_id) {
        setProgress(5, data.message || 'Indexering gestart', null);
        startPolling(data.task_id);
    }
    else { setProgress(0, data.message || 'Fout', 'error'); }
}

async function runFullIngest() {
    const device = document.getElementById('new-device').value;
    const label = document.getElementById('new-label').value;
    const mtype = document.getElementById('new-type').value;
    if (!device || !label) { alert('Vul device en label in'); return; }
    showProgress('Volledige ingest starten...');
    // Enkele partitie als disk met 1 partitie
    const resp = await fetch('/api/ingest/batch', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({disks: [{
            label, media_type: mtype,
            partitions: [{device, label: device.split('/').pop(), fstype: 'auto'}]
        }]})
    });
    const data = await resp.json();
    if (data.task_id) { startPolling(data.task_id); }
    else { setProgress(0, data.message || 'Fout', 'error'); }
}

function showProgress(msg, taskLabel) {
    const panel = document.getElementById('progress-panel');
    panel.style.display = '';
    document.getElementById('progress-bar').style.width = '0%';
    document.getElementById('progress-text').textContent = '0%';
    document.getElementById('progress-msg').textContent = msg;
    document.getElementById('progress-msg').className = 'status-msg';
    document.getElementById('progress-details').textContent = '';
    document.getElementById('tussenverslagen').innerHTML = '';
    // Toon taak-label in titel indien meegegeven
    const taskEl = document.getElementById('progress-task');
    if (taskEl) taskEl.textContent = taskLabel ? '— ' + taskLabel : '';
    panel.scrollIntoView({behavior: 'smooth'});
}

function setProgress(pct, msg, type, details) {
    document.getElementById('progress-bar').style.width = Math.min(pct, 100) + '%';
    // Toon bestandspercentage met 1 decimaal tijdens scan
    const d = details || {};
    if (d.fase === 'scannen' && d.total_files > 0) {
        const done = (d.files_ok || 0) + (d.files_error || 0) + (d.files_skipped || 0);
        const filePct = (done / d.total_files * 100).toFixed(1);
        let txt = filePct + '% (' + done.toLocaleString('nl-NL') + '/' + d.total_files.toLocaleString('nl-NL') + ')';
        if (d.files_skipped > 0) txt += ' ✔ ' + d.files_skipped.toLocaleString('nl-NL') + ' hervatting';
        document.getElementById('progress-text').textContent = txt;
    } else {
        document.getElementById('progress-text').textContent = Math.round(pct) + '%';
    }
    document.getElementById('progress-msg').textContent = msg;
    document.getElementById('progress-msg').className = 'status-msg' + (type ? ' ' + type : '');
    // Balk-kleur aanpassen bij interrupted
    const bar = document.getElementById('progress-bar');
    if (type === 'interrupted') {
        bar.style.background = 'linear-gradient(90deg, var(--warn), #ffb74d)';
    } else {
        bar.style.background = 'linear-gradient(90deg, var(--accent), var(--accent2))';
    }
    // Details-lijn altijd bijwerken (ook als aangeroepen buiten startPolling)
    let parts = [];
    if (d.fase) parts.push(`Fase: ${d.fase}`);
    if (d.label) parts.push(`Medium: ${d.label}`);
    if (d.disk_nr) parts.push(`Schijf ${d.disk_nr}/${d.disk_total}`);
    if (d.files_ok !== undefined && d.fase !== 'klaar') parts.push(`${d.files_ok.toLocaleString('nl-NL')} ok`);
    if (d.files_error !== undefined && d.files_error > 0) parts.push(`${d.files_error} fouten`);
    if (d.files_skipped) parts.push(`${d.files_skipped.toLocaleString('nl-NL')} overgeslagen (resume)`);
    if (d.total_files) parts.push(`van ${d.total_files.toLocaleString('nl-NL')}`);
    if (d.walked) parts.push(`${d.walked.toLocaleString('nl-NL')} gelopen`);
    if (d.db_files) parts.push(`${d.db_files.toLocaleString('nl-NL')} in DB`);
    if (d.current_dir) parts.push(d.current_dir.length > 60 ? '...' + d.current_dir.slice(-57) : d.current_dir);
    if (d.current_file) parts.push(d.current_file);
    document.getElementById('progress-details').textContent = parts.join(' | ');
    if (type === 'success' || type === 'error' || type === 'interrupted') {
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
        if (type === 'success') { loadStats(); loadMountedDisks(); loadConnectedDisks(); }
    }
}

function startPolling(taskId) {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(async () => {
        try {
            const resp = await fetch(`/api/progress/${taskId}`);
            const data = await resp.json();
            if (!data) return;
            const d = data.details || {};

            // Bepaal status-type voor weergave
            let type = null;
            if (data.status === 'completed') type = 'success';
            else if (data.status === 'failed') type = 'error';
            else if (data.status === 'interrupted') type = 'interrupted';

            // Stale-detectie: als status 'running' maar laatste update > 60s geleden
            if (data.status === 'running' && data.updated) {
                const lastUpdate = new Date(data.updated);
                const now = new Date();
                const secsSinceUpdate = (now - lastUpdate) / 1000;
                if (secsSinceUpdate > 120) {
                    type = 'interrupted';
                    data.message = '⚠ Geen update sinds ' + lastUpdate.toLocaleTimeString('nl-NL') + ' — scan waarschijnlijk gestopt';
                }
            }

            setProgress(data.percent, data.message, type, d);

            // Tussenverslagen
            if (d.results && d.results.length > 0) {
                let tvHtml = '<h3>Tussenverslagen</h3>';
                d.results.forEach(r => {
                    const badge = r.status === 'ok' ? 'badge-ok' : r.status === 'fout' ? 'badge-err' : 'badge-ok';
                    const logBtn = r.log_file ? ` <button class="btn-secondary btn-small" onclick="viewLogFile('${r.log_file}')">Log bekijken</button>` : '';
                    tvHtml += `<div class="tussenverslag">
                        <span class="tv-label">${escHtml(r.label)}</span>
                        <span class="${badge}">${r.status}</span>
                        <span class="meta">${escHtml(r.message)}</span>${logBtn}
                    </div>`;
                });
                document.getElementById('tussenverslagen').innerHTML = tvHtml;
            }
        } catch(e) {}
    }, 1500);
}

// Uitwerpen
async function loadMountedDisks() {
    try {
        const resp = await fetch('/api/mounted');
        const data = await resp.json();
        const sel = document.getElementById('eject-label');
        sel.innerHTML = '';
        (data.mounted || []).forEach(m => {
            const parts = m.partition_count ? ` (${m.partition_count} partities)` : '';
            sel.innerHTML += `<option value="${m.label}">${m.label}${parts}</option>`;
        });
    } catch(e) {}
}

async function ejectDisk() {
    const label = document.getElementById('eject-label').value;
    if (!label) { alert('Selecteer een schijf'); return; }
    if (!confirm(`Weet je zeker dat je ${label} wilt uitwerpen?`)) return;
    const resp = await fetch('/api/eject', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({label})
    });
    const data = await resp.json();
    const cls = data.success ? 'success' : 'error';
    document.getElementById('eject-msg').innerHTML = `<div class="status-msg ${cls}">${escHtml(data.message)}</div>`;
    if (data.success) loadMountedDisks();
}

// Sticker
async function updateStickerInfo() {
    const label = document.getElementById('sticker-select').value;
    if (!label) { document.getElementById('sticker-info').innerHTML = ''; return; }
    const resp = await fetch(`/api/media/info?label=${encodeURIComponent(label)}`);
    const m = await resp.json();
    const confirmed = m.sticker_confirmed ? `Ja (${(m.sticker_confirmed_at||'').substring(0,16)})` : 'Nee';
    document.getElementById('sticker-info').innerHTML = `
        <div style="display:flex; gap:15px; flex-wrap:wrap; margin:5px 0">
            <div><span class="meta">Huidig label:</span> <input type="text" value="${escHtml(label)}" readonly style="width:200px"></div>
            <div><span class="meta">Type:</span> ${escHtml(m.media_type||'-')}</div>
            <div><span class="meta">Volume:</span> ${escHtml(m.volume_label||'-')}</div>
            <div><span class="meta">Sticker bevestigd:</span> ${confirmed}</div>
            <div><span class="meta">Bestanden:</span> ${m.file_count||0}</div>
        </div>
        <div class="meta" style="margin-top:8px; padding:12px; background:var(--panel-2); border:2px dashed var(--accent); border-radius:8px; text-align:center">
            <div style="font-size:1.3em; color:var(--accent); font-weight:bold">${escHtml(label)}</div>
            <div style="color:var(--muted); margin-top:3px">Plak dit label op de fysieke drager</div>
        </div>`;
}

async function confirmSticker() {
    const label = document.getElementById('sticker-select').value;
    if (!label) { alert('Selecteer een medium'); return; }
    await fetch('/api/media/confirm-sticker', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({label})
    });
    updateStickerInfo();
    loadStats();
}

// #214: Zorg dat label gemount is als RO — mount automatisch als nodig.
// Geeft true terug als gemount (of al gemount), false als niet mogelijk/geweigerd.
async function _ensureMountedForCheck(label) {
    const allParts = detectedDisks.flatMap(d => d.partitions);
    const part = allParts.find(p => p.known_label === label && isIngestablePartition(p))
        || allParts.find(p => p.known_label === label);
    if (!part) {
        // Schijf niet fysiek aangesloten — laat backend de fout afhandelen
        return true;
    }
    const disk = detectedDisks.find(d => (d.partitions || []).some(p => p.known_label === label));
    const diskState = disk ? getKnownDiskState(disk) : null;
    if (diskState && !diskState.ready) {
        if (!confirm(`Schijf ${label} is nog niet correct gekoppeld.\n\nAutomatisch correct koppelen voor de check?`)) return false;
        showProgress(`Koppeling van ${label} herstellen...`);
        const resp = await fetch('/api/prepare-label', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({label})
        });
        const data = await resp.json();
        setProgress(100, data.message, data.success ? 'success' : 'error');
        if (data.success) {
            await loadConnectedDisks();
            const panel = document.getElementById('geavanceerd-panel');
            if (panel) panel.open = true;
            return true;
        }
        return false;
    }
    if (part.mount_state === 'ro_archive') return true; // Al correct gemount
    const action = part.mount_state === 'not_mounted' ? 'RO mounten' : 'hermounten naar archief-pad';
    if (!confirm(`Schijf ${label} is niet als archief gemount.\n\nAutomatisch ${action} voor de check?`)) return false;
    showProgress(`Mounten ${part.device} als ${label}...`);
    const resp = await fetch('/api/mount', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({device: part.device, label})
    });
    const data = await resp.json();
    setProgress(100, data.message, data.success ? 'success' : 'error');
    if (data.success) {
        await loadConnectedDisks();
        // #217: open geavanceerd-panel zodat check-knoppen direct zichtbaar zijn
        const panel = document.getElementById('geavanceerd-panel');
        if (panel) panel.open = true;
        return true;
    }
    return false;
}

// Check aangesloten archive-disk
function _renderCheckResult(data, resultDiv) {
    const vColor = data.verdict === 'ok' ? 'var(--accent)' : data.verdict === 'onvolledig' ? 'var(--err)' : data.verdict === 'timeout' ? 'var(--muted)' : 'var(--warn)';
    const vIcon = data.verdict === 'ok' ? '✅' : data.verdict === 'onvolledig' ? '❌' : data.verdict === 'timeout' ? '⏱' : '⚠️';
    let html = `<div style="margin:12px 0; padding:12px; border-radius:8px; border:2px solid ${vColor}; background:rgba(0,0,0,0.3)">`;
    html += `<div style="font-size:1.2em; font-weight:bold; color:${vColor}">${vIcon} ${escHtml(data.verdict_msg)}</div>`;
    if (data.source_roots && data.source_roots.length > 0) {
        html += `<div class="meta" style="margin:6px 0; font-size:0.85em">Pad: ${data.source_roots.map(r => escHtml(r)).join(', ')}</div>`;
    }
    html += `<table style="margin-top:10px; width:100%">`;
    const df = (data.disk_files !== undefined ? data.disk_files : data.disk_count) || 0;
    const dbf = (data.db_files !== undefined ? data.db_files : data.db_count) || 0;
    html += `<tr><td class="meta">Op schijf:</td><td><strong>${df.toLocaleString('nl-NL')}</strong></td>`;
    html += `<td class="meta">In DB:</td><td><strong>${dbf.toLocaleString('nl-NL')}</strong></td></tr>`;
    if (data.matched !== undefined) {
        html += `<tr><td class="meta">Geverifieerd:</td><td style="color:var(--accent)"><strong>${data.matched.toLocaleString('nl-NL')}</strong></td>`;
        html += `<td class="meta">Grootteverschillen:</td><td style="color:${data.size_mismatch > 0 ? 'var(--err)' : 'var(--accent)'}">${data.size_mismatch}</td></tr>`;
        html += `<tr><td class="meta">Alleen schijf:</td><td style="color:${data.alleen_schijf > 0 ? 'var(--warn)' : 'var(--accent)'}">${data.alleen_schijf.toLocaleString('nl-NL')}</td>`;
        html += `<td class="meta">Alleen DB:</td><td style="color:${data.alleen_db > 0 ? 'var(--warn)' : 'var(--accent)'}">${data.alleen_db.toLocaleString('nl-NL')}</td></tr>`;
    }
    if (data.elapsed) {
        html += `<tr><td class="meta">Duur:</td><td colspan="3">${data.elapsed}s</td></tr>`;
    }
    html += `</table>`;
    ['voorbeelden_alleen_schijf','voorbeelden_alleen_db'].forEach(key => {
        const items = data[key];
        if (items && items.length > 0) {
            const lbl = key.includes('schijf') ? 'Alleen op schijf (niet in DB)' : 'Alleen in DB (niet meer op schijf)';
            html += `<details style="margin-top:8px"><summary class="meta" style="cursor:pointer">${lbl} (max 10 voorbeelden)</summary><ul style="font-size:0.85em; color:var(--text); margin-top:6px">`;
            items.forEach(p => { html += `<li style="margin:2px 0">${escHtml(p)}</li>`; });
            html += `</ul></details>`;
        }
    });
    // Vervolgacties (#203) + log-knop + SQL-knop (#215)
    html += `<div style="margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; align-items:center">`;
    if (data.log_file) {
        html += `<button class="btn-secondary btn-small" onclick="viewLogFile('${escHtml(data.log_file).replace(/'/g,"\\'")}')">&#128196; Log bekijken</button>`;
    }
    // #215: SQL-knop als er bestanden alleen in DB zijn
    const alleenDb = data.alleen_db || 0;
    if (alleenDb > 0 && data.label) {
        const lbl = escHtml(data.label).replace(/'/g, "\\'");
        html += `<button class="btn-secondary btn-small" onclick="_openSqlForAlleenDb('${lbl}',${JSON.stringify(data.alleen_db_paden||[]).replace(/'/g,"\\'")})">`
            + `&#128202; ${t('sqlInTab')} (${alleenDb.toLocaleString('nl-NL')} bestanden)</button>`;
    }
    let extraHtml = '';
    if (data.verdict === 'ok') {
        html += `<span style="color:var(--accent); font-size:0.88em">&#10003; Archief is volledig — geen actie nodig.</span>`;
    } else if (data.verdict === 'waarschuwing') {
        html += `<button class="btn-secondary btn-small" onclick="document.getElementById('check-result').innerHTML=''">Sluiten</button>`;
        html += `<span class="meta" style="font-size:0.82em; align-self:center">
            Bestanden alleen in DB? Schijf was mogelijk niet gemount of paden kloppen niet. Controleer en herhaal check.<br>
            Bestanden alleen op schijf? Scan herhalen via Beheer → Verwerken.
        </span>`;
    } else if (data.verdict === 'onvolledig') {
        html += `<button class="btn-secondary btn-small" onclick="switchView('manage')">&#9654; Naar Beheer → Verwerken</button>`;
        html += `<button class="btn-warn btn-small" onclick="document.getElementById('check-result').innerHTML=''">Sluiten</button>`;
        // #219: duidelijke uitleg als apart blok onder de flex-rij
        extraHtml = `<div style="margin-top:10px; padding:10px 12px; background:rgba(255,152,0,0.07); border-radius:6px; border-left:3px solid var(--warn); font-size:0.85em; color:var(--text); line-height:1.7">`;
        extraHtml += `<strong style="color:var(--warn)">Wat betekent dit?</strong><br>`;
        extraHtml += `Er zijn <strong style="color:var(--warn)">${(data.alleen_schijf||0).toLocaleString('nl-NL')} bestanden op de schijf die nog niet in de database staan</strong> — ze zijn veilig aanwezig, maar nog niet gescand en daardoor niet doorzoekbaar.<br>`;
        extraHtml += `<br><strong>Wat kun je doen?</strong><br>`;
        extraHtml += `&#9654; <strong>Scannen (aanbevolen):</strong> klik "Naar Beheer → Verwerken" en start een nieuwe scan van deze schijf. De nieuwe bestanden worden toegevoegd zonder bestaande te overschrijven.<br>`;
        extraHtml += `&#9654; <strong>Niets doen:</strong> de al gescande ${(data.matched||0).toLocaleString('nl-NL')} bestanden blijven doorzoekbaar. Alleen de ontbrekende bestanden zijn niet vindbaar.<br>`;
        extraHtml += `<br><span style="color:var(--muted)">Tip: bestanden in <code>$RECYCLE.BIN</code> of systeemmappen (desktop.ini, Thumbs.db) worden bij een scan automatisch overgeslagen — die zijn niet de reden voor dit verschil.</span>`;
        extraHtml += `</div>`;
    }
    html += `</div>`;      // sluit flex-rij knoppen
    html += extraHtml;    // #219: uitlegblok indien onvolledig
    html += `</div>`;     // sluit hoofd-container
    resultDiv.innerHTML = html;
}

// #215: Navigeer naar SQL-tab met voorgevulde query voor bestanden-alleen-in-DB.
function _openSqlForAlleenDb(label, paden) {
    let query;
    if (paden && paden.length > 0) {
        // Bouw een IN-lijst op basis van de eerste 100 paden
        const first100 = paden.slice(0, 100);
        const inList = first100.map(p => "'" + p.replace(/'/g, "''") + "'").join(',\n  ');
        query = "-- Bestanden alleen in DB (niet meer op schijf) voor " + label + "\n"
            + "-- " + paden.length + " bestanden totaal; hieronder de eerste " + first100.length + "\n"
            + "SELECT file_id, relative_path, human_size, availability_status\n"
            + "FROM files\n"
            + "WHERE archive_label = '" + label + "'\n"
            + "  AND relative_path IN (\n  " + inList + "\n)\n"
            + "ORDER BY relative_path\nLIMIT 200";
    } else {
        query = "-- Alle bestanden voor " + label + " (filter handmatig aanpassen)\n"
            + "SELECT file_id, relative_path, human_size, availability_status\n"
            + "FROM files\n"
            + "WHERE archive_label = '" + label + "'\n"
            + "ORDER BY relative_path\nLIMIT 200";
    }
    switchView('sql');
    const sqlInput = document.getElementById('sql-input');
    if (sqlInput) { sqlInput.value = query; sqlInput.focus(); }
}

// #211: Pad-migratie — verwijder foutief prefix uit DB-paden voor een medium.
async function runPathMigration() {
    const label = document.getElementById('migrate-label').value;
    const prefix = document.getElementById('migrate-prefix').value.trim();
    const resultDiv = document.getElementById('migrate-result');
    if (!label) { alert('Selecteer een medium'); return; }
    if (!prefix || !prefix.endsWith('/')) { alert('Prefix moet eindigen op "/" (bijv. Elements/)'); return; }
    // Tel eerst hoeveel rijen geraakt worden
    const countResp = await fetch('/api/sql', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: `SELECT COUNT(*) as c FROM files WHERE archive_label='${label.replace(/'/g,"''")}' AND relative_path LIKE '${prefix.replace(/'/g,"''")}%'`})
    });
    const countData = await countResp.json();
    const count = countData.rows ? countData.rows[0][0] : 0;
    if (count === 0) { resultDiv.innerHTML = '<div class="status-msg">Geen rijen gevonden met dit prefix — migratie niet nodig.</div>'; return; }
    if (!confirm(`${count.toLocaleString('nl-NL')} bestanden in de database van ${label} hebben het prefix "${prefix}".\n\nWil je dit prefix verwijderen uit alle paden?\n\nDeze actie past de database permanent aan (kan niet ongedaan worden gemaakt via de app).`)) return;
    resultDiv.innerHTML = '<div class="status-msg">Migratie uitvoeren...</div>';
    try {
        const resp = await fetch('/api/admin/migrate-paths', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({label, strip_prefix: prefix, confirm: true})
        });
        const data = await resp.json();
        if (data.success) {
            resultDiv.innerHTML = `<div class="status-msg success">&#10003; ${escHtml(data.message)}</div>`;
        } else {
            resultDiv.innerHTML = `<div class="status-msg error">Fout: ${escHtml(data.error || data.message)}</div>`;
        }
    } catch(e) {
        resultDiv.innerHTML = `<div class="status-msg error">Fout: ${escHtml(e.message)}</div>`;
    }
}

async function checkDiskQuick() {
    const label = document.getElementById('check-label').value;
    if (!label) { alert('Selecteer een medium'); return; }
    // #214: auto-mount als schijf niet gemount is
    if (!await _ensureMountedForCheck(label)) return;
    const resultDiv = document.getElementById('check-result');
    resultDiv.innerHTML = '<div class="status-msg">Snelle check: bestanden tellen...</div>';
    try {
        const resp = await fetch('/api/check-disk-quick', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({label})
        });
        const data = await resp.json();
        if (!data.success) {
            resultDiv.innerHTML = `<div class="status-msg error" style="white-space:pre-line">${escHtml(data.message)}</div>`;
            return;
        }
        _renderCheckResult(data, resultDiv);
    } catch(e) {
        resultDiv.innerHTML = `<div class="status-msg error">Fout: ${escHtml(e.message)}</div>`;
    }
}

async function checkDiskFull() {
    const label = document.getElementById('check-label').value;
    if (!label) { alert('Selecteer een medium'); return; }
    // #214: auto-mount als schijf niet gemount is
    if (!await _ensureMountedForCheck(label)) return;
    if (!confirm(`Volledige check van ${label} starten?\n\nDit vergelijkt elk bestand (pad + grootte) met de database. Bij grote schijven (700k+ bestanden) duurt dit 10-20 minuten. Voortgang is zichtbaar in het Voortgang-paneel.`)) return;
    const resultDiv = document.getElementById('check-result');
    resultDiv.innerHTML = '<div class="status-msg">Volledige check gestart als achtergrondtaak — zie Voortgang-paneel...</div>';
    try {
        const resp = await fetch('/api/check-disk-start', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({label})
        });
        const data = await resp.json();
        if (data.success && data.task_id) {
            showProgress(`Volledige check ${label} gestart...`, `Archief-check: ${label}`);
            // Polling — bij completion toon resultaat in check-result
            const checkPoll = setInterval(async () => {
                const pr = await fetch(`/api/progress/${data.task_id}`).then(r => r.json());
                if (!pr) return;
                setProgress(pr.percent || 0, pr.message, pr.status === 'completed' ? 'success' : pr.status === 'failed' ? 'error' : null, pr.details);
                if (pr.status === 'completed' && pr.details && pr.details.verdict) {
                    clearInterval(checkPoll);
                    _renderCheckResult({
                        success: true,
                        label: label,
                        verdict: pr.details.verdict,
                        verdict_msg: pr.details.verdict_msg,
                        source_roots: pr.details.source_roots,
                        disk_files: pr.details.disk_files,
                        db_files: pr.details.db_files,
                        matched: pr.details.matched,
                        alleen_schijf: pr.details.alleen_schijf,
                        alleen_db: pr.details.alleen_db,
                        size_mismatch: pr.details.size_mismatch,
                        voorbeelden_alleen_schijf: pr.details.voorbeelden_alleen_schijf,
                        voorbeelden_alleen_db: pr.details.voorbeelden_alleen_db,
                        log_file: pr.details.log_file,
                        alleen_db_paden: pr.details.alleen_db_paden || [],
                    }, resultDiv);
                } else if (pr.status === 'failed') {
                    clearInterval(checkPoll);
                    resultDiv.innerHTML = `<div class="status-msg error">${escHtml(pr.message)}</div>`;
                }
            }, 2000);
        } else {
            resultDiv.innerHTML = `<div class="status-msg error">${escHtml(data.message)}</div>`;
        }
    } catch(e) {
        resultDiv.innerHTML = `<div class="status-msg error">Fout: ${escHtml(e.message)}</div>`;
    }
}

// Media tab
async function loadMedia() {
    const resp = await fetch('/api/media');
    const data = await resp.json();
    if (!data.media || data.media.length === 0) {
        document.getElementById('media-table').innerHTML = `<div class="empty">${t('noMedia')}</div>`;
        return;
    }
    let html = `<table class="disk-table"><tr><th>${t('colLabel')}</th><th>${t('colType')}</th><th>${t('colVolume')}</th><th>${t('colModel')}</th><th>${t('colSticker')}</th><th>${t('colFirstSeen')}</th><th>${t('statFiles')}</th><th>${t('statScans')}</th></tr>`;
    data.media.forEach(m => {
        let sticker = m.sticker_confirmed
            ? `<span class="badge-online">${t('yes')}</span>`
            : `<div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap"><span class="badge-offline">${t('no')}</span><button class="btn-secondary btn-small" onclick="confirmStickerForLabel('${escHtml(m.archive_label).replace(/'/g,"\\'")}')">&#x1F3F7; ${t('markSticker')}</button></div>`;
        html += `<tr>
            <td><strong>${escHtml(m.archive_label)}</strong></td>
            <td>${escHtml(m.media_type)}</td><td>${escHtml(m.volume_label||'-')}</td>
            <td>${escHtml(m.device_model||'-')}</td><td>${sticker}</td>
            <td>${(m.first_seen||'').substring(0,10)}</td>
            <td>${m.file_count || 0}</td>
            <td>${m.scan_count || 0}</td></tr>`;
    });
    html += '</table>';
    document.getElementById('media-table').innerHTML = html;
}

// SQL tab
async function runSQL() {
    const sql = document.getElementById('sql-input').value.trim();
    if (!sql) return;
    const resp = await fetch('/api/sql', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({query: sql})
    });
    const data = await resp.json();
    if (data.error) {
        document.getElementById('sql-results').innerHTML = `<div class="status-msg error">${escHtml(data.error)}</div>`;
        return;
    }
    if (!data.columns || data.rows.length === 0) {
        document.getElementById('sql-results').innerHTML = `<div class="status-msg">${t('sqlNoResults')}</div>`;
        return;
    }
    let html = `<div class="count">${data.rows.length} ${t('sqlRows')}</div><table>`;
    html += '<tr>' + data.columns.map(c => `<th>${escHtml(c)}</th>`).join('') + '</tr>';
    data.rows.forEach(row => {
        html += '<tr>' + row.map(v => `<td>${v === null ? '<span style="color:var(--muted-dim)">NULL</span>' : escHtml(String(v))}</td>`).join('') + '</tr>';
    });
    html += '</table>';
    document.getElementById('sql-results').innerHTML = html;
}

// #97: Scan-overzicht met duidelijke status
async function loadScans() {
    try {
        const resp = await fetch('/api/scans');
        const data = await resp.json();
        if (!data.scans || data.scans.length === 0) {
            document.getElementById('scan-overview').innerHTML = `<div class="status-msg">${t('noScans')}</div>`;
            return;
        }
        let html = `<table class="disk-table"><tr><th>${t('colLabel')}</th><th>${t('colSource')}</th><th>${t('colStart')}</th><th>${t('colEnd')}</th><th>${t('colStatus')}</th><th>${t('statFiles')}</th><th>${t('colErrors')}</th></tr>`;
        data.scans.forEach(s => {
            const statusCls = s.status === 'completed' ? 'badge-ok' : s.status === 'running' ? 'badge-mounted' : 'badge-err';
            const statusTxt = s.status === 'completed' ? t('scanDone') : s.status === 'running' ? t('scanRunning') : s.status;
            const start = (s.start_time || '').substring(0, 16).replace('T', ' ');
            const end = s.end_time ? s.end_time.substring(0, 16).replace('T', ' ') : '-';
            const srcShort = s.source_display || (s.source_root || '').split('/').slice(-2).join('/');
            const srcDetail = s.source_detail || s.source_root || '';
            const partInfo = Array.isArray(s.source_parts) && s.source_parts.length > 1
                ? `<div class="meta" style="font-size:0.78em">${s.source_parts.length} partities: ${escHtml(s.source_parts.join(', '))}</div>`
                : '';
            html += '<tr>'
                + '<td><strong>' + escHtml(s.archive_label) + '</strong></td>'
                + '<td class="meta" title="' + escHtml(srcDetail) + '">' + escHtml(srcShort) + partInfo + '</td>'
                + '<td class="meta">' + start + '</td>'
                + '<td class="meta">' + end + '</td>'
                + '<td><span class="' + statusCls + '">' + statusTxt + '</span></td>'
                + '<td>' + ((s.display_files_ok ?? s.files_ok) || 0) + '</td>'
                + '<td>' + ((s.display_files_error ?? s.files_error) || 0) + '</td>'
                + '</tr>';
        });
        html += '</table>';
        document.getElementById('scan-overview').innerHTML = html;
    } catch(e) {
        document.getElementById('scan-overview').innerHTML = '<div class="status-msg error">Fout bij laden scan-overzicht</div>';
    }
}

// Logboek
async function loadLogs() {
    const resp = await fetch('/api/logs');
    const data = await resp.json();
    if (!data.logs || data.logs.length === 0) {
        document.getElementById('log-list').innerHTML = '<div class="status-msg">Geen logbestanden gevonden</div>';
        return;
    }
    let html = '<table class="disk-table"><tr><th>Type</th><th>Bestand</th><th>Grootte</th><th>Datum</th><th></th></tr>';
    data.logs.forEach(l => {
        let typeLabel = '', typeStyle = 'color:var(--muted)';
        if (l.name.startsWith('check_'))       { typeLabel = '&#128270; Controle'; typeStyle = 'color:var(--accent2)'; }
        else if (l.name.startsWith('scan_'))   { typeLabel = '&#128190; Scan';     typeStyle = 'color:var(--accent)'; }
        else if (l.name.startsWith('mount_'))  { typeLabel = '&#128275; Mount';    typeStyle = 'color:var(--muted)'; }
        else if (l.name.startsWith('index_'))  { typeLabel = '&#128218; Index';    typeStyle = 'color:var(--warn)'; }
        else                                   { typeLabel = '&#128196; Log';      typeStyle = 'color:var(--muted)'; }
        html += `<tr>
            <td style="white-space:nowrap; font-size:0.82em; ${typeStyle}">${typeLabel}</td>
            <td>${escHtml(l.name)}</td><td>${l.size}</td><td>${l.modified}</td>
            <td><button class="btn-secondary btn-small" onclick="viewLogFile('${escHtml(l.path).replace(/'/g,"\\'")}')">  ${t('viewLog')}</button></td>
        </tr>`;
    });
    html += '</table>';
    document.getElementById('log-list').innerHTML = html;
}

async function viewLogFile(path) {
    const resp = await fetch(`/api/logs/view?path=${encodeURIComponent(path)}`);
    const data = await resp.json();
    const viewer = document.getElementById('log-viewer');
    viewer.style.display = '';
    viewer.textContent = data.content || 'Leeg logbestand';
    viewer.scrollIntoView({behavior: 'smooth'});
    // Als we in beheer-tab zijn, switch naar logs
    if (currentView !== 'logs') {
        switchView('logs');
        setTimeout(() => {
            viewer.style.display = '';
            viewer.textContent = data.content || 'Leeg logbestand';
        }, 100);
    }
}

// #95: Automatisch actieve taak detecteren (ook op ander device)
async function checkActiveTask() {
    try {
        // Stop eventuele oude polling eerst
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
        const resp = await fetch('/api/progress/active');
        const data = await resp.json();
        if (data && data.task_id) {
            const d = data.details || {};
            if (data.status === 'running') {
                showProgress(data.message || 'Taak loopt...');
                setProgress(data.percent, data.message, null, d);
                startPolling(data.task_id);
            } else if (data.status === 'interrupted') {
                showProgress(data.message || 'Taak onderbroken');
                setProgress(data.percent, data.message, 'interrupted', d);
                // Geen polling starten — taak is gestopt
            }
        } else {
            // Geen actieve taak — verberg voortgangspanel
            document.getElementById('progress-panel').style.display = 'none';
        }
    } catch(e) {}
}

// Uitwerpen vanuit disk-detectie
async function ejectDetectedDisk(label) {
    // Alias voor achterwaartse compatibiliteit
    await ejectPartition(null, label);
}

// Service beheer
async function serviceAction(action) {
    if (action === 'restart') {
        if (!confirm('De webinterface wordt kort herstart. De pagina laadt automatisch opnieuw.')) return;
    }
    if (action === 'stop') {
        if (!confirm('De service wordt gestopt. Je verliest de verbinding met deze interface!')) return;
    }
    try {
        const resp = await fetch(`/api/service/${action}`, {
            method: action === 'status' ? 'GET' : 'POST'
        });
        const data = await resp.json();
        const statusEl = document.getElementById('service-status');
        const outputEl = document.getElementById('service-output');
        if (data.active !== undefined) {
            const cls = data.active ? 'service-active' : 'service-inactive';
            const txt = data.active ? '● ' + t('webserviceRunning') : '● ' + t('webserviceStopped');
            statusEl.innerHTML = `<span class="service-status ${cls}">${txt}</span>`;
        }
        if (data.output) {
            outputEl.style.display = '';
            outputEl.textContent = data.output;
        }
        if (action === 'restart') {
            statusEl.innerHTML = '<span class="service-status service-unknown">Herstarten... pagina herlaadt over 3 seconden</span>';
            setTimeout(() => location.reload(), 3000);
        }
    } catch(e) {
        if (action === 'restart') {
            document.getElementById('service-status').innerHTML =
                '<span class="service-status service-unknown">Herstarten... pagina herlaadt over 5 seconden</span>';
            setTimeout(() => location.reload(), 5000);
        } else {
            document.getElementById('service-status').innerHTML =
                '<span class="service-status service-inactive">Verbinding verloren</span>';
        }
    }
}

async function loadServiceStatus() {
    try {
        const resp = await fetch('/api/service/status');
        const data = await resp.json();
        const cls = data.active ? 'service-active' : 'service-inactive';
        const txt = data.active ? '● ' + t('webserviceRunning') : '● ' + t('webserviceStopped');
        document.getElementById('service-status').innerHTML = `<span class="service-status ${cls}">${txt}</span>`;
    } catch(e) {}
}

// Thema (licht/donker) — canon: licht standaard, door gebruiker om te schakelen
function applyThemeIcon() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    const b = document.getElementById('theme-toggle');
    if (b) b.innerHTML = dark ? '☀️' : '🌙';  // toon doeltoestand
    if (b) b.title = dark ? 'Naar licht thema' : 'Naar donker thema';
}
function toggleTheme() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    const nxt = dark ? 'light' : 'dark';
    try { localStorage.setItem('asw-theme', nxt); } catch(e) {}
    document.documentElement.setAttribute('data-theme', nxt);
    applyThemeIcon();
}

// Init
applyThemeIcon();
try { document.getElementById('srv-host').textContent = location.host; } catch(e) {}
document.getElementById('query').addEventListener('keypress', e => {
    if (e.key === 'Enter') doSearch(e);
});
document.getElementById('sql-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); runSQL(); }
});
loadStats();
initDonateAndLanguage();
</script>
</body>
</html>"""


# === API Routes ===

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/stats')
def api_stats():
    conn = sqlite3.connect(str(DB_PATH))
    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_bytes = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM files").fetchone()[0]
    total_media = conn.execute("SELECT COUNT(*) FROM physical_media").fetchone()[0]
    total_scans = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    media_labels = [r[0] for r in conn.execute(
        "SELECT archive_label FROM physical_media ORDER BY archive_label").fetchall()]
    conn.close()
    return jsonify({
        'total_files': total_files, 'total_gb': round(total_bytes / (1024**3), 1),
        'total_media': total_media, 'total_scans': total_scans, 'media_labels': media_labels,
    })


@app.route('/api/search/metadata')
def api_search_metadata():
    query = request.args.get('query', '').strip()
    group = request.args.get('group', '').strip()
    label = request.args.get('label', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    conditions, params = [], []

    if query:
        query_cond, query_params = _parse_search_query(query)
        if query_cond:
            conditions.append(query_cond)
            params.extend(query_params)

    if group:
        conditions.append("extension_group = ?")
        params.append(group)
    if label:
        conditions.append("archive_label = ?")
        params.append(label)
    if date_from:
        conditions.append("original_content_date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("original_content_date <= ?")
        params.append(date_to)

    if not conditions:
        return jsonify({'results': []})

    where = " AND ".join(conditions)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""SELECT filename, title, author, original_content_date, extension,
        extension_group, human_size, archive_label, availability_status, relative_path, source_root
        FROM files WHERE {where}
        ORDER BY original_content_date DESC NULLS LAST, filename LIMIT 200""", params).fetchall()
    conn.close()
    return jsonify({'results': [dict(r) for r in rows]})


@app.route('/api/search/content')
def api_search_content():
    query = request.args.get('query', '').strip()
    label = request.args.get('label', '').strip()
    if not query:
        return jsonify({'results': []})
    results = []
    index_dirs = [INDEX_BASE / label] if label else [d for d in INDEX_BASE.iterdir() if d.is_dir()]
    for index_dir in index_dirs:
        if not index_dir.exists():
            continue
        try:
            output = subprocess.run(
                ['recoll', '-c', str(index_dir), '-t', '-n', '50', '-q', query],
                capture_output=True, text=True, timeout=30)
            if output.returncode == 0:
                for line in output.stdout.strip().split('\n'):
                    if any(line.startswith(t) for t in ('text/', 'application/', 'inode/')):
                        parts = line.split('\t')
                        if len(parts) >= 3:
                            results.append({
                                'label': index_dir.name,
                                'path': parts[1].replace('[', '').replace(']', '').replace('file://', ''),
                                'filename': parts[2].replace('[', '').replace(']', ''),
                                'size': parts[3] if len(parts) > 3 else '?',
                            })
        except Exception:
            pass
    return jsonify({'results': results})


@app.route('/api/file-serve')
def api_file_serve():
    """Serveer een bestand van een gemounte archief-schijf.

    Query params: label, path (relative_path in DB)
    Zoekt source_root in DB om het juiste volledige pad te bepalen.
    Geeft JSON-fout terug als schijf niet gemount is (geen kale 404).
    """
    label = request.args.get('label', '').strip()
    rel_path = request.args.get('path', '').strip()
    if not label or not rel_path:
        return jsonify({'error': 'label en path zijn vereist', 'not_mounted': False}), 400

    full_path, _source_root = _lookup_archived_file_path(label, rel_path)
    if not full_path:
        return jsonify({
            'error': f'Bestand niet gevonden in catalogus voor {label}',
            'not_mounted': False
        }), 404

    if not full_path.exists() or not full_path.is_file():
        # Bestand staat in DB maar is niet toegankelijk — schijf waarschijnlijk niet gemount
        return jsonify({
            'error': f'Bestand niet toegankelijk — is schijf {label} aangesloten op de server?',
            'not_mounted': True,
            'label': label,
            'expected_path': str(full_path)
        }), 404

    # Stuur bestand — Flask bepaalt MIME type op extensie
    try:
        return send_file(str(full_path), as_attachment=False)
    except Exception as e:
        return jsonify({'error': f'Fout bij serveren: {e}', 'not_mounted': False}), 500


@app.route('/api/file-serve-path')
def api_file_serve_path():
    """Serveer een bestand via volledig pad (voor Recoll content-zoekresultaten).

    Veiligheidcheck: pad moet binnen een bekende source_root vallen.
    """
    full_path = request.args.get('fullpath', '').strip()
    if not full_path:
        abort(400)

    fp = Path(full_path)

    if not _path_within_archive_mount(fp):
        return jsonify({'error': 'Pad valt buiten het archief-mount-pad', 'not_mounted': False}), 403

    if not fp.exists() or not fp.is_file():
        return jsonify({
            'error': 'Bestand niet toegankelijk — is de schijf aangesloten op de server?',
            'not_mounted': True,
            'expected_path': str(fp)
        }), 404

    try:
        return send_file(str(fp), as_attachment=False)
    except Exception as e:
        return jsonify({'error': f'Fout bij serveren: {e}', 'not_mounted': False}), 500


@app.route('/api/file-open', methods=['POST'])
def api_file_open():
    """Open een gecatalogiseerd bestand met de standaard-app; val terug op de map."""
    data = request.get_json(silent=True) or {}
    label = (data.get('label') or '').strip()
    rel_path = (data.get('path') or '').strip()
    if not label or not rel_path:
        return jsonify({'success': False, 'error': 'label en path zijn vereist', 'not_mounted': False}), 400

    full_path, _source_root = _lookup_archived_file_path(label, rel_path)
    if not full_path:
        return jsonify({'success': False, 'error': f'Bestand niet gevonden in catalogus voor {label}', 'not_mounted': False}), 404
    if not full_path.exists():
        return jsonify({
            'success': False,
            'error': f'Bestand niet toegankelijk — is schijf {label} aangesloten op de server?',
            'not_mounted': True,
            'label': label,
            'expected_path': str(full_path),
        }), 404

    result = _open_file_or_fallback_dir(full_path)
    status = 200 if result.get('success') else 500
    return jsonify(result), status


@app.route('/api/file-open-path', methods=['POST'])
def api_file_open_path():
    """Open een bestand op volledig pad met standaard-app; val terug op de map."""
    data = request.get_json(silent=True) or {}
    label = (data.get('label') or '').strip()
    full_path = (data.get('fullpath') or '').strip()
    if not full_path:
        return jsonify({'success': False, 'error': 'fullpath is vereist', 'not_mounted': False}), 400

    fp = Path(full_path)
    if not _path_within_archive_mount(fp):
        return jsonify({'success': False, 'error': 'Pad valt buiten het archief-mount-pad', 'not_mounted': False}), 403
    if not fp.exists():
        return jsonify({
            'success': False,
            'error': 'Bestand niet toegankelijk — is de schijf aangesloten op de server?',
            'not_mounted': True,
            'label': label,
            'expected_path': str(fp),
        }), 404

    result = _open_file_or_fallback_dir(fp)
    status = 200 if result.get('success') else 500
    return jsonify(result), status


@app.route('/api/dir-listing')
def api_dir_listing():
    """Geef inhoud van een map op een gemounte archief-schijf.

    Query params: label, path (relative map-pad, of '' voor root van source_root)
    Geeft max 500 items terug gesorteerd op naam.
    """
    label = request.args.get('label', '').strip()
    rel_path = request.args.get('path', '').strip()
    if not label:
        return jsonify({'error': 'Geen label opgegeven'})

    # Zoek source_root voor dit label + pad
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    if rel_path:
        # Zoek een bestand in of onder deze map
        row = conn.execute(
            "SELECT DISTINCT source_root FROM files WHERE archive_label=? AND relative_path LIKE ? LIMIT 1",
            (label, rel_path.rstrip('/') + '%')).fetchone()
    else:
        # Root: pak eerste source_root voor dit label
        row = conn.execute(
            "SELECT DISTINCT source_root FROM files WHERE archive_label=? LIMIT 1",
            (label,)).fetchone()
    conn.close()

    if not row:
        return jsonify({'error': f'Geen bestanden gevonden voor {label}/{rel_path}'})

    source_root = row[0]
    dir_path = Path(source_root) / rel_path if rel_path else Path(source_root)

    if not dir_path.exists():
        return jsonify({
            'error': f'Map niet beschikbaar — is schijf {label} aangesloten op de server?',
            'not_mounted': True, 'label': label, 'expected_path': str(dir_path)
        })

    if not dir_path.is_dir():
        return jsonify({'error': f'{rel_path} is geen map'})

    entries = []
    try:
        for entry in sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat()
                entries.append({
                    'name': entry.name,
                    'is_dir': entry.is_dir(),
                    'size': stat.st_size if entry.is_file() else None,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                    'rel_path': str(Path(rel_path) / entry.name) if rel_path else entry.name,
                })
            except OSError:
                pass
    except PermissionError as e:
        return jsonify({'error': f'Geen toegang: {e}'})

    return jsonify({
        'label': label,
        'source_root': source_root,
        'path': rel_path,
        'entries': entries[:500],
        'truncated': len(entries) > 500,
    })


def _lookup_known_label(conn, partition, disk_model=None):
    """Zoek bekend archive-label voor een partitie via meerdere methoden.

    Volgorde: UUID → volume_label in DB → source_root-suffix → device_model.
    Slaat gevonden match op in physical_media voor toekomstig gebruik.

    Geeft terug: {'known_label': str|None, 'match_method': str|None, 'sticker_confirmed': bool}
    """
    vol_label = partition.get('label')  # Volume-label van lsblk (bijv. FREECOM_1.5)
    uuid = partition.get('uuid')

    def _get_sticker(archive_label):
        row = conn.execute(
            "SELECT sticker_confirmed FROM physical_media WHERE archive_label=?",
            (archive_label,)).fetchone()
        return bool(row[0]) if row else False

    def _store_vol_label(archive_label, vol_lbl, uid):
        """Update physical_media met volume_label en UUID als die nog ontbreken."""
        try:
            if vol_lbl:
                conn.execute(
                    "UPDATE physical_media SET volume_label=? WHERE archive_label=? AND (volume_label IS NULL OR volume_label='')",
                    (vol_lbl, archive_label))
            if uid:
                conn.execute(
                    "UPDATE physical_media SET filesystem_uuid=? WHERE archive_label=? AND (filesystem_uuid IS NULL OR filesystem_uuid='')",
                    (uid, archive_label))
            conn.commit()
        except Exception:
            pass

    # 1. UUID match (meest betrouwbaar)
    if uuid:
        row = conn.execute(
            "SELECT archive_label FROM physical_media WHERE filesystem_uuid=?",
            (uuid,)).fetchone()
        if row:
            _store_vol_label(row[0], vol_label, None)
            return {'known_label': row[0], 'match_method': 'uuid', 'sticker_confirmed': _get_sticker(row[0])}

    # 2. Volume-label match in physical_media
    # Let op: veel fabrikanten (WD, Seagate) gebruiken dezelfde standaard volume-labels
    # op al hun schijven (bijv. "Elements"). Controleer daarom ook de UUID als die
    # beschikbaar is — als de UUIDs allebei bekend zijn en NIET overeenkomen, is dit
    # een andere fysieke schijf met toevallig hetzelfde label.
    if vol_label:
        row = conn.execute(
            "SELECT archive_label, filesystem_uuid FROM physical_media WHERE volume_label=?",
            (vol_label,)).fetchone()
        if row:
            db_uuid = row[1]
            # UUID-conflict: beide UUIDs bekend maar niet gelijk → andere schijf, geen match
            if uuid and db_uuid and uuid.upper() != db_uuid.upper():
                pass  # Bewust geen match — val door naar methode 3
            else:
                _store_vol_label(row[0], None, uuid)
                return {'known_label': row[0], 'match_method': 'volume_label', 'sticker_confirmed': _get_sticker(row[0])}

    # 2b. Canonieke match vanuit volume-label zoals "AD 007 WD2TB"
    canonical_label = _canonicalize_archive_label(vol_label)
    if canonical_label:
        row = conn.execute(
            "SELECT archive_label FROM physical_media WHERE archive_label=?",
            (canonical_label,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT DISTINCT archive_label FROM files WHERE archive_label=?",
                (canonical_label,)).fetchone()
        if row:
            _store_vol_label(canonical_label, vol_label, uuid)
            return {'known_label': canonical_label, 'match_method': 'canonical_volume_label', 'sticker_confirmed': _get_sticker(canonical_label)}

    # 3. Source root suffix: volume-label komt voor in eerder gescand pad
    if vol_label:
        row = conn.execute(
            "SELECT DISTINCT archive_label FROM files WHERE source_root LIKE ?",
            (f'%/{vol_label}',)).fetchone()
        if row:
            _store_vol_label(row[0], vol_label, uuid)
            return {'known_label': row[0], 'match_method': 'source_root', 'sticker_confirmed': _get_sticker(row[0])}

    # 4. Device model match (zwakste: niet uniek, maar beter dan niets)
    if disk_model:
        row = conn.execute(
            "SELECT archive_label FROM physical_media WHERE device_model=? LIMIT 1",
            (disk_model,)).fetchone()
        if row:
            _store_vol_label(row[0], vol_label, uuid)
            return {'known_label': row[0], 'match_method': 'model', 'sticker_confirmed': _get_sticker(row[0])}

    return {'known_label': None, 'match_method': None, 'sticker_confirmed': False}


def _classify_mount_state(mountpoint, readonly):
    """Bepaal de mount-staat van een partitie.

    Geeft terug: 'not_mounted' | 'rw' | 'ro_archive' | 'ro_other'
    - ro_archive: gemount RO onder /mnt/archive-ingest/
    - ro_other: gemount RO elders (bijv. /media/devmon/)
    - rw: gemount maar schrijfbaar (niet ideaal voor archief)
    - not_mounted: niet gemount
    """
    if not mountpoint:
        return 'not_mounted'
    if readonly:
        if mountpoint.startswith(str(MOUNT_BASE)):
            return 'ro_archive'
        return 'ro_other'
    return 'rw'


@app.route('/api/disks')
def api_disks():
    """Detecteer aangesloten externe schijven, gegroepeerd per fysieke schijf.

    Verrijkt elke partitie met:
    - known_label: bekend archive-label (of None als nieuw medium)
    - match_method: hoe het label gevonden is (uuid/volume_label/source_root/model)
    - sticker_confirmed: of de sticker al bevestigd is
    - mount_state: not_mounted / rw / ro_archive / ro_other
    """
    disks = []
    try:
        result = subprocess.run(['lsblk', '-J', '-o',
            'NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,RM'],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            conn = sqlite3.connect(str(DB_PATH), timeout=10)
            for dev in data.get('blockdevices', []):
                if dev.get('tran') != 'usb':
                    continue
                children = dev.get('children', [])
                model = dev.get('model', '')
                partitions = []
                for child in (children if children else [dev]):
                    if child.get('type') not in ('part', 'disk'):
                        continue
                    if not child.get('fstype'):
                        continue
                    mp = child.get('mountpoint')
                    readonly = False
                    if mp:
                        try:
                            mnt_out = subprocess.run(['findmnt', '-n', '-o', 'OPTIONS', mp],
                                capture_output=True, text=True, timeout=5)
                            readonly = 'ro' in mnt_out.stdout.split(',') if mnt_out.returncode == 0 else False
                        except Exception:
                            pass
                    part = {
                        'device': f"/dev/{child['name']}",
                        'size': child.get('size', '0'),
                        'fstype': child.get('fstype'),
                        'label': child.get('label'),
                        'uuid': child.get('uuid'),
                        'mountpoint': mp,
                        'readonly': readonly,
                        'mount_state': _classify_mount_state(mp, readonly),
                    }
                    # DB-match voor herkenning
                    part.update(_lookup_known_label(conn, part, model))
                    # Laatste succesvolle scan ophalen voor bekende labels
                    if part.get('known_label'):
                        scan_row = conn.execute(
                            "SELECT end_time, files_ok, status FROM scans "
                            "WHERE archive_label=? AND status='completed' AND files_ok > 0 "
                            "ORDER BY end_time DESC LIMIT 1",
                            (part['known_label'],)).fetchone()
                        if scan_row:
                            part['last_scan'] = scan_row[0][:10]   # JJJJ-MM-DD
                            part['last_scan_files'] = scan_row[1]
                        else:
                            part['last_scan'] = None
                            part['last_scan_files'] = 0
                    partitions.append(part)
                if partitions:
                    disks.append({
                        'parent_device': f"/dev/{dev['name']}",
                        'total_size': dev.get('size', '0'),
                        'model': model,
                        'serial': dev.get('serial'),
                        'media_type': 'usb_hdd',
                        'partitions': partitions,
                    })
            conn.close()
    except Exception:
        pass
    return jsonify({'disks': disks})


@app.route('/api/mounted')
def api_mounted():
    """Lijst van gemounte archive schijven (disk-niveau)."""
    mounted = []
    if MOUNT_BASE.exists():
        for d in MOUNT_BASE.iterdir():
            if d.is_dir():
                # Check of het zelf een mount is, of subdirectories mounts bevatten
                sub_mounts = []
                if d.is_mount():
                    sub_mounts.append({'name': d.name, 'mountpoint': str(d)})
                else:
                    for sub in d.iterdir():
                        if sub.is_mount():
                            sub_mounts.append({'name': sub.name, 'mountpoint': str(sub)})
                if sub_mounts:
                    mounted.append({
                        'label': d.name,
                        'mountpoint': str(d),
                        'partitions': sub_mounts,
                        'partition_count': len(sub_mounts),
                    })
    return jsonify({'mounted': mounted})


@app.route('/api/mount', methods=['POST'])
def api_mount():
    """Mount een device read-only."""
    data = request.get_json()
    device = data.get('device')
    label = (data.get('label') or '').strip()
    if not device or not label:
        return jsonify({'success': False, 'message': 'Device en label zijn vereist'})
    canonical_label = _canonicalize_archive_label(label) or label
    try:
        known_state = _known_label_mount_state(canonical_label)
        if known_state['connected'] and len(known_state['expected_mounts']) > 1:
            prepared = _prepare_connected_known_disk(canonical_label)
            return jsonify(prepared)

        result = subprocess.run(
            [str(PROJECT_DIR / 'mount_readonly.sh'), device, canonical_label],
            capture_output=True, text=True, timeout=30)
        clean = re.sub(r'\033\[[0-9;]*m', '', result.stdout + result.stderr)
        if result.returncode == 0:
            partition = _find_partition_details(device)
            if partition:
                _register_media_metadata(canonical_label, partition)
            msg = f'{canonical_label} gemount als read-only'
            if canonical_label != label:
                msg += f' (ingevoerd als "{label}")'
            return jsonify({
                'success': True,
                'message': msg,
                'label_used': canonical_label,
                'output': clean
            })
        else:
            return jsonify({'success': False, 'message': f'Mount mislukt: {clean[-300:]}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/prepare-label', methods=['POST'])
def api_prepare_label():
    """Herstel de canonieke mount-layout voor een bekende archiefschijf."""
    data = request.get_json()
    label = _canonicalize_archive_label((data.get('label') or '').strip()) or (data.get('label') or '').strip()
    if not label:
        return jsonify({'success': False, 'message': 'Label is verplicht'})
    prepared = _prepare_connected_known_disk(label)
    return jsonify(prepared)


@app.route('/api/eject', methods=['POST'])
def api_eject():
    """Unmount een archive schijf (alle partities).

    Strategie (in volgorde):
    1. Unmount alles onder MOUNT_BASE/label (archief-pad)
    2. Als niets gemount was op archief-pad én device meegegeven:
       probeer device-gebaseerde unmount (voor devmon-mounts op /media/...)
    3. Powert het parent-device veilig af via udisksctl indien beschikbaar
    """
    data = request.get_json()
    label = data.get('label')
    device = data.get('device')  # optioneel — meegegeven vanuit het Beheer-scherm
    if not label and not device:
        return jsonify({'success': False, 'message': 'Label of device is vereist'})

    try:
        errors = []
        unmounted = 0
        power_result = None

        # --- Stap 1: unmount via archief-pad ---
        if label:
            mount_path = MOUNT_BASE / label
            if mount_path.exists() and mount_path.is_dir():
                for sub in sorted(mount_path.iterdir(), reverse=True):
                    if sub.is_mount():
                        r = subprocess.run(['sudo', 'umount', str(sub)],
                            capture_output=True, text=True, timeout=15)
                        if r.returncode == 0:
                            unmounted += 1
                        else:
                            errors.append(f'{sub.name}: {r.stderr[:100]}')
                if mount_path.is_mount():
                    r = subprocess.run(['sudo', 'umount', str(mount_path)],
                        capture_output=True, text=True, timeout=15)
                    if r.returncode == 0:
                        unmounted += 1
                    else:
                        errors.append(f'{label}: {r.stderr[:100]}')

        # --- Stap 2: als nog niets unmounted én device beschikbaar → unmount device ---
        device_unmounted = False
        if unmounted == 0 and device and re.match(r'^/dev/[a-zA-Z0-9]+$', device):
            # Zoek huidige mountpoint van dit device
            mp_out = subprocess.run(['findmnt', '-n', '-o', 'TARGET', device],
                capture_output=True, text=True, timeout=5)
            if mp_out.returncode == 0:
                for mp in mp_out.stdout.strip().split('\n'):
                    mp = mp.strip()
                    if mp:
                        r = subprocess.run(['sudo', 'umount', mp],
                            capture_output=True, text=True, timeout=15)
                        if r.returncode == 0:
                            unmounted += 1
                            device_unmounted = True
                        else:
                            errors.append(f'{mp}: {r.stderr[:100]}')

        # --- Stap 3a: netwerk-USB (USB/IP) loskoppelen indien van toepassing ---
        # Een remote schijf wordt niet via udisksctl afgezet maar via 'usbip detach'.
        remote_detach = None
        detach_target = device
        if not detach_target and label:
            # Zoek het device dat bij dit label hoort via de state.
            for _e in _remote_state_read():
                detach_target = _e.get("dev")
                if detach_target:
                    break
        if detach_target:
            remote_detach = _maybe_detach_remote(detach_target)

        # --- Stap 3b: parent-device veilig uitzetten (alleen lokale schijven) ---
        if device and not (remote_detach and remote_detach.get("detached")):
            power_result = _power_off_block_device(device)

        # --- Database bijwerken ---
        if label:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("UPDATE files SET availability_status='archive_offline' WHERE archive_label=?", (label,))
            conn.commit()
            conn.close()

        # --- Resultaat ---
        if errors and unmounted == 0 and not (power_result and power_result.get('success')):
            return jsonify({'success': False,
                'message': f'Uitwerpen mislukt: {"; ".join(errors)}'})
        if unmounted == 0 and not (power_result and power_result.get('success')):
            loc_msg = f' (was niet gemount op archief-pad)' if label else ''
            return jsonify({'success': False,
                'message': f'{label or device} was niet gemount{loc_msg}'})
        where = 'via device' if device_unmounted else 'via archief-pad'
        warn_parts = []
        if errors:
            warn_parts.append('; '.join(errors))
        if remote_detach:
            if remote_detach.get('detached'):
                where += f" + netwerk-USB losgekoppeld (poort {remote_detach.get('port')})"
            else:
                warn_parts.append(
                    f"netwerk-USB niet losgekoppeld: {remote_detach.get('error', 'onbekende fout')}")
        if power_result:
            if power_result.get('powered_off'):
                where += f' + power-off {power_result.get("device", "")}'.rstrip()
            elif power_result.get('already_absent'):
                where += ' + device al afwezig'
            elif power_result.get('attempted') and not power_result.get('success'):
                warn_parts.append(f"device niet uitgeschakeld: {power_result.get('message', '')}")
        warn = f' — let op: {"; ".join(warn_parts)}' if warn_parts else ''
        return jsonify({'success': True,
            'message': f'{label or device} uitgeworpen ({unmounted} partities, {where}){warn}. Veilig loskoppelen.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# ==========================================================================
# Netwerk-USB (USB/IP) — schijf op een andere machine als lokale /dev/sdX
# ==========================================================================

def _usbip_log(msg):
    """Log netwerk-USB gebeurtenissen (geen silent failure)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now().isoformat()} {msg}\n"
        with open(LOG_DIR / "network-usb.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as e:
        print(f"[USBIP] log-fout: {e} — bericht: {msg}")


def _load_remote_hosts():
    """Lees config/remote_hosts.yaml. Retourneert lijst van dicts (leeg bij afwezig)."""
    try:
        import yaml
        if not REMOTE_HOSTS_CONFIG.exists():
            return []
        data = yaml.safe_load(REMOTE_HOSTS_CONFIG.read_text(encoding="utf-8")) or {}
        hosts = data.get("hosts") or []
        result = []
        for h in hosts:
            if not isinstance(h, dict) or not h.get("host"):
                continue
            result.append({
                "name": str(h.get("name") or h.get("host")),
                "host": str(h.get("host")).strip(),
                "os": str(h.get("os") or "unknown").strip().lower(),
                "notes": str(h.get("notes") or ""),
            })
        return result
    except Exception as e:
        _usbip_log(f"config-fout remote_hosts.yaml: {e}")
        return []


def _host_reachable(host, port=USBIP_PORT, timeout=1.5):
    """Snelle TCP-check op de usbipd-poort van een exporter."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _run_usbip_ctl(args, timeout=30):
    """Roep network-usb/usbip_ctl.sh aan via bash (geen afhankelijkheid van exec-bit).

    Retourneert (returncode, stdout, stderr).
    """
    try:
        proc = subprocess.run(
            ["bash", str(USBIP_CTL), *args],
            capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout na {timeout}s bij usbip_ctl {' '.join(args)}"
    except Exception as e:
        return 1, "", str(e)


def _parse_usbip_list(text):
    """Parse 'usbip list -r' output naar [{busid, description}].

    Voorbeeldregel:  '        1-4: Seagate Expansion : ... (0bc2:2320)'
    """
    devices = []
    busid_re = re.compile(r'^\s*([0-9]+-[0-9.]+)\s*:\s*(.*)$')
    for line in text.splitlines():
        stripped = line.strip()
        # Vervolgregels beginnen met ':' (sysfs-pad, klasse, vendor:product)
        if stripped.startswith(':'):
            continue
        m = busid_re.match(line)
        if m:
            busid = m.group(1)
            desc = m.group(2).strip()
            devices.append({"busid": busid, "description": desc or busid})
    return devices


def _parse_usbip_ports(text):
    """Parse 'usbip port' output naar [{port, host, busid}].

    Relevante regels:
      'Port 00: <Port in Use> at High Speed(480Mbps)'
      '       3-1 -> usbip://<jouw-machine-ip>:3240/1-4'
    """
    ports = []
    current_port = None
    port_re = re.compile(r'^Port\s+(\d+):')
    url_re = re.compile(r'usbip://([^:/]+):\d+/([0-9]+-[0-9.]+)')
    for line in text.splitlines():
        pm = port_re.match(line.strip())
        if pm:
            current_port = pm.group(1)
            continue
        um = url_re.search(line)
        if um and current_port is not None:
            ports.append({
                "port": current_port,
                "host": um.group(1),
                "busid": um.group(2),
            })
    return ports


def _snapshot_usb_disks():
    """Set van parent USB-schijven (bv. {'sda','sdb'}) via lsblk."""
    disks = set()
    try:
        r = subprocess.run(["lsblk", "-dn", "-o", "NAME,TYPE,TRAN"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "disk" and parts[2] == "usb":
                disks.add(parts[0])
    except Exception as e:
        _usbip_log(f"lsblk-snapshot fout: {e}")
    return disks


def _remote_state_read():
    """Lees data/remote_usbip_state.json (lijst van attachments)."""
    try:
        if REMOTE_STATE_FILE.exists():
            return json.loads(REMOTE_STATE_FILE.read_text(encoding="utf-8")) or []
    except Exception as e:
        _usbip_log(f"state-lees-fout: {e}")
    return []


def _remote_state_write(entries):
    try:
        REMOTE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        REMOTE_STATE_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except Exception as e:
        _usbip_log(f"state-schrijf-fout: {e}")


def _parent_disk_name(device):
    """/dev/sda1 -> sda,  /dev/sda -> sda."""
    name = str(device or "").strip()
    name = name.split("/")[-1]
    return re.sub(r'\d+$', '', name)


def _maybe_detach_remote(device):
    """Detach een via USB/IP gekoppelde schijf bij het uitwerpen.

    Zoekt in de state of `device` (of het parent-device) hoort bij een remote
    attachment; zo ja: 'usbip detach -p <port>' en state opschonen.
    Retourneert een korte statusdict of None als er niets te detachen viel.
    """
    if not device:
        return None
    target = _parent_disk_name(device)
    entries = _remote_state_read()
    remaining = []
    detached = None
    for e in entries:
        e_dev = _parent_disk_name(e.get("dev", ""))
        if e_dev and e_dev == target and not detached:
            rc, out, err = _run_usbip_ctl(["detach", str(e.get("port", ""))])
            if rc == 0:
                detached = {"detached": True, "port": e.get("port"),
                            "host": e.get("host"), "busid": e.get("busid")}
                _usbip_log(f"detach OK port={e.get('port')} host={e.get('host')} busid={e.get('busid')} dev={e.get('dev')}")
            else:
                detached = {"detached": False, "port": e.get("port"),
                            "host": e.get("host"), "error": (err or out).strip()[:200]}
                _usbip_log(f"detach FOUT port={e.get('port')}: {(err or out).strip()[:200]}")
                remaining.append(e)  # laat staan zodat gebruiker het opnieuw kan proberen
        else:
            remaining.append(e)
    if detached is not None:
        _remote_state_write(remaining)
    return detached


@app.route('/api/remote/hosts')
def api_remote_hosts():
    """Geef geconfigureerde exporter-hosts + snelle bereikbaarheids-check."""
    hosts = _load_remote_hosts()
    for h in hosts:
        h["reachable"] = _host_reachable(h["host"])
    return jsonify({"hosts": hosts})


@app.route('/api/remote/devices')
def api_remote_devices():
    """Toon exporteerbare USB-devices op een exporter-host.

    Query param: host (IP/hostname). Geeft nooit een kale fout: bij problemen
    komt {devices: [], error: '...'} terug zodat de UI het kan tonen.
    """
    host = request.args.get('host', '').strip()
    if not host:
        return jsonify({"devices": [], "error": "host-parameter ontbreekt"}), 400
    if not _host_reachable(host):
        return jsonify({
            "devices": [], "host": host,
            "error": f"{host} niet bereikbaar op poort {USBIP_PORT} — "
                     f"draait de usbipd-exporter en is de firewall open?"})
    rc, out, err = _run_usbip_ctl(["list", host], timeout=20)
    if rc != 0:
        _usbip_log(f"list {host} rc={rc}: {(err or out).strip()[:200]}")
        return jsonify({"devices": [], "host": host,
                        "error": (err or out).strip()[:300] or "usbip list mislukt"})
    devices = _parse_usbip_list(out)
    return jsonify({"devices": devices, "host": host})


@app.route('/api/remote/ports')
def api_remote_ports():
    """Toon actieve remote attachments (importer-zijde), verrijkt met state."""
    rc, out, err = _run_usbip_ctl(["ports"], timeout=15)
    if rc != 0:
        return jsonify({"ports": [], "error": (err or out).strip()[:300] or "usbip port mislukt"})
    ports = _parse_usbip_ports(out)
    state = {str(e.get("port")): e for e in _remote_state_read()}
    for p in ports:
        st = state.get(str(p["port"]))
        if st:
            p["dev"] = st.get("dev")
            p["attached_at"] = st.get("attached_at")
            p["host_name"] = st.get("host_name")
    return jsonify({"ports": ports})


@app.route('/api/remote/attach', methods=['POST'])
def api_remote_attach():
    """Attach een remote USB-device en bepaal de resulterende /dev/sdX.

    Body: {host, busid}. Daarna verloopt labelen/mounten/scannen via de
    bestaande schijven-flow (de schijf verschijnt in /api/disks).
    """
    data = request.get_json(silent=True) or {}
    host = (data.get('host') or '').strip()
    busid = (data.get('busid') or '').strip()
    if not host or not busid:
        return jsonify({"success": False, "message": "host en busid zijn vereist"})
    if not re.match(r'^[0-9]+-[0-9.]+$', busid):
        return jsonify({"success": False, "message": f"ongeldige busid: {busid}"})

    host_name = next((h["name"] for h in _load_remote_hosts() if h["host"] == host), host)
    before = _snapshot_usb_disks()
    rc, out, err = _run_usbip_ctl(["attach", host, busid], timeout=40)
    if rc != 0:
        msg = (err or out).strip()[:300] or "usbip attach mislukt"
        _usbip_log(f"attach FOUT host={host} busid={busid}: {msg}")
        return jsonify({"success": False, "message": f"Attach mislukt: {msg}"})

    # Wacht tot de nieuwe schijf verschijnt (kernel enumeratie kan even duren).
    new_dev = None
    for _ in range(16):  # ~8s
        time.sleep(0.5)
        after = _snapshot_usb_disks()
        fresh = after - before
        if fresh:
            new_dev = "/dev/" + sorted(fresh)[0]
            break

    # Zoek het poortnummer bij deze host+busid.
    port = None
    prc, pout, _perr = _run_usbip_ctl(["ports"], timeout=15)
    if prc == 0:
        for p in _parse_usbip_ports(pout):
            if p["host"] == host and p["busid"] == busid:
                port = p["port"]
                break

    # Leg de attachment vast in de state (voor detach bij eject).
    entries = [e for e in _remote_state_read()
               if not (e.get("host") == host and e.get("busid") == busid)]
    entries.append({
        "port": port, "host": host, "host_name": host_name, "busid": busid,
        "dev": new_dev, "attached_at": datetime.now().isoformat(),
    })
    _remote_state_write(entries)
    _usbip_log(f"attach OK host={host} busid={busid} port={port} dev={new_dev}")

    if new_dev:
        return jsonify({
            "success": True, "device": new_dev, "port": port, "host": host,
            "message": f"Schijf gekoppeld als {new_dev} (poort {port}). "
                       f"Ga naar de schijvenlijst om te labelen, mounten en scannen."})
    # Attach lukte maar geen nieuwe /dev gezien — meld dit expliciet (geen silent failure).
    return jsonify({
        "success": True, "device": None, "port": port, "host": host,
        "message": "Attach uitgevoerd, maar er verscheen (nog) geen nieuwe schijf. "
                   "Vernieuw de schijvenlijst; controleer 'usbip port' op de server."})


# Whitelist van exporter-bestanden die de server mag uitserveren (onboarding nieuwe machine).
_NETWORK_USB_FILES = {
    'install-usbipd.ps1': NETWORK_USB_DIR / 'windows' / 'install-usbipd.ps1',
    'export-disk.ps1':    NETWORK_USB_DIR / 'windows' / 'export-disk.ps1',
    'archsw-loc-agent.ps1': NETWORK_USB_DIR / 'agent' / 'windows' / 'archsw-loc-agent.ps1',
    'install-agent.ps1':  NETWORK_USB_DIR / 'agent' / 'windows' / 'install-agent.ps1',
    'setup-exporter.sh':  NETWORK_USB_DIR / 'linux' / 'setup-exporter.sh',
    'bind-disk.sh':       NETWORK_USB_DIR / 'linux' / 'bind-disk.sh',
    'archsw-loc-agent.py': NETWORK_USB_DIR / 'agent' / 'linux' / 'archsw-loc-agent.py',
    'install-agent.sh':   NETWORK_USB_DIR / 'agent' / 'linux' / 'install-agent.sh',
    'README.md':          NETWORK_USB_DIR / 'README.md',
}


@app.route('/netwerk-usb/dl/<name>')
def netwerk_usb_download(name):
    """Serveer een exporter-script (whitelist) als download voor een nieuwe machine."""
    p = _NETWORK_USB_FILES.get(name)
    if not p or not p.exists():
        abort(404)
    return send_file(str(p), as_attachment=True, download_name=name)


@app.route('/netwerk-usb')
def netwerk_usb_setup_page():
    """Onboarding-pagina: alles om een nieuwe machine als USB/IP-exporter in te richten.

    Bereikbaar via http://<server>:5059/netwerk-usb — één plek, geen zoeken.
    """
    host = request.host  # bv. <server-ip>:5059
    base = f"http://{host}/netwerk-usb/dl"
    page = f"""<!DOCTYPE html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Netwerk-USB — nieuwe machine instellen</title>
<style>
 body{{font-family:-apple-system,Segoe UI,sans-serif;background:#1a1a2e;color:#e0e0e0;
   max-width:820px;margin:0 auto;padding:20px;line-height:1.5}}
 h1{{color:#00d4aa;font-size:1.5em}} h2{{color:#4fc3f7;margin-top:22px}}
 code,pre{{font-family:Consolas,monospace}}
 pre{{background:#0f3460;border:1px solid #1a4080;border-radius:6px;padding:10px 12px;
   overflow-x:auto;white-space:pre-wrap;word-break:break-word}}
 a{{color:#00d4aa}} .card{{background:#16213e;border:1px solid #0f3460;border-radius:8px;
   padding:12px 16px;margin:10px 0}} .tag{{color:#888}}
</style></head><body>
<p style="margin-bottom:14px"><a href="/">&#8592; Terug naar zoeken / de app</a></p>
<h1>🌐 Netwerk-USB — nieuwe machine instellen</h1>
<p>Richt de machine <strong>waar de schijf aan hangt</strong> in als USB/IP-exporter. De
de server leest de schijf dan read-only uit en indexeert hem. Alle bestanden staan
hieronder — niets zoeken nodig.</p>

<div class="card">
<h2>Windows &mdash; automatisch (aanbevolen)</h2>
<p>Installeer eenmalig de <strong>ArchSW-loc agent</strong>. Daarna hoef je nooit meer iets te typen:
de app leest een bestand rechtstreeks van je schijf (of koppelt hem via USB/IP als dat nodig is)
zodra je in de zoekresultaten op <em>Bekijken</em> klikt. Open <strong>PowerShell als Administrator</strong> en plak:</p>
<pre>irm {base}/install-usbipd.ps1 -OutFile $env:TEMP\\install-usbipd.ps1
irm {base}/archsw-loc-agent.ps1 -OutFile $env:TEMP\\archsw-loc-agent.ps1
irm {base}/install-agent.ps1 -OutFile $env:TEMP\\install-agent.ps1
&amp; $env:TEMP\\install-usbipd.ps1
&amp; $env:TEMP\\install-agent.ps1 -ServerUrl "http://{host}"</pre>
<p class="tag">De agent draait als achtergrond-service (start automatisch mee) en meldt zich aan
bij de server. Verwijderen: <code>&amp; $env:TEMP\\install-agent.ps1 -Uninstall</code></p>
</div>

<div class="card">
<h2>Windows &mdash; handmatig (geavanceerd, zonder agent)</h2>
<p>Alleen nodig als je geen achtergrond-service wilt. PowerShell als Administrator:</p>
<pre>irm {base}/install-usbipd.ps1 -OutFile $env:TEMP\\install-usbipd.ps1
irm {base}/export-disk.ps1 -OutFile $env:TEMP\\export-disk.ps1
&amp; $env:TEMP\\install-usbipd.ps1
&amp; $env:TEMP\\export-disk.ps1 -List          # zoek de busid
&amp; $env:TEMP\\export-disk.ps1 -BusId 2-4      # deel die schijf</pre>
<p class="tag">Losse downloads:
<a href="{base}/install-usbipd.ps1">install-usbipd.ps1</a> ·
<a href="{base}/export-disk.ps1">export-disk.ps1</a> ·
<a href="{base}/archsw-loc-agent.ps1">archsw-loc-agent.ps1</a> ·
<a href="{base}/install-agent.ps1">install-agent.ps1</a></p>
</div>

<div class="card">
<h2>Linux</h2>
<pre>curl -fsSL {base}/setup-exporter.sh -o setup-exporter.sh
curl -fsSL {base}/bind-disk.sh -o bind-disk.sh
bash setup-exporter.sh          # eenmalig inrichten
bash bind-disk.sh               # toon busid
bash bind-disk.sh 1-4           # deel die schijf</pre>
<p class="tag">Losse downloads:
<a href="{base}/setup-exporter.sh">setup-exporter.sh</a> ·
<a href="{base}/bind-disk.sh">bind-disk.sh</a></p>
</div>

<div class="card">
<h2>Daarna koppelen</h2>
<p>Ga naar <a href="http://{host}/">de workbench</a> → tab <strong>Beheer</strong> →
paneel <strong>Netwerk-USB</strong> → kies de machine → <strong>Koppel aan server</strong>.
De schijf verschijnt dan bij <em>Aangesloten schijven</em> om te labelen, mounten en scannen.</p>
</div>

<p class="tag">Volledige uitleg + beveiliging:
<a href="{base}/README.md">network-usb/README.md</a>.
USB/IP-poort 3240 is onversleuteld → gebruik dit op een vertrouwd LAN.</p>
</body></html>"""
    return page


@app.route('/api/remote/detach', methods=['POST'])
def api_remote_detach():
    """Detach een remote attachment op poortnummer. Body: {port}."""
    data = request.get_json(silent=True) or {}
    port = str(data.get('port', '')).strip()
    if not port or not re.match(r'^\d+$', port):
        return jsonify({"success": False, "message": "geldig poortnummer is vereist"})
    rc, out, err = _run_usbip_ctl(["detach", port], timeout=20)
    if rc != 0:
        msg = (err or out).strip()[:300] or "usbip detach mislukt"
        _usbip_log(f"detach FOUT port={port}: {msg}")
        return jsonify({"success": False, "message": f"Detach mislukt: {msg}"})
    entries = [e for e in _remote_state_read() if str(e.get("port")) != port]
    _remote_state_write(entries)
    _usbip_log(f"detach OK port={port}")
    return jsonify({"success": True, "message": f"Poort {port} losgekoppeld."})


# ==========================================================================
# Auto-koppelen bij openen — archief-agent per machine (ontwerp: Mantis #1113)
# ==========================================================================

def _load_agents():
    """Lees data/remote_agents.json → dict {ip: {port, token, name, registered_at}}."""
    try:
        if REMOTE_AGENTS_FILE.exists():
            return json.loads(REMOTE_AGENTS_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _usbip_log(f"agents-lees-fout: {e}")
    return {}


def _save_agents(agents):
    try:
        REMOTE_AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        REMOTE_AGENTS_FILE.write_text(json.dumps(agents, indent=2), encoding="utf-8")
    except Exception as e:
        _usbip_log(f"agents-schrijf-fout: {e}")


def _agent_ips_for(remote_ip):
    """Welke agent(s) horen bij dit verzoek?

    Normaal: de agent op het bron-IP van de browser. Maar bij een VPN/proxy/tweede
    netwerkkaart ziet de server een ander IP dan waaronder de agent zich aanmeldde.
    Daarom: als het bron-IP geen agent heeft, val terug op alle geregistreerde agents
    (in een persoonlijke setup is dat de eigen machine). Voorkomt vals 'niet aangesloten'.
    """
    agents = _load_agents()
    if remote_ip in agents:
        return [remote_ip]
    return sorted(agents.keys())


def _host_display_name(ip):
    """Vriendelijke naam voor een IP uit remote_hosts.yaml, anders het IP zelf."""
    for h in _load_remote_hosts():
        if h["host"] == ip:
            return h["name"]
    return ip


def _agent_request(ip, method, path, body=None, timeout=8):
    """Roep de archief-agent op host `ip` aan. Retourneert (ok, data_of_foutmelding)."""
    import urllib.request
    agents = _load_agents()
    agent = agents.get(ip)
    if not agent:
        return False, "geen agent geregistreerd voor deze machine"
    url = f"http://{ip}:{agent.get('port', AGENT_DEFAULT_PORT)}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Agent-Token", agent.get("token", ""))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode())
    except Exception as e:
        return False, str(e)


def _volume_label_for(label):
    """Volume-label voor een archive-label (om op de exporter de juiste schijf te vinden)."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        row = conn.execute("SELECT volume_label FROM physical_media WHERE archive_label=?",
                           (label,)).fetchone()
        conn.close()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def _agent_get_raw(ip, path, timeout=30):
    """GET een ruwe respons (bytes) van de agent. Retourneert (status, bytes, content_type, reason)."""
    import urllib.request
    import urllib.error
    agents = _load_agents()
    agent = agents.get(ip)
    if not agent:
        return (0, None, None, "no_agent")
    url = f"http://{ip}:{agent.get('port', AGENT_DEFAULT_PORT)}{path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Agent-Token", agent.get("token", ""))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (r.status, r.read(), r.headers.get("Content-Type"), None)
    except urllib.error.HTTPError as e:
        reason = None
        try:
            reason = json.loads(e.read().decode()).get("reason")
        except Exception:
            pass
        return (e.code, None, None, reason)
    except Exception as e:
        return (0, None, None, str(e))


def _expected_uuids_for_label(label):
    """Verwachte filesystem-UUID(s) voor een archive-label uit physical_media."""
    uuids = set()
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        for (u,) in conn.execute(
                "SELECT filesystem_uuid FROM physical_media WHERE archive_label=?", (label,)):
            if u:
                uuids.add(str(u).strip().lower())
        conn.close()
    except Exception as e:
        _usbip_log(f"uuid-lookup fout voor {label}: {e}")
    return uuids


def _device_partition_uuids(dev):
    """Set van (lowercase) UUID's van een device en zijn partities via lsblk."""
    uuids = set()
    try:
        r = subprocess.run(["lsblk", "-no", "UUID", dev],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            u = line.strip().lower()
            if u:
                uuids.add(u)
    except Exception as e:
        _usbip_log(f"lsblk UUID fout voor {dev}: {e}")
    return uuids


def _find_usb_dev_by_uuid(expected):
    """Zoek onder de huidige USB-schijven er een met een matchende UUID (voor al-geattacht)."""
    if not expected:
        return None
    for name in _snapshot_usb_disks():
        dev = "/dev/" + name
        if expected & _device_partition_uuids(dev):
            return dev
    return None


def _wait_new_usb_dev(before, tries=16, delay=0.5):
    """Wacht tot een nieuwe USB-schijf verschijnt; retourneer /dev/<naam> of None."""
    for _ in range(tries):
        time.sleep(delay)
        fresh = _snapshot_usb_disks() - before
        if fresh:
            return "/dev/" + sorted(fresh)[0]
    return None


def _find_port_for(ip, busid):
    """Zoek het usbip-poortnummer voor host+busid via 'usbip port'."""
    rc, out, _err = _run_usbip_ctl(["ports"], timeout=15)
    if rc == 0:
        for p in _parse_usbip_ports(out):
            if p["host"] == ip and p["busid"] == busid:
                return p["port"]
    return None


def _rollback_attach(ip, busid):
    """Detach op de server en unbind op de agent (opruimen bij verkeerde schijf)."""
    port = _find_port_for(ip, busid)
    if port:
        _run_usbip_ctl(["detach", str(port)])
    _agent_request(ip, "POST", "/unbind", {"busid": busid})


@app.route('/api/remote/register-agent', methods=['POST'])
def api_remote_register_agent():
    """Een archief-agent meldt zich aan (vanaf zijn eigen machine).

    De host wordt bepaald uit het bron-IP van het verzoek; token + poort worden
    opgeslagen zodat de app de agent later mag aanroepen.
    """
    data = request.get_json(silent=True) or {}
    port = int(data.get("port") or AGENT_DEFAULT_PORT)
    token = (data.get("token") or "").strip()
    ip = request.remote_addr
    if not token:
        return jsonify({"success": False, "message": "token ontbreekt"}), 400
    agents = _load_agents()
    agents[ip] = {
        "port": port,
        "token": token,
        "name": _host_display_name(ip),
        "registered_at": datetime.now().isoformat(),
    }
    _save_agents(agents)
    _usbip_log(f"agent geregistreerd: {ip}:{port} ({agents[ip]['name']})")
    return jsonify({"success": True, "host": ip, "name": agents[ip]["name"]})


@app.route('/api/remote/whoami')
def api_remote_whoami():
    """Welke machine benadert de app nu (voor 'de machine waar je op werkt')."""
    ip = request.remote_addr
    agent = _load_agents().get(ip)
    return jsonify({
        "ip": ip,
        "name": _host_display_name(ip),
        "has_agent": bool(agent),
    })


@app.route('/api/remote/ensure-disk', methods=['POST'])
def api_remote_ensure_disk():
    """Zorg dat de schijf van `label` beschikbaar is op de server — automatisch.

    Bepaalt de machine waar je op werkt (bron-IP), vraagt zijn archief-agent de
    juiste schijf te delen, koppelt hem via USB/IP aan de server, verifieert via
    de filesystem-UUID en mount read-only. Retourneert een status voor de UI:
      already | coupled | not_present | wrong_disk | ambiguous | no_agent | error
    """
    data = request.get_json(silent=True) or {}
    label = _canonicalize_archive_label((data.get("label") or "").strip()) or (data.get("label") or "").strip()
    if not label:
        return jsonify({"status": "error", "message": "label is vereist"})

    ip = request.remote_addr
    name = _host_display_name(ip)

    # Al gemount? Dan is er niets te doen.
    mount_path = MOUNT_BASE / label
    if mount_path.exists() and (mount_path.is_mount() or any(p.is_mount() for p in mount_path.iterdir() if p.is_dir())):
        return jsonify({"status": "already", "label": label, "message": f"{label} is al gekoppeld."})

    agents = _load_agents()
    if ip not in agents:
        return jsonify({
            "status": "no_agent", "ip": ip, "host": name, "label": label,
            "message": f"Op {name} draait nog geen ArchSW-loc agent. Installeer hem eenmalig via /netwerk-usb, dan gaat dit voortaan vanzelf."
        })

    ok, disks = _agent_request(ip, "GET", "/disks", timeout=10)
    if not ok:
        return jsonify({"status": "error", "label": label,
                        "message": f"Archief-agent op {name} onbereikbaar: {disks}"})

    candidates = [d for d in disks.get("bindable", [])
                  if d.get("likely_disk") and d.get("state") in ("Not shared", "Shared")]
    if not candidates:
        return jsonify({
            "status": "not_present", "label": label, "host": name,
            "message": f"Sluit schijf {label} aan op {name}. Zodra hij zichtbaar is, koppel ik hem automatisch."
        })

    expected = _expected_uuids_for_label(label)
    _usbip_log(f"ensure-disk {label} op {ip}: {len(candidates)} kandidaat(en), verwachte uuids={expected or 'onbekend'}")

    for c in candidates:
        busid = c.get("busid")
        state = c.get("state", "")
        # Alleen binden als de schijf nog niet gedeeld is; 'Shared'/'Attached' overslaan.
        if state == "Not shared":
            okb, rb = _agent_request(ip, "POST", "/bind", {"busid": busid})
            if not okb or not (isinstance(rb, dict) and rb.get("ok")):
                _usbip_log(f"bind mislukt busid={busid}: {rb}")
                continue
        before = _snapshot_usb_disks()
        rc, out, err = _run_usbip_ctl(["attach", ip, busid], timeout=40)
        dev = None
        if rc == 0:
            dev = _wait_new_usb_dev(before)
        else:
            # Mogelijk al geattacht (eerdere run) → zoek de schijf op UUID onder de huidige schijven.
            _usbip_log(f"attach rc={rc} busid={busid}: {(err or out).strip()[:150]} — probeer bestaande schijf")
            if expected:
                dev = _find_usb_dev_by_uuid(expected)
        if not dev:
            _rollback_attach(ip, busid)
            continue
        found = _device_partition_uuids(dev)
        match = bool(expected & found) if expected else (len(candidates) == 1)
        if not match:
            _rollback_attach(ip, busid)
            if not expected:
                return jsonify({
                    "status": "ambiguous", "label": label, "host": name,
                    "message": f"Meerdere schijven aangesloten op {name} en {label} heeft nog geen bekende UUID. Sluit alleen {label} aan, of koppel handmatig via Beheer."
                })
            continue

        # Match — mount read-only via bestaande flow
        canonical = _canonicalize_archive_label(label) or label
        result = subprocess.run(
            [str(PROJECT_DIR / 'mount_readonly.sh'), dev, canonical],
            capture_output=True, text=True, timeout=40)
        if result.returncode != 0:
            clean = re.sub(r'\033\[[0-9;]*m', '', result.stdout + result.stderr)
            _rollback_attach(ip, busid)
            return jsonify({"status": "error", "label": label,
                            "message": f"Koppelen lukte, maar mounten mislukte: {clean[-200:]}"})

        # Registreer voor nette detach bij uitwerpen
        port = _find_port_for(ip, busid)
        entries = [e for e in _remote_state_read()
                   if not (e.get("host") == ip and e.get("busid") == busid)]
        entries.append({"port": port, "host": ip, "host_name": name, "busid": busid,
                        "dev": dev, "label": label, "attached_at": datetime.now().isoformat()})
        _remote_state_write(entries)
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("UPDATE files SET availability_status='online' WHERE archive_label=?", (label,))
            conn.commit(); conn.close()
        except Exception:
            pass
        _usbip_log(f"ensure-disk OK: {label} via {ip} busid={busid} dev={dev} port={port}")
        return jsonify({"status": "coupled", "label": label, "device": dev, "host": name,
                        "message": f"{label} automatisch gekoppeld vanaf {name}."})

    return jsonify({
        "status": "wrong_disk", "label": label, "host": name,
        "message": f"De op {name} aangesloten schijf is niet {label}. Sluit de juiste schijf aan (sticker {label})."
    })


def _agent_read_file(ip, label, rel):
    """Lees een bestand via de agent op host `ip`. Retourneert (bytes, content_type, fout).

    Padverschil-fallback: de catalogus-paden zijn relatief t.o.v. de server-mount. Sommige
    schijven zijn daar onder een partitie-submap met het volume-label gemount (bv.
    'Elements/...'), terwijl de schijf op Windows/Linux op zijn volume-root staat. Daarom:
    probeer het pad zoals-is; bij 'niet gevonden' én een leidende '<volume-label>/'-prefix,
    probeer nogmaals zonder die prefix.
    """
    from urllib.parse import urlencode
    vol = _volume_label_for(label)
    candidates = [rel]
    if vol and rel.lower().startswith(vol.lower() + '/'):
        candidates.append(rel[len(vol) + 1:])
    last_status, last_reason = None, None
    for cand in candidates:
        q = urlencode({'volume': vol, 'path': cand})
        status, data, ctype, reason = _agent_get_raw(ip, f'/read-file?{q}')
        if status == 200 and data is not None:
            return data, ctype, None
        last_status, last_reason = status, reason
        if reason != 'not_found':
            break  # not_here / andere fout: prefix-fallback heeft geen zin
    if last_reason == 'not_here':
        return None, None, (f"schijf {label} (volume '{vol}') is NIET aangesloten op deze machine "
                            f"— sluit hem aan en probeer opnieuw (het bestand is niet beschadigd)")
    if last_reason == 'not_found':
        return None, None, f"bestand niet aangetroffen op schijf {label} (naam/pad gewijzigd?)"
    return None, None, f"niet leesbaar via de agent (status {last_status})"


def _resolve_file_bytes(label, rel, ip):
    """Haal de inhoud van een bestand op: de server-mount of lokaal via de agent.

    Retourneert (bytes, None) of (None, foutmelding).
    """
    full_path, _sr = _lookup_archived_file_path(label, rel)
    if full_path and full_path.exists() and full_path.is_file():
        try:
            return full_path.read_bytes(), None
        except Exception as e:
            return None, str(e)
    agent_ips = _agent_ips_for(ip)
    last_err = None
    for aip in agent_ips:
        data, _ctype, err = _agent_read_file(aip, label, rel)
        if data is not None:
            return data, None
        last_err = err
    if agent_ips:
        return None, last_err
    return None, (f"schijf {label} is niet gekoppeld en op deze machine draait geen agent "
                  f"— sluit de schijf aan of installeer de agent via /netwerk-usb")


def _disk_size_gib(disk):
    """Schijfgrootte in GiB uit een agent-usbdisk (Windows: sizeGB num; Linux: size str)."""
    v = disk.get('sizeGB')
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    s = str(disk.get('size') or '').strip()
    m = re.match(r'^([\d.,]+)\s*([KMGT]?)i?B?$', s, re.I)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(',', '.'))
    except ValueError:
        return None
    mult = {'': 1/1024/1024/1024, 'K': 1/1024/1024, 'M': 1/1024, 'G': 1, 'T': 1024}
    return num * mult.get(m.group(2).upper(), 1)


def _server_mounted_labels():
    """Archive-labels die nu op de server gemount zijn."""
    labels = set()
    try:
        if MOUNT_BASE.exists():
            for d in MOUNT_BASE.iterdir():
                if not d.is_dir():
                    continue
                if d.is_mount() or any(p.is_dir() and p.is_mount() for p in d.iterdir()):
                    labels.add(d.name)
    except Exception:
        pass
    return labels


@app.route('/api/available-labels')
def api_available_labels():
    """Welke archiefschijven zijn nu echt bereikbaar vanaf deze machine?

    = de server-gemount + schijven die lokaal op de machine van de gebruiker gemount zijn
    (via de agent). Alleen deze bestanden zijn direct te openen/downloaden.
    """
    labels = set(_server_mounted_labels())
    ip = request.remote_addr
    # Verzamel per lokaal gemount volume: uuid + (label, grootte) van alle relevante agents.
    # UUID is de betrouwbare sleutel (volume-labels zijn niet uniek, bv. twee 'Elements').
    local_uuids = set()
    local = []
    for aip in _agent_ips_for(ip):
        ok, disks = _agent_request(aip, 'GET', '/disks', timeout=6)
        if ok and isinstance(disks, dict):
            for d in disks.get('usbdisks', []):
                sz = _disk_size_gib(d)
                for v in d.get('volumes', []):
                    if not (v.get('letter') or v.get('mountpoint')):
                        continue
                    u = str(v.get('uuid') or '').strip().upper()
                    if u:
                        local_uuids.add(u)
                    if v.get('label'):
                        local.append((v['label'], sz))
    if local or local_uuids:
            try:
                conn = sqlite3.connect(str(DB_PATH), timeout=10)
                rows = conn.execute(
                    "SELECT archive_label, volume_label, size_bytes, filesystem_uuid "
                    "FROM physical_media").fetchall()
                conn.close()
                for al, vl, sb, fuuid in rows:
                    fu = str(fuuid or '').strip().upper()
                    # 1) Betrouwbaar: UUID-match.
                    if fu and fu in local_uuids:
                        labels.add(al)
                        continue
                    # 2) Fallback (agent zonder uuid): label (+grootte indien bekend).
                    if not vl:
                        continue
                    for (lbl, sz) in local:
                        if vl != lbl:
                            continue
                        cat_gib = (sb / (1024.0 ** 3)) if sb else None
                        if not local_uuids:  # alleen fallback als we helemaal geen uuids kregen
                            if sz is None or cat_gib is None or abs(cat_gib - sz) <= max(4.0, 0.06 * cat_gib):
                                labels.add(al)
            except Exception:
                pass
    return jsonify({'labels': sorted(labels)})


@app.route('/api/download-zip', methods=['POST'])
def api_download_zip():
    """Bundel geselecteerde bestanden in een ZIP (de server-mount of lokaal via agent).

    Body: {files: [{label, path}, ...]}. Handig voor binaries die niet inline te tonen zijn.
    """
    import io
    import zipfile
    data = request.get_json(silent=True) or {}
    files = data.get('files') or []
    if not files:
        return jsonify({'error': 'geen bestanden geselecteerd'}), 400
    ip = request.remote_addr
    MAX_TOTAL = 1024 * 1024 * 1024  # 1 GB veiligheidsgrens
    buf = io.BytesIO()
    total = 0
    added = 0
    errors = []
    used = set()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files[:500]:
            label = (f.get('label') or '').strip()
            rel = (f.get('path') or '').strip()
            if not label or not rel:
                continue
            content, err = _resolve_file_bytes(label, rel, ip)
            if content is None:
                errors.append(f'{label}/{rel}: {err}')
                continue
            total += len(content)
            if total > MAX_TOTAL:
                errors.append('totaal te groot — rest overgeslagen')
                break
            arc = f'{label}/{rel}'
            if arc in used:
                arc = f'{label}/{added}_{rel}'
            used.add(arc)
            zf.writestr(arc, content)
            added += 1
        if errors:
            zf.writestr('_niet-opgehaald.txt',
                        'Deze bestanden konden niet worden opgehaald:\n\n' + '\n'.join(errors))
    if added == 0:
        return jsonify({'error': 'Geen bestanden opgehaald', 'details': errors}), 404
    _usbip_log(f"download-zip: {added} bestand(en), {total} bytes, {len(errors)} fout(en) via {ip}")
    buf.seek(0)
    return Response(buf.read(), mimetype='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="archief-selectie.zip"'})


@app.route('/api/smart-serve')
def api_smart_serve():
    """Serveer een bestand met de meest transparante bron:
      1. Schijf op de server gemount -> direct serveren.
      2. Schijf hangt aan de machine van de gebruiker -> agent leest hem lokaal (schijf
         blijft in Windows/Linux, geen USB/IP-overname).
      3. Anders -> not_mounted JSON, zodat de UI de koppel-flow toont.
    """
    label = request.args.get('label', '').strip()
    rel = request.args.get('path', '').strip()
    if not label or not rel:
        return jsonify({'error': 'label en path zijn vereist', 'not_mounted': False}), 400

    # 1. Lokaal op de server?
    full_path, _sr = _lookup_archived_file_path(label, rel)
    if full_path and full_path.exists() and full_path.is_file():
        try:
            return send_file(str(full_path), as_attachment=False)
        except Exception as e:
            return jsonify({'error': f'Fout bij serveren: {e}', 'not_mounted': False}), 500

    # 2. Lokaal lezen via de agent op de machine van de gebruiker (of terugval op alle agents)
    ip = request.remote_addr
    for aip in _agent_ips_for(ip):
        data, ctype, err = _agent_read_file(aip, label, rel)
        if data is not None:
            _usbip_log(f"smart-serve lokaal gelezen: {label}/{rel} via {aip}")
            return Response(data, content_type=(ctype or 'application/octet-stream'))
        _usbip_log(f"smart-serve lokaal lezen mislukt {label}/{rel} via {aip}: {err}")

    # 3. Niet beschikbaar -> UI toont koppel-flow
    return jsonify({
        'error': f'Bestand niet beschikbaar - schijf {label} is niet gekoppeld en niet lokaal leesbaar.',
        'not_mounted': True, 'label': label
    }), 404


@app.route('/api/remote/file-available')
def api_file_available():
    """Goedkope check: is dit bestand nu te openen (de server-mount of lokaal via agent)?

    Retourneert {available, source: server|local|none, host}. Wordt gebruikt voor
    auto-detect: de UI polt hierop en opent het bestand zodra het kan.
    """
    label = request.args.get('label', '').strip()
    rel = request.args.get('path', '').strip()
    full_path, _sr = _lookup_archived_file_path(label, rel)
    if full_path and full_path.exists() and full_path.is_file():
        return jsonify({'available': True, 'source': 'server'})
    ip = request.remote_addr
    name = _host_display_name(ip)
    vol = _volume_label_for(label)
    for aip in _agent_ips_for(ip):
        ok, disks = _agent_request(aip, 'GET', '/disks', timeout=6)
        if ok and isinstance(disks, dict):
            for d in disks.get('usbdisks', []):
                for v in d.get('volumes', []):
                    if v.get('label') == vol and (v.get('letter') or v.get('mountpoint')):
                        return jsonify({'available': True, 'source': 'local', 'host': name})
    return jsonify({'available': False, 'host': name})


@app.route('/api/scan/start', methods=['POST'])
def api_scan_start():
    """Start metadata scan in achtergrond."""
    data = request.get_json()
    label = data.get('label')
    if not label:
        return jsonify({'success': False, 'message': 'Label is verplicht'})
    running = _find_progress_task_for_label(label, prefixes=('scan_', 'batch_'), statuses=('running',))
    if running:
        return jsonify({
            'success': True,
            'task_id': running['task_id'],
            'message': f'Er loopt al een scan voor {label}. Voortgang wordt hervat.',
            'already_running': True,
        })

    source_dir = _ensure_archive_label_mounted(label, str(MOUNT_BASE / label))
    if not source_dir:
        return jsonify({
            'success': False,
            'message': f'{label} is niet correct gekoppeld op het archief-pad. Koppel of herstel de schijf eerst.'
        })
    task_id = f"scan_{label}_{datetime.now().strftime('%H%M%S')}"
    t = threading.Thread(target=_run_scan, args=(label, source_dir, task_id), daemon=True)
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/index/start', methods=['POST'])
def api_index_start():
    """Start Recoll indexering in achtergrond."""
    data = request.get_json()
    label = data.get('label')
    if not label:
        return jsonify({'success': False, 'message': 'Label is verplicht'})
    running = _find_progress_task_for_label(label, prefixes=('index_', 'batch_'), statuses=('running',))
    if running:
        return jsonify({
            'success': True,
            'task_id': running['task_id'],
            'message': f'Er loopt al een indexering voor {label}. Voortgang wordt hervat.',
            'already_running': True,
        })

    source_dir = _ensure_archive_label_mounted(label, str(MOUNT_BASE / label))
    if not source_dir:
        return jsonify({
            'success': False,
            'message': f'{label} is niet correct gekoppeld op het archief-pad. Koppel of herstel de schijf eerst.'
        })
    task_id = f"index_{label}_{datetime.now().strftime('%H%M%S')}"
    t = threading.Thread(target=_run_index, args=(label, task_id), daemon=True)
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/ingest/batch', methods=['POST'])
def api_ingest_batch():
    """Batch ingest: meerdere schijven achter elkaar."""
    data = request.get_json()
    disks = data.get('disks', [])
    if not disks:
        return jsonify({'success': False, 'message': 'Geen schijven opgegeven'})
    for disk in disks:
        label = (disk.get('label') or '').strip()
        if not label:
            continue
        running = _find_progress_task_for_label(label, prefixes=('scan_', 'index_', 'batch_'), statuses=('running',))
        if running:
            return jsonify({
                'success': False,
                'message': f'Er loopt al een taak voor {label}. Wacht tot die klaar is of hervat die taak.',
                'task_id': running['task_id'],
            })
    task_id = f"batch_{datetime.now().strftime('%H%M%S')}"
    t = threading.Thread(target=_run_batch_ingest, args=(disks, task_id), daemon=True)
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/progress/<task_id>')
def api_progress(task_id):
    """Haal voortgang op voor een achtergrond-taak."""
    if not PROGRESS_FILE.exists():
        return jsonify(None)
    try:
        with _progress_lock:
            progress = json.loads(PROGRESS_FILE.read_text())
        return jsonify(progress.get(task_id))
    except (json.JSONDecodeError, OSError):
        return jsonify(None)


@app.route('/api/progress/active')
def api_progress_active():
    """Vind een eventueel lopende of recent onderbroken taak."""
    if not PROGRESS_FILE.exists():
        return jsonify(None)
    try:
        with _progress_lock:
            progress = json.loads(PROGRESS_FILE.read_text())
        # Zoek actieve taak: running heeft prioriteit boven interrupted
        running = [(k, v) for k, v in progress.items() if v.get('status') == 'running']
        if running:
            running.sort(key=lambda x: x[1].get('updated', ''), reverse=True)
            task_id, info = running[0]
            info['task_id'] = task_id
            return jsonify(info)
        # Geen running? Toon meest recente interrupted (als < 1 uur oud)
        interrupted = [(k, v) for k, v in progress.items() if v.get('status') == 'interrupted']
        if interrupted:
            interrupted.sort(key=lambda x: x[1].get('updated', ''), reverse=True)
            task_id, info = interrupted[0]
            # Alleen tonen als recent (< 1 uur)
            try:
                updated = datetime.fromisoformat(info.get('updated', ''))
                if (datetime.now() - updated).total_seconds() < 3600:
                    info['task_id'] = task_id
                    return jsonify(info)
            except (ValueError, TypeError):
                pass
    except (json.JSONDecodeError, OSError):
        pass
    return jsonify(None)


@app.route('/api/media')
def api_media():
    """Lijst van alle geregistreerde media met scan-count."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    media = conn.execute("""
        SELECT pm.*,
            COUNT(DISTINCT f.file_id) as file_count,
            COUNT(DISTINCT s.scan_id) as scan_count
        FROM physical_media pm
        LEFT JOIN files f ON f.media_id = pm.media_id
        LEFT JOIN scans s ON s.media_id = pm.media_id
        GROUP BY pm.media_id
        ORDER BY pm.archive_label
    """).fetchall()
    conn.close()
    return jsonify({'media': [dict(m) for m in media]})


@app.route('/api/media/info')
def api_media_info():
    """Detail-info voor een enkel medium."""
    label = request.args.get('label', '')
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT pm.*, COUNT(f.file_id) as file_count
        FROM physical_media pm
        LEFT JOIN files f ON f.media_id = pm.media_id
        WHERE pm.archive_label = ?
        GROUP BY pm.media_id
    """, (label,)).fetchone()
    conn.close()
    if row:
        return jsonify(dict(row))
    return jsonify({})


@app.route('/api/media/confirm-sticker', methods=['POST'])
def api_confirm_sticker():
    data = request.get_json()
    label = data.get('label')
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE physical_media SET sticker_confirmed=1, sticker_confirmed_at=? WHERE archive_label=?",
                 (datetime.now().isoformat(), label))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/check-disk-quick', methods=['POST'])
def api_check_disk_quick():
    """Snelle archief-check: vergelijk alleen bestandsaantallen (geen walk, instant).

    Telt bestanden op schijf via os.scandir (een niveau diep is voldoende voor de check
    of de schijf uberhaupt gemount is en bestandsinhoud bevat).
    """
    data = request.get_json()
    label = data.get('label', '').strip()
    if not label:
        return jsonify({'success': False, 'message': 'Geen label opgegeven'})

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    roots = conn.execute(
        "SELECT DISTINCT source_root FROM files WHERE archive_label=?", (label,)).fetchall()
    db_count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE archive_label=?", (label,)).fetchone()[0]
    conn.close()

    if not roots:
        return jsonify({'success': False, 'message': f'Geen bestanden in DB voor {label}'})

    source_roots = [r[0] for r in roots]
    missing = [r for r in source_roots if not Path(r).exists()]
    if missing:
        return jsonify({'success': False,
            'message': 'Schijf niet gemount. Verwachte paden ontbreken:\n' + '\n'.join(f'  - {r}' for r in missing)})

    # Snelle check: tel bestanden op schijf met os.walk maar stop bij 2x DB-aantal of na 30s
    import signal
    disk_count = 0
    start = time.time()
    try:
        for src_root in source_roots:
            for dirpath, dirnames, filenames in os.walk(src_root):
                disk_count += len(filenames)
                if time.time() - start > 25:  # veilige limiet
                    break
    except Exception:
        pass

    elapsed = round(time.time() - start, 1)
    diff = disk_count - db_count
    if elapsed >= 25:
        verdict = 'timeout'
        verdict_msg = f'Tel-limiet bereikt na {elapsed}s — schijf heeft veel bestanden. Gebruik volledige check.'
    elif abs(diff) == 0:
        verdict = 'ok'
        verdict_msg = f'Aantallen kloppen: {db_count:,} bestanden in DB en op schijf'
    elif diff > 0:
        verdict = 'onvolledig'
        verdict_msg = f'{diff:,} bestanden op schijf maar NIET in DB (scan miste bestanden)'
    else:
        verdict = 'waarschuwing'
        verdict_msg = f'{abs(diff):,} bestanden in DB maar niet meer op schijf (verwijderd of andere mount?)'

    return jsonify({
        'success': True, 'label': label, 'source_roots': source_roots,
        'db_count': db_count, 'disk_count': disk_count,
        'diff': diff, 'elapsed': elapsed,
        'verdict': verdict, 'verdict_msg': verdict_msg,
    })


def _write_check_log(label, lines):
    """Schrijf check-logbestand (zelfde conventie als scan-logs)."""
    log_file = LOG_DIR / f"check_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file.write_text('\n'.join(lines), encoding='utf-8')
    return str(log_file)


def _run_check_disk_full(label, task_id):
    """Achtergrond-taak: volledige bestandsvergelijking (zie api_check_disk voor logica)."""
    start_ts = datetime.now()
    log_lines = [
        f"=== Archief-check gestart: {label} ===",
        f"Start: {start_ts.isoformat()}",
        f"Taak-ID: {task_id}",
        "",
    ]

    _update_progress(task_id, 'running', f'Check {label}: bestanden ophalen uit DB...', 2,
                     {'fase': 'db', 'label': label})
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    roots = conn.execute(
        "SELECT DISTINCT source_root FROM files WHERE archive_label=?", (label,)).fetchall()
    rows = conn.execute(
        "SELECT source_root, relative_path, size_bytes FROM files WHERE archive_label=?",
        (label,)).fetchall()
    conn.close()

    source_roots = [r[0] for r in roots]
    db_files = {(src, rel): sz for src, rel, sz in rows}
    log_lines.append(f"Bronmappen: {', '.join(source_roots)}")
    log_lines.append(f"Bestanden in DB: {len(db_files):,}")
    log_lines.append("")

    missing = [r for r in source_roots if not Path(r).exists()]
    if missing:
        log_lines += [f"FOUT: schijf niet gemount — ontbrekend pad: {r}" for r in missing]
        _write_check_log(label, log_lines)
        _update_progress(task_id, 'failed',
            'Schijf niet gemount: ' + ', '.join(missing), 0, {'label': label})
        return

    _update_progress(task_id, 'running',
        f'Check {label}: schijf walken ({len(db_files):,} bestanden in DB)...', 10,
        {'fase': 'walk', 'label': label, 'db_files': len(db_files)})

    disk_files = {}
    walked = 0
    for src_root in source_roots:
        for dirpath, dirnames, filenames in os.walk(str(Path(src_root))):
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    rel = str(fp.relative_to(Path(src_root)))
                    disk_files[(src_root, rel)] = fp.stat().st_size
                except (OSError, ValueError):
                    pass
            walked += len(filenames)
            if walked % 10000 == 0:
                pct = min(10 + int((walked / max(len(db_files), 1)) * 80), 90)
                _update_progress(task_id, 'running',
                    f'Check {label}: {walked:,} bestanden gelopen...', pct,
                    {'fase': 'walk', 'label': label, 'walked': walked})

    disk_set, db_set = set(disk_files), set(db_files)
    alleen_schijf = disk_set - db_set
    alleen_db = db_set - disk_set
    in_beide = disk_set & db_set
    size_mismatch = [(k[1], disk_files[k], db_files[k]) for k in in_beide if disk_files[k] != db_files[k]]

    if alleen_schijf or size_mismatch:
        verdict, verdict_msg = 'onvolledig', f'{len(alleen_schijf):,} niet gearchiveerd, {len(size_mismatch)} grootteverschillen'
    elif alleen_db:
        verdict, verdict_msg = 'waarschuwing', f'Compleet, maar {len(alleen_db):,} bestanden in DB niet meer op schijf'
    else:
        verdict, verdict_msg = 'ok', f'Volledig geverifieerd — alle {len(in_beide):,} bestanden kloppen'

    eind_ts = datetime.now()
    duur = round((eind_ts - start_ts).total_seconds())
    log_lines += [
        f"Bestanden op schijf: {len(disk_files):,}",
        f"Bestanden in DB:     {len(db_files):,}",
        f"Overeenkomen:        {len(in_beide):,}",
        f"Alleen op schijf:    {len(alleen_schijf):,}",
        f"Alleen in DB:        {len(alleen_db):,}",
        f"Grootteverschillen:  {len(size_mismatch):,}",
        "",
        f"Verdict: {verdict.upper()} — {verdict_msg}",
        f"Einde: {eind_ts.isoformat()} (duur: {duur}s)",
        "",
    ]
    if alleen_schijf:
        log_lines.append("Voorbeelden alleen op schijf (max 20):")
        log_lines += [f"  + {k[1]}" for k in sorted(alleen_schijf)[:20]]
        log_lines.append("")
    if alleen_db:
        log_lines.append("Voorbeelden alleen in DB (max 20):")
        log_lines += [f"  - {k[1]}" for k in sorted(alleen_db)[:20]]
        log_lines.append("")
    if size_mismatch:
        log_lines.append("Grootteverschillen (max 20):")
        log_lines += [f"  ~ {rel}: schijf={dsz} DB={dbsz}" for rel, dsz, dbsz in size_mismatch[:20]]
        log_lines.append("")

    log_file = _write_check_log(label, log_lines)

    _update_progress(task_id, 'completed', f'{label}: {verdict_msg}', 100, {
        'fase': 'klaar', 'label': label,
        'verdict': verdict, 'verdict_msg': verdict_msg,
        'disk_files': len(disk_files), 'db_files': len(db_files),
        'matched': len(in_beide) - len(size_mismatch),
        'alleen_schijf': len(alleen_schijf), 'alleen_db': len(alleen_db),
        'size_mismatch': len(size_mismatch),
        'voorbeelden_alleen_schijf': [k[1] for k in sorted(alleen_schijf)][:10],
        'voorbeelden_alleen_db': [k[1] for k in sorted(alleen_db)][:10],
        'source_roots': source_roots,
        'log_file': log_file,
        # #215: lijst van ontbrekende paden voor SQL-query (max 500)
        'alleen_db_paden': [k[1] for k in sorted(alleen_db)][:500],
    })


@app.route('/api/check-disk-start', methods=['POST'])
def api_check_disk_start():
    """Start volledige archief-check als achtergrondtaak (voor grote schijven)."""
    data = request.get_json()
    label = data.get('label', '').strip()
    if not label:
        return jsonify({'success': False, 'message': 'Geen label opgegeven'})
    task_id = f"check_{label}_{datetime.now().strftime('%H%M%S')}"
    t = threading.Thread(target=_run_check_disk_full, args=(label, task_id), daemon=True)
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/check-disk', methods=['POST'])
def api_check_disk():
    """Vergelijk aangesloten schijf met archief in database.

    Gebruikt source_root uit de DB om te bepalen waar bestanden verwacht worden.
    Ondersteunt meerdere source_roots per label (bijv. meerdere partities).

    Controleert:
    - Hoeveel bestanden op schijf vs in DB
    - Welke bestanden op schijf staan maar niet in DB (gemist)
    - Welke bestanden in DB staan maar niet op schijf (verdwenen)
    - Bestandsgroottes matchen
    """
    data = request.get_json()
    label = data.get('label', '').strip()
    if not label:
        return jsonify({'success': False, 'message': 'Geen label opgegeven'})

    # Haal source_roots en bestanden uit DB voor dit label
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    roots = conn.execute(
        "SELECT DISTINCT source_root FROM files WHERE archive_label = ?",
        (label,)).fetchall()
    if not roots:
        conn.close()
        return jsonify({'success': False,
            'message': f'Geen bestanden in database voor label {label}'})

    source_roots = [r[0] for r in roots]

    # Controleer welke source_roots bestaan (gemount zijn)
    def _root_accessible(path_str):
        """Check of een source_root bestaat en bestanden bevat."""
        p = Path(path_str)
        if not p.exists():
            return False
        try:
            return any(True for _ in p.iterdir())
        except (PermissionError, OSError):
            return False
    missing_roots = [r for r in source_roots if not _root_accessible(r)]
    if missing_roots:
        conn.close()
        # Geef duidelijke foutmelding met verwachte paden
        pad_lijst = '\n'.join(f'  - {r}' for r in missing_roots)
        return jsonify({'success': False,
            'message': f'Schijf niet (volledig) gemount. Verwachte paden niet beschikbaar:\n{pad_lijst}\n\nMount de schijf eerst via Beheer > Aansluiten, of controleer of de juiste partitie gemount is.'})

    # Haal alle bestanden uit DB voor dit label
    rows = conn.execute(
        "SELECT source_root, relative_path, size_bytes FROM files WHERE archive_label = ?",
        (label,)).fetchall()
    conn.close()

    db_files = {}
    for src_root, rel_path, size in rows:
        # Sla op als (source_root, relative_path) → size
        db_files[(src_root, rel_path)] = size

    # Verzamel bestanden op schijf per source_root
    disk_files = {}
    for src_root in source_roots:
        root_path = Path(src_root)
        for dirpath, dirnames, filenames in os.walk(str(root_path)):
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    rel = str(fp.relative_to(root_path))
                    disk_files[(src_root, rel)] = fp.stat().st_size
                except (OSError, ValueError):
                    pass

    # Vergelijk
    disk_set = set(disk_files.keys())
    db_set = set(db_files.keys())

    alleen_schijf = disk_set - db_set  # Op schijf maar niet in DB
    alleen_db = db_set - disk_set       # In DB maar niet op schijf
    in_beide = disk_set & db_set

    # Size mismatches
    size_mismatch = []
    for key in in_beide:
        if disk_files[key] != db_files.get(key, 0):
            size_mismatch.append({
                'path': key[1],  # relative_path
                'source_root': key[0],
                'disk_size': disk_files[key],
                'db_size': db_files.get(key, 0)
            })

    result = {
        'success': True,
        'label': label,
        'source_roots': source_roots,
        'disk_files': len(disk_files),
        'db_files': len(db_files),
        'matched': len(in_beide) - len(size_mismatch),
        'alleen_schijf': len(alleen_schijf),
        'alleen_db': len(alleen_db),
        'size_mismatch': len(size_mismatch),
        'voorbeelden_alleen_schijf': [k[1] for k in sorted(alleen_schijf)][:10],
        'voorbeelden_alleen_db': [k[1] for k in sorted(alleen_db)][:10],
        'voorbeelden_size_mismatch': size_mismatch[:10],
    }

    # Beoordeling
    if alleen_schijf or size_mismatch:
        result['verdict'] = 'onvolledig'
        parts = []
        if alleen_schijf:
            parts.append(f'{len(alleen_schijf)} bestanden niet gearchiveerd')
        if size_mismatch:
            parts.append(f'{len(size_mismatch)} grootteverschillen')
        result['verdict_msg'] = ', '.join(parts)
    elif alleen_db:
        result['verdict'] = 'waarschuwing'
        result['verdict_msg'] = f'Archief compleet, maar {len(alleen_db)} bestanden in DB niet meer op schijf'
    else:
        result['verdict'] = 'ok'
        result['verdict_msg'] = f'Archief compleet — alle {len(in_beide)} bestanden geverifieerd'

    return jsonify(result)


@app.route('/api/sql', methods=['POST'])
def api_sql():
    """Voer een SQL query uit (alleen SELECT)."""
    data = request.get_json()
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'error': 'Geen query opgegeven'})
    # Veiligheidscheck: alleen SELECT
    first_word = query.split()[0].upper() if query.split() else ''
    if first_word not in ('SELECT', 'WITH', 'EXPLAIN'):
        return jsonify({'error': 'Alleen SELECT queries zijn toegestaan'})
    # Blokkeer destructieve woorden
    upper = query.upper()
    for forbidden in ('DROP', 'DELETE', 'INSERT', 'UPDATE', 'ALTER', 'CREATE', 'ATTACH'):
        if forbidden in upper:
            return jsonify({'error': f'{forbidden} is niet toegestaan'})
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.execute(query)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(500)
        conn.close()
        return jsonify({'columns': columns, 'rows': rows})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/scans')
def api_scans():
    """Overzicht van alle scans met status (#97)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""SELECT scan_id, archive_label, source_root,
        start_time, end_time, status, number_of_files, files_ok, files_error
        FROM scans ORDER BY scan_id DESC LIMIT 50""").fetchall()
    conn.close()
    progress_state = _read_progress_state()
    running_by_label = {}
    for task_id, info in progress_state.items():
        if info.get('status') != 'running' or not str(task_id).startswith('scan_'):
            continue
        details = info.get('details') or {}
        label = details.get('label')
        if not label:
            continue
        previous = running_by_label.get(label)
        if not previous or info.get('updated', '') > previous.get('updated', ''):
            running_by_label[label] = info

    scans = []
    for row in rows:
        item = dict(row)
        item.update(_describe_scan_source(item.get('archive_label'), item.get('source_root')))
        display_files_ok = item.get('files_ok') or 0
        display_files_error = item.get('files_error') or 0
        if item.get('status') == 'running':
            live = running_by_label.get(item.get('archive_label'))
            if live:
                details = live.get('details') or {}
                display_files_ok = details.get('files_ok', display_files_ok) or 0
                display_files_error = details.get('files_error', display_files_error) or 0
                item['live_phase'] = details.get('fase')
                item['live_total_files'] = details.get('total_files')
        item['display_files_ok'] = display_files_ok
        item['display_files_error'] = display_files_error
        scans.append(item)
    return jsonify({'scans': scans})


@app.route('/api/logs')
def api_logs():
    """Lijst beschikbare logbestanden."""
    logs = []
    log_dirs = [LOG_DIR, INDEX_BASE]
    for log_dir in log_dirs:
        if not log_dir.exists():
            continue
        for f in sorted(log_dir.rglob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                stat = f.stat()
                size = stat.st_size
                if size > 1024*1024:
                    size_str = f"{size/(1024*1024):.1f} MB"
                elif size > 1024:
                    size_str = f"{size/1024:.1f} KB"
                else:
                    size_str = f"{size} B"
                logs.append({
                    'name': f.name,
                    'path': str(f),
                    'size': size_str,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                })
            except OSError:
                pass
    return jsonify({'logs': logs[:50]})


@app.route('/api/logs/view')
def api_logs_view():
    """Lees inhoud van een logbestand."""
    path = request.args.get('path', '')
    if not path:
        return jsonify({'content': ''})
    # Veiligheidscheck: alleen bestanden in project dir
    try:
        resolved = Path(path).resolve()
        if not str(resolved).startswith(str(PROJECT_DIR.resolve())):
            return jsonify({'content': 'Toegang geweigerd: buiten project directory'})
        if resolved.suffix != '.log':
            return jsonify({'content': 'Alleen .log bestanden'})
        content = resolved.read_text(errors='replace')
        # Beperk tot laatste 500 regels
        lines = content.split('\n')
        if len(lines) > 500:
            content = f"... ({len(lines) - 500} regels overgeslagen) ...\n" + '\n'.join(lines[-500:])
        return jsonify({'content': content})
    except Exception as e:
        return jsonify({'content': f'Fout: {e}'})


@app.route('/api/service/status')
def api_service_status():
    """Status van de systemd service opvragen."""
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'status', 'archive-search-workbench'],
            capture_output=True, text=True, timeout=10)
        clean = re.sub(r'\033\[[0-9;]*m', '', result.stdout + result.stderr)
        active = 'active (running)' in clean.lower()
        return jsonify({'active': active, 'output': clean[:2000]})
    except Exception as e:
        return jsonify({'active': None, 'output': str(e)})


@app.route('/api/service/restart', methods=['POST'])
def api_service_restart():
    """Herstart de systemd service. Let op: dit herstart de huidige applicatie!"""
    try:
        _cleanup_stale_tasks()
        # Stuur het restart-commando in een apart proces zodat het antwoord
        # nog verstuurd kan worden voordat de service stopt
        subprocess.Popen(
            ['sudo', 'systemctl', 'restart', 'archive-search-workbench'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'success': True, 'message': 'Herstart geïnitieerd — interface komt zo terug'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/service/stop', methods=['POST'])
def api_service_stop():
    """Stop de systemd service. Let op: de web-interface wordt onbereikbaar!"""
    try:
        _cleanup_stale_tasks()
        subprocess.Popen(
            ['sudo', 'systemctl', 'stop', 'archive-search-workbench'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'success': True, 'message': 'Service wordt gestopt'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/service/start', methods=['POST'])
def api_service_start():
    """Start de systemd service."""
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'start', 'archive-search-workbench'],
            capture_output=True, text=True, timeout=10)
        clean = re.sub(r'\033\[[0-9;]*m', '', result.stdout + result.stderr)
        success = result.returncode == 0
        return jsonify({'success': success, 'active': success,
                        'output': clean[:1000] if clean.strip() else 'Service gestart'})
    except Exception as e:
        return jsonify({'success': False, 'output': str(e)})


@app.route('/api/donate_button')
def api_donate_button():
    """Geeft primaire donate-route terug uit donate-routes.v1.json, of null als niet geconfigureerd."""
    btn = _resolve_donate_button()
    return jsonify({'donate_button': btn})


@app.route('/api/admin/migrate-paths', methods=['POST'])
def api_admin_migrate_paths():
    """#211 Pad-migratie: verwijder foutief prefix uit relative_path en full_path.

    Body: { "label": "ARCHIVE-DISK-004", "strip_prefix": "Elements/", "confirm": true }
    Vereist confirm=true als expliciete HITL-bevestiging.
    """
    data = request.get_json()
    label = (data.get('label') or '').strip()
    strip_prefix = (data.get('strip_prefix') or '').strip()
    confirm = data.get('confirm', False)

    if not label or not strip_prefix:
        return jsonify({'success': False, 'error': 'label en strip_prefix zijn vereist'}), 400
    if not confirm:
        return jsonify({'success': False, 'error': 'confirm=true vereist (HITL)'}), 400
    if '/' not in strip_prefix or strip_prefix.startswith('/'):
        return jsonify({'success': False, 'error': 'strip_prefix moet relatief zijn (bijv. "Elements/")'}), 400

    prefix_len = len(strip_prefix)
    like_pattern = strip_prefix + '%'

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")

        # Tel eerst hoeveel rijen geraakt worden
        count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE archive_label=? AND relative_path LIKE ?",
            (label, like_pattern)).fetchone()[0]

        if count == 0:
            conn.close()
            return jsonify({'success': True, 'migrated': 0, 'message': 'Geen rijen met dit prefix gevonden.'})

        # Voer de migratie uit
        conn.execute("""
            UPDATE files
            SET relative_path = substr(relative_path, ?),
                full_path      = source_root || '/' || substr(relative_path, ?),
                parent_dir     = replace(parent_dir, source_root || '/' || ?, source_root)
            WHERE archive_label = ? AND relative_path LIKE ?
        """, (prefix_len + 1, prefix_len + 1, strip_prefix.rstrip('/'), label, like_pattern))
        conn.commit()
        migrated = conn.total_changes
        conn.close()
        return jsonify({'success': True, 'migrated': migrated,
                        'message': f'{migrated:,} paden bijgewerkt voor {label} (prefix "{strip_prefix}" verwijderd)'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


_startup_autoresume()


if __name__ == '__main__':
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Archive Search Workbench v2 — Web Interface")
    print(f"Database: {DB_PATH}")
    print(f"URL: http://0.0.0.0:5059")
    app.run(host='0.0.0.0', port=5059, debug=False)
