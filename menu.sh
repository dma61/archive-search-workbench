#!/usr/bin/env bash
# menu.sh — Interactief menu voor Archive Search Workbench

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$PROJECT_DIR/.venv/bin/python"

GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
NC="\033[0m"

clear_screen() { echo -e "\033[2J\033[H"; }

show_menu() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║   Archive Search Workbench — Hoofdmenu              ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${GREEN}[1]${NC}  Toon aangesloten opslagmedia"
    echo -e "  ${GREEN}[2]${NC}  Toon mountpoints"
    echo -e "  ${GREEN}[3]${NC}  Registreer / label nieuw fysiek medium"
    echo -e "  ${GREEN}[4]${NC}  Mount extern medium read-only"
    echo -e "  ${GREEN}[5]${NC}  Bekijk/wijzig config"
    echo -e "  ${GREEN}[6]${NC}  Scan metadata"
    echo -e "  ${GREEN}[7]${NC}  Bouw/update Recoll index"
    echo -e "  ${GREEN}[8]${NC}  Zoek op bestandsnaam/metadata"
    echo -e "  ${GREEN}[9]${NC}  Zoek in documentinhoud"
    echo -e "  ${GREEN}[10]${NC} Maak rapportages"
    echo -e "  ${GREEN}[11]${NC} Toon scanstatus"
    echo -e "  ${GREEN}[12]${NC} Veilige unmount"
    echo -e "  ${GREEN}[13]${NC} Toon bekende fysieke media"
    echo -e "  ${GREEN}[14]${NC} Bekijk logs"
    echo -e "  ${RED}[0]${NC}  Stop"
    echo ""
    echo -n "Keuze: "
}

show_media() {
    "$PROJECT_DIR/detect_disks.sh"
}

show_mounts() {
    echo -e "${CYAN}=== Actieve mountpoints (archive-ingest) ===${NC}"
    findmnt --target /mnt/archive-ingest 2>/dev/null || echo "(geen mounts onder /mnt/archive-ingest)"
    echo ""
    echo "Alle archive mounts:"
    mount | grep "archive-ingest" || echo "(geen)"
}

register_medium() {
    echo -e "${CYAN}=== Nieuw medium registreren ===${NC}"
    "$PROJECT_DIR/detect_disks.sh"
    echo ""
    echo -n "Device (bijv. /dev/sda1): "
    read -r device
    echo -n "Archive label (bijv. ARCHIVE-DISK-001): "
    read -r label
    echo -n "Media type (usb_hdd/usb_ssd/usb_flash/sd_card/external_sata_usb): "
    read -r mtype

    # Mount read-only
    "$PROJECT_DIR/mount_readonly.sh" "$device" "$label"

    echo ""
    echo -e "${YELLOW}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  Plak nu een sticker op deze fysieke drager met:    ║${NC}"
    echo -e "${YELLOW}║                                                      ║${NC}"
    echo -e "${YELLOW}║     Label: ${GREEN}$label${YELLOW}                    ║${NC}"
    echo -e "${YELLOW}║                                                      ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -n "Is de sticker geplakt? [j/n]: "
    read -r confirmed

    sticker_val=0
    if [ "$confirmed" = "j" ] || [ "$confirmed" = "J" ]; then
        sticker_val=1
    fi

    # Registreer in database
    "$VENV" -c "
import sqlite3, sys
from datetime import datetime
from pathlib import Path
db = Path('$PROJECT_DIR/data/archive_catalog.db')
conn = sqlite3.connect(str(db))
now = datetime.now().isoformat()
# Check of al bestaat
existing = conn.execute('SELECT media_id FROM physical_media WHERE archive_label = ?', ('$label',)).fetchone()
if existing:
    conn.execute('UPDATE physical_media SET last_seen = ?, sticker_confirmed = ?, sticker_confirmed_at = ? WHERE archive_label = ?',
        (now, $sticker_val, now if $sticker_val else None, '$label'))
else:
    conn.execute('''INSERT INTO physical_media (archive_label, media_type, first_seen, last_seen, sticker_confirmed, sticker_confirmed_at)
        VALUES (?, ?, ?, ?, ?, ?)''', ('$label', '$mtype', now, now, $sticker_val, now if $sticker_val else None))
conn.commit()
conn.close()
print('Medium geregistreerd in database')
"
    echo -e "${GREEN}✅ Medium $label geregistreerd en gemount${NC}"
}

mount_medium() {
    echo -e "${CYAN}=== Mount extern medium read-only ===${NC}"
    "$PROJECT_DIR/detect_disks.sh"
    echo ""
    echo -n "Device (bijv. /dev/sda1): "
    read -r device
    echo -n "Archive label: "
    read -r label
    "$PROJECT_DIR/mount_readonly.sh" "$device" "$label"
}

view_config() {
    echo -e "${CYAN}=== Config (config/config.yaml) ===${NC}"
    cat "$PROJECT_DIR/config/config.yaml"
    echo ""
    echo -e "${YELLOW}Bewerk met: nano $PROJECT_DIR/config/config.yaml${NC}"
}

