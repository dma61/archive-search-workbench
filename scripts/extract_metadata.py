#!/usr/bin/env python3
"""Metadata-extractie uit bestanden.

Ondersteunt PDF, Office (DOCX/XLSX/PPTX), afbeeldingen (EXIF), en archieven.
Bepaalt original_content_date volgens prioriteitensysteem.
"""

import subprocess
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Optional


def _parse_date(date_str: Optional[str]) -> Optional[str]:
    """Probeer diverse datumformaten te parsen naar ISO string."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    if not date_str or date_str in ('None', 'null', '0', ''):
        return None

    formats = [
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d %H:%M:%S',
        '%Y:%m:%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%SZ',
        '%d/%m/%Y %H:%M:%S',
        '%m/%d/%Y %H:%M:%S',
        '%Y%m%d%H%M%S',
        '%Y-%m-%d',
        '%d-%m-%Y',
    ]
    # PDF D: formaat
    if date_str.startswith('D:'):
        cleaned = date_str[2:]
        cleaned = re.sub(r"['\+\-Z].*$", "", cleaned)
        cleaned = re.sub(r"[^\d]", "", cleaned[:14])
        if len(cleaned) >= 8:
            formats.insert(0, '%Y%m%d%H%M%S')
            formats.insert(1, '%Y%m%d')
            date_str = cleaned

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str[:19], fmt)
            return dt.isoformat()
        except (ValueError, OverflowError):
            continue
    return None


def _is_suspect_date(iso_str: Optional[str]) -> bool:
    """Controleer of datum verdacht is (1970, 1980, 1900, toekomst)."""
    if not iso_str:
        return False
    try:
        dt = datetime.fromisoformat(iso_str)
        # Strip timezone-info voor vergelijking — datetime.now() is altijd naive
        dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        if dt_naive.year in (1970, 1980, 1900, 1601):
            return True
        if dt_naive > datetime.now():
            return True
        if dt_naive.year < 1985:
            return True
    except (ValueError, OverflowError):
        return True
    return False


def extract_pdf_metadata(filepath: str) -> dict:
    """Extraheer metadata uit PDF via pymupdf."""
    result = {}
    try:
        import fitz
        doc = fitz.open(filepath)
        meta = doc.metadata or {}
        doc.close()

        result['author'] = meta.get('author') or None
        result['creator'] = meta.get('creator') or None
        result['producer'] = meta.get('producer') or None
        result['title'] = meta.get('title') or None
        result['subject'] = meta.get('subject') or None
        result['keywords'] = meta.get('keywords') or None
        result['document_created_time'] = _parse_date(meta.get('creationDate'))
        result['document_modified_time'] = _parse_date(meta.get('modDate'))
        result['application'] = meta.get('creator') or None
        result['metadata_source'] = 'pymupdf'
    except Exception as e:
        result['error_message'] = f"PDF metadata fout: {e}"
    return result


def extract_office_metadata(filepath: str) -> dict:
    """Extraheer metadata uit Office documenten (DOCX/XLSX/PPTX)."""
    result = {}
    ext = Path(filepath).suffix.lower()

    try:
        if ext == '.docx':
            from docx import Document
            doc = Document(filepath)
            props = doc.core_properties
            result['author'] = props.author or None
            result['creator'] = props.author or None
            result['last_modified_by'] = props.last_modified_by or None
            result['title'] = props.title or None
            result['subject'] = props.subject or None
            result['keywords'] = props.keywords or None
            result['company'] = None
            result['document_created_time'] = props.created.replace(tzinfo=None).isoformat() if props.created else None
            result['document_modified_time'] = props.modified.replace(tzinfo=None).isoformat() if props.modified else None
            result['metadata_source'] = 'python-docx'

        elif ext in ('.xlsx', '.xlsm'):
            from openpyxl import load_workbook
            wb = load_workbook(filepath, read_only=True, data_only=True)
            props = wb.properties
            result['author'] = props.creator or None
            result['creator'] = props.creator or None
            result['last_modified_by'] = props.lastModifiedBy or None
            result['title'] = props.title or None
            result['subject'] = props.subject or None
            result['keywords'] = props.keywords or None
            result['document_created_time'] = props.created.replace(tzinfo=None).isoformat() if props.created else None
            result['document_modified_time'] = props.modified.replace(tzinfo=None).isoformat() if props.modified else None
            result['metadata_source'] = 'openpyxl'
            wb.close()

    except Exception as e:
        result['error_message'] = f"Office metadata fout: {e}"
    return result


def extract_image_metadata(filepath: str) -> dict:
    """Extraheer EXIF metadata uit afbeeldingen via exiftool."""
    result = {}
    try:
        output = subprocess.run(
            ['exiftool', '-json', '-DateTimeOriginal', '-DateTimeDigitized',
             '-CreateDate', '-Artist', '-Copyright', '-Software',
             '-Model', '-Make', filepath],
            capture_output=True, text=True, timeout=30
        )
        if output.returncode == 0 and output.stdout.strip():
            data = json.loads(output.stdout)[0]
            result['document_created_time'] = _parse_date(
                data.get('DateTimeOriginal') or data.get('CreateDate'))
            result['document_modified_time'] = _parse_date(data.get('DateTimeDigitized'))
            result['author'] = data.get('Artist') or None
            result['application'] = data.get('Software') or None
            creator_parts = [data.get('Make', ''), data.get('Model', '')]
            result['creator'] = ' '.join(p for p in creator_parts if p).strip() or None
            result['metadata_source'] = 'exiftool'
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        result['error_message'] = f"EXIF metadata fout: {e}"
    return result


def extract_date_from_filename(filename: str) -> Optional[str]:
    """Probeer een datum te herkennen in de bestandsnaam."""
    patterns = [
        r'(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])',
        r'(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])',
        r'(20\d{2})_(0[1-9]|1[0-2])_(0[1-9]|[12]\d|3[01])',
        r'(19\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])',
    ]
    for pattern in patterns:
        m = re.search(pattern, filename)
        if m:
            try:
                date_str = m.group(0).replace('_', '').replace('-', '')
                dt = datetime.strptime(date_str[:8], '%Y%m%d')
                if 1985 < dt.year <= datetime.now().year:
                    return dt.isoformat()
            except ValueError:
                continue
    return None


def determine_original_content_date(file_meta: dict) -> tuple:
    """Bepaal original_content_date volgens prioriteitensysteem.

    Retourneert (datum_iso, bron, confidence).
    """
    doc_created = file_meta.get('document_created_time')
    if doc_created and not _is_suspect_date(doc_created):
        return doc_created, 'document_metadata', 'high'

    doc_modified = file_meta.get('document_modified_time')
    if doc_modified and not _is_suspect_date(doc_modified):
        return doc_modified, 'document_metadata_modified', 'medium'

    filename_date = extract_date_from_filename(file_meta.get('filename', ''))
    if filename_date:
        return filename_date, 'filename_pattern', 'medium'

    fs_modified = file_meta.get('filesystem_modified_time')
    if fs_modified and not _is_suspect_date(fs_modified):
        return fs_modified, 'filesystem_modified', 'low'

    fs_created = file_meta.get('filesystem_created_time')
    if fs_created and not _is_suspect_date(fs_created):
        return fs_created, 'filesystem_created', 'low'

    return None, 'none', 'suspect'


EXTENSION_GROUPS = {
    'documenten': {'pdf', 'doc', 'docx', 'odt', 'rtf', 'txt'},
    'spreadsheets': {'xls', 'xlsx', 'xlsm', 'csv'},
    'databases': {'mdb', 'accdb', 'sqlite', 'db', 'dbf', 'sql'},
    'afbeeldingen': {'jpg', 'jpeg', 'png', 'gif', 'tiff', 'bmp', 'heic', 'webp'},
    'archieven': {'zip', 'rar', '7z', 'tar', 'gz'},
    'code_kennis': {'md', 'yaml', 'yml', 'json', 'xml', 'puml', 'py', 'ps1',
                    'sh', 'sql', 'html', 'css', 'js'},
}


def get_extension_group(ext: str) -> Optional[str]:
    """Bepaal de extensiegroep voor een bestandsextensie."""
    ext_clean = ext.lower().lstrip('.')
    for group, extensions in EXTENSION_GROUPS.items():
        if ext_clean in extensions:
            return group
    return None


def human_size(size_bytes: int) -> str:
    """Converteer bytes naar leesbare grootte."""
    if size_bytes is None:
        return '?'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"
