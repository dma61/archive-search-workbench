#!/usr/bin/env bash
# smoke_test.sh — Snelle verificatie dat alle componenten werken

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$PROJECT_DIR/.venv/bin/python"

GREEN="\033[0;32m"
RED="\033[0;31m"
NC="\033[0m"

PASS=0
FAIL=0

check() {
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo -e "${GREEN}PASS${NC} $desc"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}FAIL${NC} $desc"
        FAIL=$((FAIL + 1))
    fi
}

echo "Archive Search Workbench — Smoke Test"
echo "======================================"
echo ""

# Bestaan van bestanden
check "setup.sh bestaat" test -f "$PROJECT_DIR/setup.sh"
check "menu.sh bestaat" test -f "$PROJECT_DIR/menu.sh"
check "detect_disks.sh bestaat" test -f "$PROJECT_DIR/detect_disks.sh"
check "mount_readonly.sh bestaat" test -f "$PROJECT_DIR/mount_readonly.sh"
check "build_recoll_index.sh bestaat" test -f "$PROJECT_DIR/build_recoll_index.sh"
check "search_content.sh bestaat" test -f "$PROJECT_DIR/search_content.sh"
check "config.yaml bestaat" test -f "$PROJECT_DIR/config/config.yaml"
check "db_init.py bestaat" test -f "$PROJECT_DIR/scripts/db_init.py"
check "scan_metadata.py bestaat" test -f "$PROJECT_DIR/scripts/scan_metadata.py"
check "extract_metadata.py bestaat" test -f "$PROJECT_DIR/scripts/extract_metadata.py"
check "search_filename.py bestaat" test -f "$PROJECT_DIR/scripts/search_filename.py"
check "report.py bestaat" test -f "$PROJECT_DIR/scripts/report.py"

# Venv en imports
check "Python venv bestaat" test -f "$VENV"
check "Python imports laden" "$VENV" -c "import yaml, rich, tqdm, pandas, openpyxl, docx, fitz, PIL, tabulate"

# Database
check "Database bestaat" test -f "$PROJECT_DIR/data/archive_catalog.db"
check "Schema versie = 1" "$VENV" -c "
import sqlite3
conn = sqlite3.connect('$PROJECT_DIR/data/archive_catalog.db')
v = conn.execute('SELECT version FROM schema_version').fetchone()[0]
assert v == 1, f'Schema versie is {v}, verwacht 1'
"

# Tools
check "recollindex beschikbaar" command -v recollindex
check "rg beschikbaar" command -v rg
check "exiftool beschikbaar" command -v exiftool
check "sqlite3 beschikbaar" command -v sqlite3

# db_init.py idempotent
check "db_init.py idempotent" "$VENV" "$PROJECT_DIR/scripts/db_init.py"

echo ""
echo "======================================"
echo -e "Resultaat: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"

if [ $FAIL -gt 0 ]; then
    exit 1
fi
