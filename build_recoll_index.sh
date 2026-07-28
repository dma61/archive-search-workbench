#!/usr/bin/env bash
# build_recoll_index.sh — Bouw/update Recoll full-text index per medium
# Maakt per archive-label een aparte index met eigen recoll.conf

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INDEX_BASE="$PROJECT_DIR/recoll-indexes"
MOUNT_BASE="/mnt/archive-ingest"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/recoll_$(date +%Y%m%d-%H%M).log"

GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[1;33m"
NC="\033[0m"

log() { echo -e "$(date +%H:%M:%S) $1" | tee -a "$LOG_FILE"; }
ok()  { log "${GREEN}✅ $1${NC}"; }
err() { log "${RED}❌ $1${NC}"; }
warn(){ log "${YELLOW}⚠️  $1${NC}"; }

# Genereer recoll.conf voor een specifiek medium
generate_recoll_conf() {
    local index_dir="$1"
    local source_dir="$2"
    local conf_file="$index_dir/recoll.conf"

    cat > "$conf_file" << EOFCONF
# Recoll configuratie voor $(basename "$source_dir")
# Automatisch gegenereerd door build_recoll_index.sh

topdirs = $source_dir

# Indexeer inhoud van archieven
zipSkippedNames =
zipMaxMBs = 500

# Bestandsgrootte limiet
maxfsoccuppc = 0
compressedfilemaxkbs = 500000

# Overslaan
skippedNames = .git node_modules \$RECYCLE.BIN RECYCLER System Volume Information
skippedPaths =

# Ondersteunde types
indexallfilenames = 1

# Logging
loglevel = 3
logfilename = $index_dir/recoll.log

# Database locatie
dbdir = $index_dir/xapiandb
EOFCONF
    log "  recoll.conf gegenereerd: $conf_file"
}

# Bouw index voor een specifiek label
build_index_for_label() {
    local label="$1"
    local source_dir="$MOUNT_BASE/$label"
    local index_dir="$INDEX_BASE/$label"

    if [ ! -d "$source_dir" ]; then
        err "Bronmap niet gevonden: $source_dir"
        return 1
    fi

    log "Indexering starten voor: $label"
    log "  Bron: $source_dir"
    log "  Index: $index_dir"

    mkdir -p "$index_dir"
    generate_recoll_conf "$index_dir" "$source_dir"

    # Bouw index met nice/ionice voor lage systeemlast
    nice -n 19 ionice -c 3 recollindex -c "$index_dir" >> "$LOG_FILE" 2>&1

    if [ $? -eq 0 ]; then
        ok "Index gebouwd voor $label"
        # Toon statistieken
        local doc_count
        doc_count=$(recoll -c "$index_dir" -t -q "a OR e OR i" 2>/dev/null | head -1 || echo "?")
        log "  Documenten geindexeerd: $doc_count"
    else
        err "Indexering mislukt voor $label — zie $LOG_FILE"
        return 1
    fi
}

# Main
echo -e "${GREEN}=== Recoll Indexering ===${NC}" | tee -a "$LOG_FILE"
log "Datum: $(date '+%Y-%m-%d %H:%M')"

if [ $# -gt 0 ]; then
    # Specifiek label opgegeven
    build_index_for_label "$1"
else
    # Alle gemounte labels indexeren
    if [ ! -d "$MOUNT_BASE" ]; then
        err "Mount base niet gevonden: $MOUNT_BASE"
        exit 1
    fi

    labels_found=0
    for dir in "$MOUNT_BASE"/*/; do
        if [ -d "$dir" ]; then
            label=$(basename "$dir")
            build_index_for_label "$label"
            labels_found=$((labels_found + 1))
        fi
    done

    if [ $labels_found -eq 0 ]; then
        warn "Geen gemounte media gevonden in $MOUNT_BASE"
        warn "Mount eerst een medium via: ./mount_readonly.sh /dev/sdX1 LABEL"
    else
        ok "Indexering compleet voor $labels_found media"
    fi
fi

log "Log: $LOG_FILE"
