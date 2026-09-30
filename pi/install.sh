#!/bin/bash
# Brass Creature installer for a Raspberry Pi Zero W (Raspberry Pi OS Bookworm or Trixie).
#
#   sudo ./install.sh --dry-run    show what would change; change nothing
#   sudo ./install.sh              install
#   sudo ./install.sh --uninstall  undo everything this script did
#
# It refuses to run on the mesh base station (any Pi that has
# mesh-listen.service) unless you add --force. Use the second Pi.
#
# Every file it edits is copied to /var/backups/trill-creature/<time>/ first.
# Nothing is downloaded. A reboot is needed afterwards.
#
# NOT YET RUN ON A REAL PI. The file edits are tested against a copy of
# pizero's real config.txt (see test_install.sh); the modules and the service
# are untested.

set -eu

ROOT="${TRILL_ROOT:-}"          # test hook: pretend this directory is /
HERE="$(cd "$(dirname "$0")" && pwd)"
MODE=install
DRY=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --force) FORCE=1 ;;
        --uninstall) MODE=uninstall ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

MARK_BEGIN='# --- trill-creature begin ---'
MARK_END='# --- trill-creature end ---'
APP_DIR="$ROOT/opt/trill-creature"
UNIT="$ROOT/etc/systemd/system/trill-creature.service"
MODS="$ROOT/etc/modules-load.d/trill-creature.conf"
MODPROBE="$ROOT/etc/modprobe.d/trill-creature.conf"
GETHER="$ROOT/etc/modules-load.d/usb-gadget.conf"      # written by rpi-usb-gadget
GETHER_OFF="$GETHER.trill-disabled"
BACKUP="$ROOT/var/backups/trill-creature/$(date +%Y%m%d-%H%M%S)"

if [ -f "$ROOT/boot/firmware/config.txt" ]; then
    BOOT="$ROOT/boot/firmware"
else
    BOOT="$ROOT/boot"
fi
CONFIG="$BOOT/config.txt"
CMDLINE="$BOOT/cmdline.txt"

say()  { printf '%s\n' "$*"; }
plan() { if [ "$DRY" = 1 ]; then say "WOULD  $*"; else say "DOING  $*"; fi; }
run()  { if [ "$DRY" = 0 ]; then "$@"; fi; }

backup() {
    [ -f "$1" ] || return 0
    [ "$DRY" = 1 ] && return 0
    mkdir -p "$BACKUP"
    cp -p "$1" "$BACKUP/$(basename "$1")"
}

