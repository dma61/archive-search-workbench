#!/usr/bin/env bash
# mount_readonly.sh — Mount extern medium read-only onder /mnt/archive-ingest/
# Gebruik: ./mount_readonly.sh /dev/sdX1 ARCHIVE-DISK-001
# Of: ./mount_readonly.sh --remount ARCHIVE-DISK-001 (hermont bestaande mount als ro)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
MOUNT_BASE="/mnt/archive-ingest"
LOG_FILE="$LOG_DIR/mount_$(date +%Y%m%d-%H%M).log"

GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[1;33m"
NC="\033[0m"

log() { echo -e "$(date +%H:%M:%S) $1" | tee -a "$LOG_FILE"; }
ok()  { log "✅ $1"; }
err() { log "❌ $1"; }
warn(){ log "⚠️  $1"; }

usage() {
    echo "Gebruik:"
    echo "  $0 /dev/sdX1 ARCHIVE-LABEL"
    echo "  $0 --remount-existing /dev/sdX1 ARCHIVE-LABEL"
    echo "  $0 --unmount ARCHIVE-LABEL"
    echo ""
    echo "Opties:"
    echo "  --remount-existing  Unmount bestaande rw mount, hermount als ro"
    echo "  --unmount           Veilig unmounten van een label"
}

unmount_label() {
    local label="$1"
    local mountpoint="$MOUNT_BASE/$label"
    
    if mountpoint -q "$mountpoint" 2>/dev/null; then
        log "Unmounten van $mountpoint..."
        sudo umount "$mountpoint"
        ok "Unmount succesvol: $mountpoint"
    else
        warn "$mountpoint is niet gemount"
    fi
}

mount_readonly() {
    local device="$1"
    local label="$2"
    local mountpoint="$MOUNT_BASE/$label"
    
    # Validaties
    if [ ! -b "$device" ]; then
        err "Device $device bestaat niet of is geen block device"
        exit 1
    fi
    
    # Detecteer filesystem type
    local fstype
    fstype=$(sudo blkid -s TYPE -o value "$device" 2>/dev/null || true)
    if [ -z "$fstype" ]; then
        err "Kan filesystem type niet detecteren voor $device"
        exit 1
    fi
    log "Filesystem: $fstype"
    
    # Maak mountpoint
    sudo mkdir -p "$mountpoint"
    
    # Check of device al ergens gemount is
    local existing_mount
    existing_mount=$(findmnt -n -o TARGET "$device" 2>/dev/null | head -1 || true)
    if [ -n "$existing_mount" ]; then
        warn "Device $device is al gemount op $existing_mount"
        
        # Check of het al read-only is
        local opts
        opts=$(findmnt -n -o OPTIONS "$device" 2>/dev/null | head -1 || true)
        if echo "$opts" | grep -q "\bro\b"; then
            ok "Al read-only gemount op $existing_mount"
            
            # Bind mount naar onze locatie als het niet al daar is
            if [ "$existing_mount" != "$mountpoint" ]; then
                log "Bind mount naar $mountpoint..."
                sudo mount --bind "$existing_mount" "$mountpoint"
                sudo mount -o remount,ro,bind "$mountpoint"
                ok "Bind mount read-only naar $mountpoint"
            fi
            return 0
        fi
        
        # Gemount als rw — we moeten het remounten
        warn "Huidige mount is READ-WRITE — wordt hermont als read-only"
        log "Unmounten van $existing_mount..."
        sudo umount "$existing_mount" 2>/dev/null || {
            warn "Kan $existing_mount niet unmounten, probeer lazy unmount..."
            sudo umount -l "$existing_mount"
        }
    fi
    
    # Mount opties per filesystem type
    local mount_opts="ro,noexec,nosuid,nodev"
    case "$fstype" in
        ntfs|fuseblk)
            # ntfs-3g met uid/gid van huidige gebruiker voor leesrechten
            local uid=$(id -u)
            local gid=$(id -g)
            mount_opts="ro,noexec,nosuid,nodev,uid=$uid,gid=$gid,dmask=0022,fmask=0133"
            sudo mount -t ntfs-3g -o "$mount_opts" "$device" "$mountpoint"
            ;;
        exfat)
            local uid=$(id -u)
            local gid=$(id -g)
            mount_opts="ro,noexec,nosuid,nodev,uid=$uid,gid=$gid"
            sudo mount -t exfat -o "$mount_opts" "$device" "$mountpoint"
            ;;
        vfat|fat32)
            local uid=$(id -u)
            local gid=$(id -g)
            mount_opts="ro,noexec,nosuid,nodev,uid=$uid,gid=$gid"
            sudo mount -t vfat -o "$mount_opts" "$device" "$mountpoint"
            ;;
        ext4|ext3|ext2)
            sudo mount -o "$mount_opts" "$device" "$mountpoint"
            ;;
        *)
            warn "Onbekend filesystem $fstype — probeer generieke mount"
            sudo mount -o "$mount_opts" "$device" "$mountpoint"
            ;;
    esac
    
    # Verificatie: IS het echt read-only?
    local verify_opts
    verify_opts=$(findmnt -n -o OPTIONS "$mountpoint" 2>/dev/null || true)
    if echo "$verify_opts" | grep -q "\bro\b"; then
        ok "Gemount als READ-ONLY op $mountpoint"
    else
        err "WAARSCHUWING: Mount is NIET read-only! Unmounten voor veiligheid..."
        sudo umount "$mountpoint"
        exit 1
    fi
    
    # Schrijftest (moet falen)
    if sudo touch "$mountpoint/.write_test" 2>/dev/null; then
        err "KRITIEK: Schrijven naar read-only mount lukte! Unmounten..."
        sudo rm -f "$mountpoint/.write_test"
        sudo umount "$mountpoint"
        exit 1
    else
        ok "Schrijfbeveiliging geverifieerd (schrijven mislukt zoals verwacht)"
    fi
    
    log "Timestamp: $(date -Iseconds)"
    log "Device: $device"
    log "Label: $label"
    log "Mountpoint: $mountpoint"
    log "Filesystem: $fstype"
    log "Mode: read-only"
}

# --- Main ---
if [ $# -lt 1 ]; then
    usage
    exit 1
fi

case "$1" in
    --unmount)
        [ $# -lt 2 ] && { usage; exit 1; }
        unmount_label "$2"
        ;;
    --help|-h)
        usage
        ;;
    *)
        [ $# -lt 2 ] && { usage; exit 1; }
        mount_readonly "$1" "$2"
        ;;
esac
