#!/usr/bin/env python3
"""Rapportage-generator voor Archive Search Workbench.

Genereert rapport.md en diverse CSV-exports uit de archive catalogus.
"""

import sys
import sqlite3
import csv
from pathlib import Path
from datetime import datetime

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "data" / "archive_catalog.db"
OUTPUT_DIR = PROJECT_DIR / "output"


def generate_reports():
    """Genereer alle rapporten en CSV exports."""
    timestamp = datetime.now().strftime('%Y%m%d-%H%M')
    report_dir = OUTPUT_DIR / f"rapport_{timestamp}"
    report_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    print(f"Rapportage genereren in: {report_dir}")

    # Statistieken ophalen
    media = conn.execute("SELECT * FROM physical_media ORDER BY archive_label").fetchall()
    scans = conn.execute("SELECT * FROM scans ORDER BY start_time DESC").fetchall()
    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_bytes = conn.execute("SELECT SUM(size_bytes) FROM files").fetchone()[0] or 0

    # Rapport.md
    with open(report_dir / "rapport.md", 'w', encoding='utf-8') as f:
        f.write(f"# Archive Search Workbench — Rapport\n\n")
        f.write(f"Gegenereerd: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")

        f.write(f"## Overzicht fysieke media\n\n")
        f.write(f"| Label | Type | Volume | Eerste gezien | Sticker |\n")
        f.write(f"|-------|------|--------|---------------|--------|\n")
        for m in media:
            sticker = "Ja" if m['sticker_confirmed'] else "Nee"
            f.write(f"| {m['archive_label']} | {m['media_type']} | "
                    f"{m['volume_label'] or '?'} | {m['first_seen'][:10]} | {sticker} |\n")

        f.write(f"\n## Totalen\n\n")
        f.write(f"- Fysieke media: {len(media)}\n")
        f.write(f"- Scans: {len(scans)}\n")
        f.write(f"- Bestanden: {total_files}\n")
        f.write(f"- Totale grootte: {total_bytes / (1024**3):.1f} GB\n\n")

        # Top extensies
        f.write(f"## Top 20 extensies\n\n")
        f.write(f"| Extensie | Aantal | Grootte |\n")
        f.write(f"|----------|--------|---------|\n")
        ext_stats = conn.execute("""
            SELECT extension, COUNT(*) as cnt, SUM(size_bytes) as total
            FROM files GROUP BY extension ORDER BY cnt DESC LIMIT 20
        """).fetchall()
        for e in ext_stats:
            size_gb = (e['total'] or 0) / (1024**3)
            f.write(f"| {e['extension'] or '(geen)'} | {e['cnt']} | {size_gb:.2f} GB |\n")

        # Top auteurs
        f.write(f"\n## Top auteurs\n\n")
        authors = conn.execute("""
            SELECT author, COUNT(*) as cnt FROM files
            WHERE author IS NOT NULL AND author != ''
            GROUP BY author ORDER BY cnt DESC LIMIT 20
        """).fetchall()
        if authors:
            f.write(f"| Auteur | Aantal |\n")
            f.write(f"|--------|--------|\n")
            for a in authors:
                f.write(f"| {a['author']} | {a['cnt']} |\n")
        else:
            f.write("Geen auteursinformatie gevonden.\n")

        # Oudste documenten
        f.write(f"\n## Oudste 20 documenten (op inhoudelijke datum)\n\n")
        oldest = conn.execute("""
            SELECT filename, original_content_date, date_source, archive_label, relative_path
            FROM files WHERE original_content_date IS NOT NULL
            AND date_confidence != 'suspect'
            ORDER BY original_content_date ASC LIMIT 20
        """).fetchall()
        for o in oldest:
            f.write(f"- **{o['original_content_date'][:10]}** — {o['filename']} "
                    f"({o['archive_label']}, bron: {o['date_source']})\n")

        # Databases gevonden
        f.write(f"\n## Gevonden databases\n\n")
        dbs = conn.execute("""
            SELECT filename, extension, human_size, archive_label, relative_path
            FROM files WHERE extension_group = 'databases'
            ORDER BY size_bytes DESC
        """).fetchall()
        if dbs:
            for d in dbs:
                f.write(f"- {d['filename']} ({d['human_size']}) — {d['archive_label']}:{d['relative_path']}\n")
        else:
            f.write("Geen databases gevonden.\n")

        # Archieven
        f.write(f"\n## Gevonden archieven\n\n")
        archives = conn.execute("""
            SELECT filename, extension, human_size, archive_label, relative_path
            FROM files WHERE extension_group = 'archieven'
            ORDER BY size_bytes DESC LIMIT 30
        """).fetchall()
        if archives:
            for a in archives:
                f.write(f"- {a['filename']} ({a['human_size']}) — {a['archive_label']}:{a['relative_path']}\n")
        else:
            f.write("Geen archieven gevonden.\n")

        # Verdachte datums
        f.write(f"\n## Bestanden met verdachte datums\n\n")
        suspect = conn.execute("""
            SELECT filename, date_confidence, date_source, original_content_date, archive_label
            FROM files WHERE date_confidence = 'suspect' LIMIT 20
        """).fetchall()
        if suspect:
            for s in suspect:
                f.write(f"- {s['filename']} (datum: {s['original_content_date']}, "
                        f"bron: {s['date_source']}, label: {s['archive_label']})\n")

    print(f"  rapport.md geschreven")

    # CSV exports
    csv_queries = {
        'alle_bestanden': "SELECT * FROM files ORDER BY archive_label, relative_path",
        'documenten': "SELECT * FROM files WHERE extension_group = 'documenten' ORDER BY original_content_date",
        'spreadsheets': "SELECT * FROM files WHERE extension_group = 'spreadsheets' ORDER BY original_content_date",
        'databases': "SELECT * FROM files WHERE extension_group = 'databases' ORDER BY size_bytes DESC",
        'afbeeldingen': "SELECT * FROM files WHERE extension_group = 'afbeeldingen' ORDER BY original_content_date",
        'archieven': "SELECT * FROM files WHERE extension_group = 'archieven' ORDER BY size_bytes DESC",
        'code_kennis': "SELECT * FROM files WHERE extension_group = 'code_kennis' ORDER BY relative_path",
        'grootste_500': "SELECT * FROM files ORDER BY size_bytes DESC LIMIT 500",
        'oudste_500': "SELECT * FROM files WHERE original_content_date IS NOT NULL ORDER BY original_content_date ASC LIMIT 500",
        'recentste_500': "SELECT * FROM files WHERE original_content_date IS NOT NULL ORDER BY original_content_date DESC LIMIT 500",
        'fysieke_media': "SELECT * FROM physical_media ORDER BY archive_label",
    }

    for name, query in csv_queries.items():
        rows = conn.execute(query).fetchall()
        if rows:
            csv_path = report_dir / f"{name}.csv"
            with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(rows[0].keys())
                for row in rows:
                    writer.writerow(list(row))
            print(f"  {name}.csv ({len(rows)} rijen)")
        else:
            print(f"  {name}.csv (leeg, overgeslagen)")

    # Extensie-overzicht
    ext_overview = conn.execute("""
        SELECT extension, extension_group, COUNT(*) as aantal,
               SUM(size_bytes) as totaal_bytes,
               MIN(original_content_date) as oudste,
               MAX(original_content_date) as nieuwste
        FROM files GROUP BY extension ORDER BY aantal DESC
    """).fetchall()
    if ext_overview:
        csv_path = report_dir / "extensie_overzicht.csv"
        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(['extensie', 'groep', 'aantal', 'totaal_bytes', 'oudste', 'nieuwste'])
            for row in ext_overview:
                writer.writerow(list(row))
        print(f"  extensie_overzicht.csv ({len(ext_overview)} rijen)")

    # Mappen-overzicht
    dir_overview = conn.execute("""
        SELECT parent_dir, COUNT(*) as aantal, SUM(size_bytes) as totaal_bytes, archive_label
        FROM files GROUP BY parent_dir ORDER BY totaal_bytes DESC LIMIT 100
    """).fetchall()
    if dir_overview:
        csv_path = report_dir / "mappen_overzicht.csv"
        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(['map', 'aantal', 'totaal_bytes', 'archive_label'])
            for row in dir_overview:
                writer.writerow(list(row))
        print(f"  mappen_overzicht.csv ({len(dir_overview)} rijen)")

    conn.close()
    print(f"\n✅ Rapportage compleet in: {report_dir}")
    return str(report_dir)


if __name__ == "__main__":
    generate_reports()