scan_metadata() {
    echo -e "${CYAN}=== Metadata scan starten ===${NC}"
    "$VENV" "$PROJECT_DIR/scripts/scan_metadata.py"
}

build_index() {
    echo -e "${CYAN}=== Recoll indexering ===${NC}"
    echo -n "Specifiek label (leeg voor alle): "
    read -r label
    if [ -n "$label" ]; then
        "$PROJECT_DIR/build_recoll_index.sh" "$label"
    else
        "$PROJECT_DIR/build_recoll_index.sh"
    fi
}

search_filename() {
    echo -e "${CYAN}=== Zoek op bestandsnaam/metadata ===${NC}"
    echo -n "Zoekterm: "
    read -r query
    echo -n "Extensie filter (leeg = alle): "
    read -r ext

    cmd="$VENV $PROJECT_DIR/scripts/search_filename.py"
    if [ -n "$query" ]; then cmd="$cmd \"$query\""; fi
    if [ -n "$ext" ]; then cmd="$cmd -e $ext"; fi

    eval "$cmd"
}

search_content() {
    echo -e "${CYAN}=== Zoek in documentinhoud ===${NC}"
    echo -n "Zoekterm: "
    read -r query
    "$PROJECT_DIR/search_content.sh" "$query"
}

make_reports() {
    "$VENV" "$PROJECT_DIR/scripts/report.py"
}

show_scan_status() {
    echo -e "${CYAN}=== Scan status ===${NC}"
    "$VENV" -c "
import sqlite3
from pathlib import Path
db = Path('$PROJECT_DIR/data/archive_catalog.db')
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
scans = conn.execute('SELECT * FROM scans ORDER BY start_time DESC LIMIT 10').fetchall()
if not scans:
    print('Geen scans gevonden.')
else:
    print(f'Laatste {len(scans)} scans:')
    print(f'{\"─\" * 80}')
    for s in scans:
        print(f'  [{s[\"status\"]:11}] {s[\"archive_label\"]:20} | '
              f'{s[\"number_of_files\"]:>6} bestanden | '
              f'{s[\"start_time\"][:16]}')
        if s['files_error'] and s['files_error'] > 0:
            print(f'              Fouten: {s[\"files_error\"]}')
conn.close()
"
}

unmount_medium() {
    echo -e "${CYAN}=== Veilige unmount ===${NC}"
    echo "Actieve archive mounts:"
    mount | grep "archive-ingest" || echo "(geen)"
    echo ""
    echo -n "Archive label om te unmounten: "
    read -r label
    "$PROJECT_DIR/mount_readonly.sh" --unmount "$label"
}

show_known_media() {
    echo -e "${CYAN}=== Bekende fysieke media ===${NC}"
    "$VENV" -c "
import sqlite3
from pathlib import Path
db = Path('$PROJECT_DIR/data/archive_catalog.db')
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
media = conn.execute('SELECT * FROM physical_media ORDER BY archive_label').fetchall()
if not media:
    print('Geen media geregistreerd.')
else:
    print(f'{'Label':<22} {'Type':<16} {'Volume':<16} {'Sticker':<8} {'Eerste gezien'}')
    print('─' * 80)
    for m in media:
        sticker = 'Ja' if m['sticker_confirmed'] else 'Nee'
        vol = m['volume_label'] or '?'
        print(f'{m[\"archive_label\"]:<22} {m[\"media_type\"]:<16} {vol:<16} {sticker:<8} {m[\"first_seen\"][:10]}')
conn.close()
"
}

view_logs() {
    echo -e "${CYAN}=== Recente logs ===${NC}"
    ls -lt "$PROJECT_DIR/logs/" | head -10
    echo ""
    echo -n "Log bekijken (bestandsnaam, of Enter voor nieuwste): "
    read -r logfile
    if [ -z "$logfile" ]; then
        logfile=$(ls -t "$PROJECT_DIR/logs/" | head -1)
    fi
    if [ -n "$logfile" ] && [ -f "$PROJECT_DIR/logs/$logfile" ]; then
        tail -50 "$PROJECT_DIR/logs/$logfile"
    fi
}

# Main loop
while true; do
    show_menu
    read -r choice
    echo ""

    case "$choice" in
        1)  show_media ;;
        2)  show_mounts ;;
        3)  register_medium ;;
        4)  mount_medium ;;
        5)  view_config ;;
        6)  scan_metadata ;;
        7)  build_index ;;
        8)  search_filename ;;
        9)  search_content ;;
        10) make_reports ;;
        11) show_scan_status ;;
        12) unmount_medium ;;
        13) show_known_media ;;
        14) view_logs ;;
        0)  echo -e "${GREEN}Tot ziens!${NC}"; exit 0 ;;
        *)  echo -e "${RED}Ongeldige keuze${NC}" ;;
    esac

    echo ""
    echo -n "Druk Enter om door te gaan..."
    read -r
done