# Is this setting already active for a Pi Zero? Lines under [cm4], [pi5] and
# so on do not apply to it; lines under [all], [pi0], [pi0w] or no heading do.
config_has() {
    awk -v want="$1" '
        /^\[/ { sect = $0 }
        { line = $0; sub(/[ \t]+$/, "", line) }
        (sect == "" || sect == "[all]" || sect == "[pi0]" || sect == "[pi0w]") &&
            index(line, want) == 1 { found = 1 }
        END { exit found ? 0 : 1 }' "$CONFIG"
}

strip_block() {   # print file $1 without our marked block
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        $0 == b { skip = 1; next }
        $0 == e { skip = 0; next }
        !skip' "$1"
}

# ---------------------------------------------------------------- checks

if [ -z "$ROOT" ]; then
    if [ "$DRY" = 0 ] && [ "$(id -u)" -ne 0 ]; then
        say "Run this with sudo."; exit 1
    fi
    if ! grep -q 'Raspberry Pi' /proc/device-tree/model 2>/dev/null; then
        say "This is not a Raspberry Pi. Copy the folder to the Pi and run it there."; exit 1
    fi
fi
if [ ! -f "$CONFIG" ]; then
    say "Cannot find config.txt under $ROOT/boot. Is this Raspberry Pi OS?"; exit 1
fi
if [ "$MODE" = install ] && [ "$FORCE" = 0 ]; then
    if [ -e "$ROOT/etc/systemd/system/mesh-listen.service" ]; then
        say "STOP: this Pi runs mesh-listen.service. It is the mesh base station."
        say "Installing here would switch off its USB network link (ssh pizero-usb)"
        say "and needs a reboot, which drops the mesh radio link for a minute."
        say "Use the second Pi. (To install here anyway, add --force.)"
        exit 3
    fi
fi

# ---------------------------------------------------------------- uninstall

if [ "$MODE" = uninstall ]; then
    plan "stop and disable trill-creature.service"
    if [ -z "$ROOT" ]; then run systemctl disable --now trill-creature.service 2>/dev/null || true; fi
    plan "remove $UNIT, $APP_DIR, $MODS, $MODPROBE"
    run rm -f "$UNIT" "$MODS" "$MODPROBE"
    run rm -rf "$APP_DIR"
    if grep -qxF "$MARK_BEGIN" "$CONFIG"; then
        plan "remove the trill-creature block from $CONFIG"
        backup "$CONFIG"
        if [ "$DRY" = 0 ]; then strip_block "$CONFIG" > "$CONFIG.trill-tmp"; cat "$CONFIG.trill-tmp" > "$CONFIG"; rm -f "$CONFIG.trill-tmp"; fi
    fi
    if [ -f "$GETHER_OFF" ]; then
        plan "switch USB networking (g_ether) back on: restore $GETHER"
        run mv "$GETHER_OFF" "$GETHER"
    fi
    if [ -f "$CMDLINE" ] && grep -q 'modules-load=dwc2,g_midi' "$CMDLINE" && [ -f "$ROOT/var/lib/trill-creature/cmdline-was-g_ether" ]; then
        plan "put g_ether back in $CMDLINE"
        backup "$CMDLINE"
        if [ "$DRY" = 0 ]; then sed 's/modules-load=dwc2,g_midi/modules-load=dwc2,g_ether/' "$CMDLINE" > "$CMDLINE.trill-tmp"; cat "$CMDLINE.trill-tmp" > "$CMDLINE"; rm -f "$CMDLINE.trill-tmp"; fi
    fi
    run rm -rf "$ROOT/var/lib/trill-creature"
    if [ -z "$ROOT" ]; then run systemctl daemon-reload; fi
    say "Done. I2C was left as it is now; backups are in $ROOT/var/backups/trill-creature/. Reboot to finish: sudo reboot"
    exit 0
fi

# ---------------------------------------------------------------- install

say "Brass Creature installer. Boot files: $BOOT"

# 1. config.txt: switch on I2C and the USB "gadget" controller
NEED=""
if config_has 'dtparam=i2c_arm=on'; then
    say "OK     I2C is already switched on in config.txt"
else
    NEED="${NEED}dtparam=i2c_arm=on
"
fi
if config_has 'dtoverlay=dwc2'; then
    say "OK     the USB gadget controller (dwc2) is already switched on in config.txt"
else
    NEED="${NEED}dtoverlay=dwc2,dr_mode=peripheral
"
fi
if [ -n "$NEED" ]; then
    plan "add to the end of $CONFIG: $(printf '%s' "$NEED" | tr '\n' ' ')"
    backup "$CONFIG"
    if [ "$DRY" = 0 ]; then
        strip_block "$CONFIG" > "$CONFIG.trill-tmp"
        if [ -n "$(tail -c 1 "$CONFIG.trill-tmp")" ]; then echo >> "$CONFIG.trill-tmp"; fi
        printf '%s\n[all]\n%s%s\n' "$MARK_BEGIN" "$NEED" "$MARK_END" >> "$CONFIG.trill-tmp"
        cat "$CONFIG.trill-tmp" > "$CONFIG"      # keep the FAT file's own inode
        rm -f "$CONFIG.trill-tmp"
    fi
fi

# 2. load the I2C device driver and the USB MIDI gadget at boot
plan "write $MODS (loads i2c-dev and g_midi at boot)"
run mkdir -p "$(dirname "$MODS")"
if [ "$DRY" = 0 ]; then printf 'i2c-dev\ng_midi\n' > "$MODS"; fi

plan "write $MODPROBE (the Mac will list the Pi as 'Creature')"
run mkdir -p "$(dirname "$MODPROBE")"
if [ "$DRY" = 0 ]; then printf 'options g_midi iManufacturer=BrassCreature iProduct=Creature\n' > "$MODPROBE"; fi

# 3. only one USB gadget can own the port. Switch the network gadget off.
if [ -f "$GETHER" ]; then
    plan "switch USB networking off: rename $GETHER to $(basename "$GETHER_OFF") (ssh over the USB cable stops; ssh over Wi-Fi is unchanged)"
    run mv "$GETHER" "$GETHER_OFF"
fi
if [ -f "$CMDLINE" ] && grep -q 'modules-load=dwc2,g_ether' "$CMDLINE"; then
    plan "replace g_ether with g_midi in $CMDLINE"
    backup "$CMDLINE"
    if [ "$DRY" = 0 ]; then
        mkdir -p "$ROOT/var/lib/trill-creature"; : > "$ROOT/var/lib/trill-creature/cmdline-was-g_ether"
        sed 's/modules-load=dwc2,g_ether/modules-load=dwc2,g_midi/' "$CMDLINE" > "$CMDLINE.trill-tmp"
        cat "$CMDLINE.trill-tmp" > "$CMDLINE"; rm -f "$CMDLINE.trill-tmp"
    fi
fi
if [ -f "$ROOT/etc/modules" ] && grep -q '^g_ether' "$ROOT/etc/modules"; then
    say "NOTE   /etc/modules also loads g_ether. Remove that line by hand, or g_midi will not get the port."
fi

# 4. the program and its service
plan "copy creature.py to $APP_DIR/"
run mkdir -p "$APP_DIR"
run cp "$HERE/creature.py" "$APP_DIR/creature.py"
run chmod 755 "$APP_DIR/creature.py"

plan "write $UNIT and enable it (starts at every boot, runs as root)"
run mkdir -p "$(dirname "$UNIT")"
if [ "$DRY" = 0 ]; then
    cat > "$UNIT" <<'EOF'
[Unit]
Description=Brass Creature: Trill Craft to USB MIDI
After=systemd-modules-load.service

[Service]
ExecStart=/usr/bin/python3 -u /opt/trill-creature/creature.py
StateDirectory=trill-creature
Restart=always
RestartSec=2
Nice=-5

[Install]
WantedBy=multi-user.target
EOF
fi
if [ -z "$ROOT" ]; then
    run systemctl daemon-reload
    run systemctl enable trill-creature.service
fi

if [ "$DRY" = 1 ]; then
    say "Dry run only. Nothing was changed."
else
    say "Installed. Reboot to finish:  sudo reboot"
    say "After the reboot, see what it is doing with:  journalctl -u trill-creature -n 20"
fi
