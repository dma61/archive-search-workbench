#!/usr/bin/env python3
"""Metadata scanner voor Archive Search Workbench.

Scant recursief een bronmap, registreert bestanden in SQLite,
extraheert metadata per bestandstype, met checkpoint/resume ondersteuning.
"""

import os
import sys
import sqlite3
import yaml
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

# Project imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_metadata import (
    extract_pdf_metadata, extract_office_metadata, extract_image_metadata,
    determine_original_content_date, get_extension_group, human_size
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "config.yaml"
DB_PATH = PROJECT_DIR / "data" / "archive_catalog.db"
LOCK_FILE = PROJECT_DIR / "data" / "workbench.lock"

# Logging
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"scan_{datetime.now().strftime('%Y%m%d-%H%M')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(str(log_file)),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    """Laad config.yaml."""
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def acquire_lock() -> bool:
    """Probeer lock te verkrijgen. Retourneert False als al gelocked."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # Check of PID nog draait
            os.kill(pid, 0)
            return False  # Proces draait nog
        except (ProcessLookupError, ValueError):
            # PID draait niet meer, stale lock
            log.warning(f"Stale lock gevonden (PID uit lock bestaat niet), wordt overschreven")
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Geef lock vrij."""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def get_media_id(conn: sqlite3.Connection, archive_label: str) -> Optional[int]:
    """Haal media_id op voor een archive_label."""
    row = conn.execute(
        "SELECT media_id FROM physical_media WHERE archive_label = ?",
        (archive_label,)
    ).fetchone()
    return row[0] if row else None


def register_medium(conn: sqlite3.Connection, archive_label: str,
                    source_root: str, media_type: str = "unknown") -> int:
    """Registreer een nieuw fysiek medium als het nog niet bestaat."""
    existing = get_media_id(conn, archive_label)
    if existing:
        conn.execute(
            "UPDATE physical_media SET last_seen = ? WHERE media_id = ?",
            (datetime.now().isoformat(), existing)
        )
        conn.commit()
        return existing

    now = datetime.now().isoformat()

    # Probeer UUID en label te lezen via blkid (als het een mountpoint is)
    fs_uuid = None
    vol_label = None
    try:
        import subprocess
        # Zoek welk device gemount is op source_root
        result = subprocess.run(
            ['findmnt', '-n', '-o', 'SOURCE', source_root],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            device = result.stdout.strip()
            blkid_result = subprocess.run(
                ['sudo', 'blkid', '-s', 'UUID', '-s', 'LABEL', '-o', 'value', device],
                capture_output=True, text=True, timeout=5
            )
            if blkid_result.returncode == 0:
                lines = blkid_result.stdout.strip().split('\n')
                if len(lines) >= 1:
                    fs_uuid = lines[0] if lines[0] else None
                if len(lines) >= 2:
                    vol_label = lines[1] if lines[1] else None
    except Exception:
        pass

    conn.execute("""
        INSERT INTO physical_media
            (archive_label, media_type, first_seen, last_seen,
             filesystem_uuid, volume_label)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (archive_label, media_type, now, now, fs_uuid, vol_label))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def create_scan(conn: sqlite3.Connection, media_id: int,
                archive_label: str, source_root: str) -> int:
    """Maak een nieuw scan-record aan."""
    conn.execute("""
        INSERT INTO scans (media_id, archive_label, source_root, start_time, status)
        VALUES (?, ?, ?, ?, 'running')
    """, (media_id, archive_label, source_root, datetime.now().isoformat()))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_filesystem_dates(filepath: Path) -> dict:
    """Haal filesystem datums op voor een bestand."""
    result = {}
    try:
        stat = filepath.stat()
        result['filesystem_modified_time'] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        result['filesystem_accessed_time'] = datetime.fromtimestamp(stat.st_atime).isoformat()
        # st_ctime is change time op Linux, niet creation time
        # Probeer st_birthtime (niet altijd beschikbaar)
        if hasattr(stat, 'st_birthtime'):
            result['filesystem_created_time'] = datetime.fromtimestamp(stat.st_birthtime).isoformat()
        else:
            result['filesystem_created_time'] = datetime.fromtimestamp(stat.st_ctime).isoformat()
    except (OSError, OverflowError) as e:
        log.warning(f"Kan filesystem datums niet lezen voor {filepath}: {e}")
    return result


