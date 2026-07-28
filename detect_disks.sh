#!/usr/bin/env bash
# detect_disks.sh — Toon aangesloten opslagmedia met details
# Detecteert removable devices en toont relevante informatie

set -euo pipefail

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
CYAN="\033[0;36m"
NC="\033[0m"

echo -e "=== Aangesloten Opslagmedia ==="
echo "Datum: $(date '+%Y-%m-%d %H:%M')"
echo ""

# Gebruik lsblk JSON output voor betrouwbare parsing
DEVICES=$(lsblk -J -o NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,RM,HOTPLUG 2>/dev/null)

if [ -z "$DEVICES" ]; then
    echo "❌ Kan lsblk niet uitvoeren"
    exit 1
fi

# Toon alle block devices die relevant zijn (niet nvme boot disk)
echo -e "Alle block devices:"
echo ""
printf "%-10s %-10s %-8s %-8s %-20s %-12s %-30s %-5s\n"     "DEVICE" "GROOTTE" "TYPE" "FS" "LABEL" "TRAN" "MODEL" "RM"
echo "────────────────────────────────────────────────────────────────────────────────────────────────────"

lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,TRAN,MODEL,RM 2>/dev/null | while read -r line; do
    echo "$line"
done

echo ""
echo -e "Detail per partitie met UUID:"
echo ""

# Toon details per partitie
sudo blkid 2>/dev/null | while read -r line; do
    device=$(echo "$line" | cut -d: -f1)
    rest=$(echo "$line" | cut -d: -f2-)
    
    # Filter boot partities eruit
    if echo "$device" | grep -q "nvme0n1"; then
        continue
    fi
    
    echo -e "Device: $device"
    echo "  $rest"
    
    # Probeer model/serial via udevadm
    parent=$(echo "$device" | sed 's/[0-9]*$//')
    if [ -b "$parent" ] && [ "$parent" != "$device" ]; then
        model=$(udevadm info --query=property --name="$parent" 2>/dev/null | grep "ID_MODEL=" | cut -d= -f2 || true)
        serial=$(udevadm info --query=property --name="$parent" 2>/dev/null | grep "ID_SERIAL_SHORT=" | cut -d= -f2 || true)
        if [ -n "$model" ]; then echo "  Model: $model"; fi
        if [ -n "$serial" ]; then echo "  Serial: $serial"; fi
    fi
    
    # Check mountpoint
    mp=$(findmnt -n -o TARGET "$device" 2>/dev/null || true)
    if [ -n "$mp" ]; then
        opts=$(findmnt -n -o OPTIONS "$device" 2>/dev/null || true)
        echo -e "  Mountpoint: $mp"
        if echo "$opts" | grep -q "\bro\b"; then
            echo -e "  Mount mode: READ-ONLY"
        else
            echo -e "  Mount mode: READ-WRITE"
        fi
    else
        echo "  Mountpoint: (niet gemount)"
    fi
    
    # Schat media type
    tran=$(udevadm info --query=property --name="$parent" 2>/dev/null | grep "ID_BUS=" | cut -d= -f2 || true)
    removable=$(cat /sys/block/$(basename $parent)/removable 2>/dev/null || echo "?")
    size_bytes=$(blockdev --getsize64 "$device" 2>/dev/null || echo "0")
    size_gb=$((size_bytes / 1073741824))
    
    media_type="unknown"
    if [ "$tran" = "usb" ]; then
        rotational=$(cat /sys/block/$(basename $parent)/queue/rotational 2>/dev/null || echo "?")
        if [ $size_gb -lt 64 ] && [ "$removable" = "1" ]; then
            media_type="usb_flash"
        elif [ "$rotational" = "1" ]; then
            media_type="usb_hdd"
        elif [ "$rotational" = "0" ]; then
            media_type="usb_ssd"
        else
            media_type="usb_hdd"
        fi
    fi
    
    echo "  Geschat type: $media_type"
    echo "  Grootte: ${size_gb}GB"
    echo ""
done

echo -e "=== Einde detectie ==="
