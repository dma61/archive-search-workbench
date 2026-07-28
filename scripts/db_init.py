#!/usr/bin/env python3
"""Database initialisatie voor Archive Search Workbench.

Maakt het SQLite schema aan met versioning voor latere migraties.
Idempotent: veilig om meerdere keren te draaien.
"""

import sqlite3
import sys
from pathlib import Path
from datetime import datetime

SCHEMA_VERSION = 1

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "data" / "archive_catalog.db"


def get_schema_version(conn):
    """Haal huidige schema-versie op. Retourneert 0 als tabel niet bestaat."""
    try:
        row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def migrate_v1(conn):
    """Schema versie 1: initieel schema met alle tabellen."""

    conn.executescript("""

    CREATE TABLE IF NOT EXISTS schema_version (
        version     INTEGER PRIMARY KEY,
        applied_at  TEXT NOT NULL,
        description TEXT
    );

    CREATE TABLE IF NOT EXISTS physical_media (
        media_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        archive_label       TEXT UNIQUE NOT NULL,
        media_type          TEXT NOT NULL CHECK(media_type IN (
            'usb_hdd', 'usb_ssd', 'usb_flash', 'sd_card',
            'external_sata_usb', 'disk_image', 'nas_share', 'unknown'
        )),
        first_seen          TEXT NOT NULL,
        last_seen           TEXT NOT NULL,
        filesystem_uuid     TEXT,
        volume_label        TEXT,
        device_model        TEXT,
        device_serial       TEXT,
        size_bytes          INTEGER,
        filesystem_type     TEXT,
        smart_status        TEXT,
        sticker_confirmed   INTEGER DEFAULT 0,
        sticker_confirmed_at TEXT,
        notes               TEXT
    );

    CREATE TABLE IF NOT EXISTS scans (
        scan_id             INTEGER PRIMARY KEY AUTOINCREMENT,
        media_id            INTEGER NOT NULL REFERENCES physical_media(media_id),
        archive_label       TEXT NOT NULL,
        source_root         TEXT NOT NULL,
        start_time          TEXT NOT NULL,
        end_time            TEXT,
        number_of_files     INTEGER DEFAULT 0,
        total_bytes         INTEGER DEFAULT 0,
        last_checkpoint_dir TEXT,
        status              TEXT NOT NULL DEFAULT 'running' CHECK(status IN (
            'running', 'completed', 'failed', 'interrupted'
        )),
        files_ok            INTEGER DEFAULT 0,
        files_error         INTEGER DEFAULT 0,
        errors              TEXT
    );

    CREATE TABLE IF NOT EXISTS files (
        file_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id                 INTEGER NOT NULL REFERENCES scans(scan_id),
        media_id                INTEGER NOT NULL REFERENCES physical_media(media_id),
        archive_label           TEXT NOT NULL,
        source_root             TEXT NOT NULL,
        full_path               TEXT NOT NULL,
        relative_path           TEXT NOT NULL,
        parent_dir              TEXT,
        filename                TEXT NOT NULL,
        extension               TEXT,
        extension_group         TEXT,
        size_bytes              INTEGER,
        human_size              TEXT,

        -- Filesystem datums
        filesystem_created_time  TEXT,
        filesystem_modified_time TEXT,
        filesystem_accessed_time TEXT,

        -- Documentdatums
        document_created_time   TEXT,
        document_modified_time  TEXT,
        original_content_date   TEXT,

        -- Metadata
        author                  TEXT,
        creator                 TEXT,
        last_modified_by        TEXT,
        company                 TEXT,
        title                   TEXT,
        subject                 TEXT,
        keywords                TEXT,
        producer                TEXT,
        application             TEXT,

        -- Metadata oorsprong
        date_source             TEXT,
        metadata_source         TEXT,
        date_confidence         TEXT CHECK(date_confidence IN (
            'high', 'medium', 'low', 'suspect', NULL
        )),

        -- Archiefvelden
        inside_archive          INTEGER DEFAULT 0,
        archive_container       TEXT,
        archive_internal_path   TEXT,

        -- Beschikbaarheid
        availability_status     TEXT DEFAULT 'indexed_only' CHECK(availability_status IN (
            'indexed_only', 'cached_local', 'archive_offline',
            'online', 'missing'
        )),
        original_device_label   TEXT,
        original_mountpoint     TEXT,
        last_seen_online        TEXT,
        cached_local_path       TEXT,
        preview_available       INTEGER DEFAULT 0,

        -- Status
        readable                INTEGER DEFAULT 1,
        indexed_by_recoll       INTEGER DEFAULT 0,
        metadata_extracted      INTEGER DEFAULT 0,

        -- Fouten
        error_message           TEXT,

        -- Checkpoint
        scan_checkpoint_batch   INTEGER
    );

    -- Indexes voor snelle zoekopdrachten
    CREATE INDEX IF NOT EXISTS idx_files_filename ON files(filename);
    CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension);
    CREATE INDEX IF NOT EXISTS idx_files_original_content_date ON files(original_content_date);
    CREATE INDEX IF NOT EXISTS idx_files_author ON files(author);
    CREATE INDEX IF NOT EXISTS idx_files_title ON files(title);
    CREATE INDEX IF NOT EXISTS idx_files_source_root ON files(source_root);
    CREATE INDEX IF NOT EXISTS idx_files_archive_label ON files(archive_label);
    CREATE INDEX IF NOT EXISTS idx_files_media_id ON files(media_id);
    CREATE INDEX IF NOT EXISTS idx_files_extension_group ON files(extension_group);
    CREATE INDEX IF NOT EXISTS idx_files_parent_dir ON files(parent_dir);
    CREATE INDEX IF NOT EXISTS idx_files_inside_archive ON files(inside_archive);

    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
        (1, datetime.now().isoformat(), "Initieel schema: physical_media, scans, files")
    )


MIGRATIONS = {
    1: migrate_v1,
}


def init_database():
    """Initialiseer of migreer de database. Retourneert (succes, bericht)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    current = get_schema_version(conn)
    target = SCHEMA_VERSION

    if current == target:
        conn.close()
        return True, f"Database al op versie {current}, geen migratie nodig"

    if current > target:
        conn.close()
        return False, f"Database versie {current} is nieuwer dan script versie {target}"

    migraties_uitgevoerd = 0
    for v in range(current + 1, target + 1):
        if v not in MIGRATIONS:
            conn.close()
            return False, f"Migratie naar versie {v} niet gevonden"
        print(f"  Migratie {current} -> {v}...")
        MIGRATIONS[v](conn)
        migraties_uitgevoerd += 1

    conn.commit()
    conn.close()

    return True, f"Database gemigreerd van versie {current} naar {target} ({migraties_uitgevoerd} migraties)"


if __name__ == "__main__":
    print(f"Database pad: {DB_PATH}")
    succes, bericht = init_database()
    if succes:
        print(f"✅ {bericht}")
    else:
        print(f"❌ {bericht}")
        sys.exit(1)
