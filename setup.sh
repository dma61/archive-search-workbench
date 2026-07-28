#!/usr/bin/env bash
# setup.sh — Idempotente setup voor Archive Search Workbench
# Installeert systeem-packages, maakt Python venv, maakt directories

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/setup_$(date +%Y%m%d-%H%M).log"

# Kleuren
GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[1;33m"
NC="\033[0m"

log() { echo -e "$(date +%H:%M:%S) $1" | tee -a "$LOG_FILE"; }
ok()  { log "✅ $1"; }
err() { log "❌ $1"; }
warn(){ log "⚠️  $1"; }
info(){ log "🔍 $1"; }

echo "========================================" | tee -a "$LOG_FILE"
log "Archive Search Workbench — Setup"
log "Datum: $(date +%Y-%m-%d\ %H:%M)"
log "Machine: $(hostname)"
echo "========================================" | tee -a "$LOG_FILE"

# --- Directories aanmaken ---
info "Directories controleren..."
DIRS=(config scripts data output logs recoll-indexes temp tests)
for dir in "${DIRS[@]}"; do
    mkdir -p "$PROJECT_DIR/$dir"
done
ok "Alle directories aanwezig"

# --- Mount directory voor ingest ---
if [ ! -d /mnt/archive-ingest ]; then
    info "Mountpoint /mnt/archive-ingest aanmaken..."
    sudo mkdir -p /mnt/archive-ingest
    sudo chown $(whoami):$(whoami) /mnt/archive-ingest
    ok "/mnt/archive-ingest aangemaakt"
else
    ok "/mnt/archive-ingest bestaat al"
fi

# --- Systeem-packages ---
info "Systeem-packages controleren..."

PACKAGES=(
    python3-venv
    python3-pip
    sqlite3
    recoll
    antiword
    catdoc
    poppler-utils
    unzip
    p7zip-full
    unrar
    ripgrep
    smartmontools
    libimage-exiftool-perl
    mediainfo
    ntfs-3g
    udisks2
)

MISSING=()
for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -eq 0 ]; then
    ok "Alle systeem-packages al geïnstalleerd"
else
    warn "Ontbrekende packages: ${MISSING[*]}"
    info "Installeren via apt..."
    sudo apt update >> "$LOG_FILE" 2>&1
    sudo apt install -y "${MISSING[@]}" >> "$LOG_FILE" 2>&1
    if [ $? -eq 0 ]; then
        ok "Packages geïnstalleerd: ${MISSING[*]}"
    else
        err "Fout bij installeren packages — zie $LOG_FILE"
        exit 1
    fi
fi

# --- Verificatie kritieke tools ---
info "Tools verifiëren..."
TOOLS_OK=0
TOOLS_FAIL=0

check_tool() {
    if command -v "$1" >/dev/null 2>&1; then
        ok "$1 gevonden: $(command -v "$1")"
        TOOLS_OK=$((TOOLS_OK+1))
    else
        err "$1 NIET gevonden"
        TOOLS_FAIL=$((TOOLS_FAIL+1))
    fi
}

check_tool python3
check_tool sqlite3
check_tool recollindex
check_tool rg
check_tool exiftool
check_tool mediainfo
check_tool 7z
check_tool unrar

log "Tools: $TOOLS_OK ok, $TOOLS_FAIL mislukt"
if [ $TOOLS_FAIL -gt 0 ]; then
    warn "Niet alle tools beschikbaar — sommige functies werken mogelijk niet"
fi

# --- Python venv ---
VENV="$PROJECT_DIR/.venv"
if [ ! -d "$VENV" ]; then
    info "Python venv aanmaken..."
    python3 -m venv "$VENV"
    ok "Venv aangemaakt in .venv/"
else
    ok "Venv bestaat al"
fi

info "Python packages installeren in venv..."
"$VENV/bin/pip" install --upgrade pip >> "$LOG_FILE" 2>&1
"$VENV/bin/pip" install     pyyaml     rich     tqdm     pandas     openpyxl     python-docx     pymupdf     pillow     tabulate     >> "$LOG_FILE" 2>&1

if [ $? -eq 0 ]; then
    ok "Python packages geïnstalleerd"
else
    err "Fout bij pip install — zie $LOG_FILE"
    exit 1
fi

# --- Verificatie Python imports ---
info "Python imports testen..."
"$VENV/bin/python" -c "
import yaml, rich, tqdm, pandas, openpyxl, docx, fitz, PIL, tabulate
print('Alle imports OK')
" >> "$LOG_FILE" 2>&1
if [ $? -eq 0 ]; then
    ok "Alle Python packages importeerbaar"
else
    err "Niet alle Python packages laden — zie $LOG_FILE"
fi

# --- SQLite versie check ---
SQLITE_VER=$(python3 -c "import sqlite3; print(sqlite3.sqlite_version)")
ok "SQLite versie: $SQLITE_VER"

# --- Samenvatting ---
echo "" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"
ok "Setup compleet!"
log "Log: $LOG_FILE"
log "Volgende stap: ./menu.sh"
echo "========================================" | tee -a "$LOG_FILE"
