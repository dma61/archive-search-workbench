#!/usr/bin/env bash
# search_content.sh — Zoek in documentinhoud via Recoll full-text index
# Wrapper rond recoll query

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INDEX_BASE="$PROJECT_DIR/recoll-indexes"

GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
NC="\033[0m"

usage() {
    echo "Gebruik: $0 <zoekterm> [--label ARCHIVE-LABEL] [--limit N]"
    echo ""
    echo "Voorbeelden:"
    echo "  $0 'contract huur'"
    echo "  $0 'factuur 2014' --label ARCHIVE-DISK-001"
    echo "  $0 'python script' --limit 20"
}

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

QUERY="$1"
shift

LABEL=""
LIMIT=25

while [ $# -gt 0 ]; do
    case "$1" in
        --label) LABEL="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Onbekende optie: $1"; usage; exit 1 ;;
    esac
done

echo -e "${CYAN}=== Inhoud zoeken: '$QUERY' ===${NC}"
echo ""

search_in_index() {
    local label="$1"
    local index_dir="$INDEX_BASE/$label"

    if [ ! -d "$index_dir" ]; then
        return
    fi

    echo -e "${GREEN}--- $label ---${NC}"

    # Recoll text-mode query
    results=$(recoll -c "$index_dir" -t -n "$LIMIT" -q "$QUERY" 2>/dev/null || true)

    if [ -z "$results" ]; then
        echo "  Geen resultaten."
        return
    fi

    echo "$results" | while IFS= read -r line; do
        # Recoll output bevat bestandspaden en snippets
        if echo "$line" | grep -q "^/"; then
            echo -e "  ${YELLOW}$label:${NC}$line"
        elif echo "$line" | grep -q "ABSTRACT"; then
            snippet=$(echo "$line" | sed 's/ABSTRACT *//')
            echo "    $snippet"
        else
            echo "  $line"
        fi
    done
    echo ""
}

if [ -n "$LABEL" ]; then
    search_in_index "$LABEL"
else
    # Zoek in alle beschikbare indexes
    found=0
    for dir in "$INDEX_BASE"/*/; do
        if [ -d "$dir" ]; then
            label=$(basename "$dir")
            search_in_index "$label"
            found=$((found + 1))
        fi
    done

    if [ $found -eq 0 ]; then
        echo "Geen Recoll indexes gevonden."
        echo "Bouw eerst een index via: ./build_recoll_index.sh"
    fi
fi