def extract_metadata_for_file(filepath: Path, extension: str) -> dict:
    """Extraheer metadata op basis van bestandstype."""
    ext = extension.lower()
    if ext == 'pdf':
        return extract_pdf_metadata(str(filepath))
    elif ext in ('docx',):
        return extract_office_metadata(str(filepath))
    elif ext in ('xlsx', 'xlsm'):
        return extract_office_metadata(str(filepath))
    elif ext in ('jpg', 'jpeg', 'png', 'gif', 'tiff', 'bmp', 'heic', 'webp'):
        return extract_image_metadata(str(filepath))
    return {}


def scan_directory(source_root: str, archive_label: str, config: dict):
    """Hoofd-scanfunctie. Scant een bronmap en slaat alles op in SQLite."""

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    exclude_dirs = set(config.get('exclude_dirs', []))
    follow_symlinks = config.get('follow_symlinks', False)

    # Bouw set van alle relevante extensies
    all_extensions = set()
    for group_exts in config.get('include_extensions', {}).values():
        all_extensions.update(ext.lower() for ext in group_exts)

    # Registreer medium
    media_id = register_medium(conn, archive_label, source_root)
    log.info(f"Medium geregistreerd: {archive_label} (media_id={media_id})")

    # Maak scan-record
    scan_id = create_scan(conn, media_id, archive_label, source_root)
    log.info(f"Scan gestart: scan_id={scan_id}, bron={source_root}")

    root_path = Path(source_root)
    files_ok = 0
    files_error = 0
    total_bytes = 0
    batch = 0
    error_messages = []
    seen_paths = set()  # Symlink loop protectie

    try:
        for dirpath, dirnames, filenames in os.walk(source_root, followlinks=follow_symlinks):
            current = Path(dirpath)

            # Symlink loop protectie
            real_path = current.resolve()
            if real_path in seen_paths:
                log.warning(f"Symlink loop gedetecteerd, overslaan: {dirpath}")
                dirnames.clear()
                continue
            seen_paths.add(real_path)

            # Exclude directories
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]

            for filename in filenames:
                filepath = current / filename

                # Encoding fallback voor bestandsnamen
                try:
                    _ = str(filepath)
                except UnicodeError:
                    log.warning(f"Bestandsnaam encoding probleem, overslaan: {dirpath}/{filename}")
                    files_error += 1
                    continue

                try:
                    # Basis bestandsinfo
                    ext = filepath.suffix.lstrip('.').lower() if filepath.suffix else ''
                    relative = str(filepath.relative_to(root_path))

                    # Stat info
                    try:
                        stat = filepath.stat()
                        size = stat.st_size
                    except OSError as e:
                        log.warning(f"Kan stat niet lezen: {filepath}: {e}")
                        files_error += 1
                        error_messages.append(f"{filepath}: {e}")
                        continue

                    total_bytes += size
                    ext_group = get_extension_group(ext)

                    # Filesystem datums
                    fs_dates = get_filesystem_dates(filepath)

                    # Metadata extractie (alleen voor bekende extensies)
                    file_meta = {}
                    if ext in all_extensions:
                        try:
                            file_meta = extract_metadata_for_file(filepath, ext)
                        except Exception as e:
                            log.warning(f"Metadata extractie mislukt: {filepath}: {e}")
                            file_meta = {'error_message': str(e)}

                    # Combineer voor datum-bepaling
                    combined = {**fs_dates, **file_meta, 'filename': filename}
                    content_date, date_source, date_confidence = determine_original_content_date(combined)

                    # Insert in database
                    conn.execute("""
                        INSERT INTO files (
                            scan_id, media_id, archive_label, source_root,
                            full_path, relative_path, parent_dir, filename,
                            extension, extension_group, size_bytes, human_size,
                            filesystem_created_time, filesystem_modified_time,
                            filesystem_accessed_time,
                            document_created_time, document_modified_time,
                            original_content_date,
                            author, creator, last_modified_by, company,
                            title, subject, keywords, producer, application,
                            date_source, metadata_source, date_confidence,
                            availability_status, original_device_label,
                            original_mountpoint, last_seen_online,
                            readable, metadata_extracted,
                            error_message, scan_checkpoint_batch
                        ) VALUES (
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?,
                            ?, ?,
                            ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?, ?,
                            'online', ?,
                            ?, ?,
                            1, ?,
                            ?, ?
                        )
                    """, (
                        scan_id, media_id, archive_label, source_root,
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
                        date_source, file_meta.get('metadata_source'),
                        date_confidence,
                        archive_label,
                        source_root, datetime.now().isoformat(),
                        1 if file_meta and 'error_message' not in file_meta else 0,
                        file_meta.get('error_message'),
                        batch
                    ))
                    files_ok += 1

                except Exception as e:
                    log.error(f"Fout bij verwerken {filepath}: {e}")
                    files_error += 1
                    error_messages.append(f"{filepath}: {e}")

            # Checkpoint per directory
            batch += 1
            if batch % 10 == 0:
                conn.commit()
                conn.execute(
                    "UPDATE scans SET last_checkpoint_dir = ?, number_of_files = ?, total_bytes = ? WHERE scan_id = ?",
                    (str(current), files_ok + files_error, total_bytes, scan_id)
                )
                conn.commit()
                log.info(f"  Checkpoint: {files_ok} ok, {files_error} fouten, {human_size(total_bytes)}")

        # Scan voltooid
        conn.execute("""
            UPDATE scans SET
                end_time = ?, number_of_files = ?, total_bytes = ?,
                files_ok = ?, files_error = ?,
                status = 'completed',
                errors = ?
            WHERE scan_id = ?
        """, (
            datetime.now().isoformat(), files_ok + files_error, total_bytes,
            files_ok, files_error,
            '\n'.join(error_messages[:100]) if error_messages else None,
            scan_id
        ))
        conn.commit()

        log.info("=" * 60)
        log.info(f"Scan voltooid voor {archive_label}")
        log.info(f"  Bestanden OK: {files_ok}")
        log.info(f"  Bestanden met fouten: {files_error}")
        log.info(f"  Totale grootte: {human_size(total_bytes)}")
        if files_error > 0:
            log.warning(f"  Eerste 5 fouten:")
            for err_msg in error_messages[:5]:
                log.warning(f"    - {err_msg}")
        log.info("=" * 60)

    except KeyboardInterrupt:
        log.warning("Scan onderbroken door gebruiker")
        conn.execute(
            "UPDATE scans SET status = 'interrupted', end_time = ? WHERE scan_id = ?",
            (datetime.now().isoformat(), scan_id)
        )
        conn.commit()
    except Exception as e:
        log.error(f"Scan mislukt: {e}")
        conn.execute(
            "UPDATE scans SET status = 'failed', end_time = ?, errors = ? WHERE scan_id = ?",
            (datetime.now().isoformat(), str(e), scan_id)
        )
        conn.commit()
    finally:
        conn.close()

    return files_ok, files_error


