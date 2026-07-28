#!/usr/bin/env python3
"""Zoeken op bestandsnaam en metadata in de archive catalogus.

Zoekt in SQLite op filename, extensie, auteur, titel, datum, etc.
Toont per resultaat beschikbaarheidsinstructie.
"""

import sys
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "data" / "archive_catalog.db"


def search(query: str = None, extension: str = None, group: str = None,
           author: str = None, title: str = None, archive_label: str = None,
           date_from: str = None, date_to: str = None,
           limit: int = 50) -> list:
    """Zoek bestanden op basis van filters."""

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    conditions = []
    params = []

    if query:
        conditions.append(
            "(filename LIKE ? OR title LIKE ? OR full_path LIKE ? OR keywords LIKE ?)")
        q = f"%{query}%"
        params.extend([q, q, q, q])

    if extension:
        conditions.append("extension = ?")
        params.append(extension.lower().lstrip('.'))

    if group:
        conditions.append("extension_group = ?")
        params.append(group)

    if author:
        conditions.append("(author LIKE ? OR creator LIKE ? OR last_modified_by LIKE ?)")
        a = f"%{author}%"
        params.extend([a, a, a])

    if title:
        conditions.append("title LIKE ?")
        params.append(f"%{title}%")

    if archive_label:
        conditions.append("archive_label = ?")
        params.append(archive_label)

    if date_from:
        conditions.append("original_content_date >= ?")
        params.append(date_from)

    if date_to:
        conditions.append("original_content_date <= ?")
        params.append(date_to)

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT filename, title, author, original_content_date, date_confidence,
               extension, extension_group, human_size, archive_label,
               availability_status, full_path, relative_path
        FROM files
        WHERE {where}
        ORDER BY original_content_date DESC NULLS LAST, filename
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def format_results(results: list) -> str:
    """Formatteer zoekresultaten voor weergave."""
    if not results:
        return "Geen resultaten gevonden."

    lines = []
    lines.append(f"Gevonden: {len(results)} resultaten\n")
    lines.append(f"{'─' * 80}")

    for i, r in enumerate(results, 1):
        lines.append(f"\n[{i}] {r['filename']}")
        if r['title']:
            lines.append(f"    Titel: {r['title']}")
        if r['author']:
            lines.append(f"    Auteur: {r['author']}")
        if r['original_content_date']:
            conf = f" ({r['date_confidence']})" if r['date_confidence'] else ""
            lines.append(f"    Datum: {r['original_content_date'][:10]}{conf}")
        lines.append(f"    Type: {r['extension']} ({r['extension_group'] or '?'})")
        lines.append(f"    Grootte: {r['human_size']}")
        lines.append(f"    Label: {r['archive_label']}")
        lines.append(f"    Pad: {r['relative_path']}")

        if r['availability_status'] != 'online':
            lines.append(
                f"    ⚠️  OFFLINE — Sluit fysieke drager met sticker "
                f"{r['archive_label']} aan om dit bestand te openen.")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Zoek in archive catalogus op bestandsnaam/metadata")
    parser.add_argument("query", nargs="?", help="Zoekterm (bestandsnaam/titel/pad)")
    parser.add_argument("-e", "--extension", help="Filter op extensie")
    parser.add_argument("-g", "--group", help="Filter op extensiegroep")
    parser.add_argument("-a", "--author", help="Filter op auteur")
    parser.add_argument("-t", "--title", help="Filter op titel")
    parser.add_argument("-l", "--label", help="Filter op archive label")
    parser.add_argument("--from", dest="date_from", help="Datum vanaf (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", help="Datum tot (YYYY-MM-DD)")
    parser.add_argument("-n", "--limit", type=int, default=50, help="Max resultaten")

    args = parser.parse_args()

    if not any([args.query, args.extension, args.group, args.author,
                args.title, args.label, args.date_from]):
        parser.print_help()
        sys.exit(1)

    results = search(
        query=args.query,
        extension=args.extension,
        group=args.group,
        author=args.author,
        title=args.title,
        archive_label=args.label,
        date_from=args.date_from,
        date_to=args.date_to,
        limit=args.limit
    )

    print(format_results(results))


if __name__ == "__main__":
    main()