def main():
    """Hoofdfunctie: laad config, scan alle roots."""
    if not acquire_lock():
        log.error("Een andere scan draait al (lock actief). Wacht tot die klaar is.")
        sys.exit(1)

    try:
        config = load_config()
        scan_roots = config.get('scan_roots', [])

        if not scan_roots:
            log.error("Geen scan_roots geconfigureerd in config.yaml")
            sys.exit(1)

        # Zoek welk medium er gemount is
        total_ok = 0
        total_err = 0

        for root in scan_roots:
            root_path = Path(root)
            if not root_path.exists():
                log.warning(f"Scan root bestaat niet: {root}")
                continue

            # Zoek submappen (elk is een archive label)
            subdirs = [d for d in root_path.iterdir() if d.is_dir()]
            if not subdirs:
                log.warning(f"Geen submappen gevonden in {root}")
                continue

            for subdir in subdirs:
                archive_label = subdir.name
                log.info(f"\n{'=' * 60}")
                log.info(f"Start scan: {archive_label}")
                log.info(f"Pad: {subdir}")
                log.info(f"{'=' * 60}")

                ok, err = scan_directory(str(subdir), archive_label, config)
                total_ok += ok
                total_err += err

        log.info(f"\nTotaal over alle media: {total_ok} ok, {total_err} fouten")

    finally:
        release_lock()


if __name__ == "__main__":
    main()
