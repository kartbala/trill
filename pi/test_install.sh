#!/bin/bash
# Tests install.sh against pretend Pi file systems. Run on any computer:
#     bash test_install.sh
# It proves the file edits only. It cannot prove that the modules load or that
# the service starts on a real Pi.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $*"; exit 1; }
pass() { echo "ok   $*"; }

# The config.txt that Raspberry Pi OS Trixie Lite ships, as read from pizero on 2026-09-30.
stock_config() {
cat <<'CFG'
# For more options and information see
# http://rptl.io/configtxt

# Uncomment some or all of these to enable the optional hardware interfaces
#dtparam=i2c_arm=on
#dtparam=i2s=on
#dtparam=spi=on

# Enable audio (loads snd_bcm2835)
dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
disable_overscan=1
arm_boost=1

[cm4]
otg_mode=1

[cm5]
dtoverlay=dwc2,dr_mode=host

[pi5]
dtoverlay=nospi10

[all]
CFG
}

mkroot() {   # $1 = name
    R="$TMP/$1"
    mkdir -p "$R/boot/firmware" "$R/etc/modules-load.d" "$R/etc/systemd/system"
    stock_config > "$R/boot/firmware/config.txt"
    echo 'console=serial0,115200 console=tty1 root=PARTUUID=00000000-02 rootfstype=ext4 rootwait' > "$R/boot/firmware/cmdline.txt"
    echo "$R"
}

# 1. a fresh Pi: both lines get added, inside [all], once
R=$(mkroot fresh)
cp "$R/boot/firmware/config.txt" "$TMP/fresh.orig"
TRILL_ROOT="$R" bash "$HERE/install.sh" --dry-run > "$TMP/dry.txt"
cmp -s "$R/boot/firmware/config.txt" "$TMP/fresh.orig" || fail "dry run changed config.txt"
[ ! -e "$R/opt/trill-creature" ] || fail "dry run created files"
grep -q 'Nothing was changed' "$TMP/dry.txt" || fail "dry run did not say so"
pass "dry run changes nothing"

TRILL_ROOT="$R" bash "$HERE/install.sh" > /dev/null
tail -5 "$R/boot/firmware/config.txt" | tr '\n' '|' | grep -qF '# --- trill-creature begin ---|[all]|dtparam=i2c_arm=on|dtoverlay=dwc2,dr_mode=peripheral|# --- trill-creature end ---|' || fail "block not written as expected"
[ "$(cat "$R/etc/modules-load.d/trill-creature.conf" | tr '\n' ' ')" = "i2c-dev g_midi " ] || fail "modules-load file"
grep -q '^options g_midi ' "$R/etc/modprobe.d/trill-creature.conf" || fail "modprobe file"
cmp -s "$HERE/creature.py" "$R/opt/trill-creature/creature.py" || fail "program not copied"
grep -q 'ExecStart=/usr/bin/python3 -u /opt/trill-creature/creature.py' "$R/etc/systemd/system/trill-creature.service" || fail "unit"
ls "$R"/var/backups/trill-creature/*/config.txt > /dev/null || fail "no backup of config.txt"
pass "fresh Pi: config block, modules, program, unit, backup"

cp "$R/boot/firmware/config.txt" "$TMP/once.txt"
TRILL_ROOT="$R" bash "$HERE/install.sh" > /dev/null
cmp -s "$R/boot/firmware/config.txt" "$TMP/once.txt" || fail "second run changed config.txt again"
pass "running it twice adds nothing twice"

TRILL_ROOT="$R" bash "$HERE/install.sh" --uninstall > /dev/null
cmp -s "$R/boot/firmware/config.txt" "$TMP/fresh.orig" || fail "uninstall did not restore config.txt byte for byte"
[ ! -e "$R/opt/trill-creature" ] && [ ! -e "$R/etc/systemd/system/trill-creature.service" ] && [ ! -e "$R/etc/modules-load.d/trill-creature.conf" ] || fail "uninstall left files"
pass "uninstall restores config.txt byte for byte and removes its files"

# 2. a Pi set up like pizero2's image: rpi-usb-gadget already on
R=$(mkroot gadget)
echo 'dtoverlay=dwc2,dr_mode=peripheral' >> "$R/boot/firmware/config.txt"
echo 'g_ether' > "$R/etc/modules-load.d/usb-gadget.conf"
TRILL_ROOT="$R" bash "$HERE/install.sh" > "$TMP/gadget.txt"
[ "$(grep -c '^dtoverlay=dwc2,dr_mode=peripheral' "$R/boot/firmware/config.txt")" = 1 ] || fail "dwc2 overlay duplicated"
grep -q '^dtparam=i2c_arm=on' "$R/boot/firmware/config.txt" || fail "i2c not added"
[ ! -e "$R/etc/modules-load.d/usb-gadget.conf" ] && [ -e "$R/etc/modules-load.d/usb-gadget.conf.trill-disabled" ] || fail "g_ether not switched off"
TRILL_ROOT="$R" bash "$HERE/install.sh" --uninstall > /dev/null
[ "$(cat "$R/etc/modules-load.d/usb-gadget.conf")" = g_ether ] || fail "g_ether not restored"
pass "rpi-usb-gadget Pi: no duplicate overlay, g_ether switched off and restored"

# 3. a hand-rolled g_ether in cmdline.txt
R=$(mkroot cmdline)
echo 'console=tty1 root=PARTUUID=00000000-02 rootwait modules-load=dwc2,g_ether quiet' > "$R/boot/firmware/cmdline.txt"
TRILL_ROOT="$R" bash "$HERE/install.sh" > /dev/null
grep -q 'modules-load=dwc2,g_midi quiet' "$R/boot/firmware/cmdline.txt" || fail "cmdline not switched to g_midi"
[ "$(wc -l < "$R/boot/firmware/cmdline.txt" | tr -d ' ')" = 1 ] || fail "cmdline.txt must stay one line"
TRILL_ROOT="$R" bash "$HERE/install.sh" --uninstall > /dev/null
grep -q 'modules-load=dwc2,g_ether quiet' "$R/boot/firmware/cmdline.txt" || fail "cmdline not restored"
pass "cmdline.txt g_ether is swapped and restored"

# 4. the mesh base station is refused
R=$(mkroot mesh)
touch "$R/etc/systemd/system/mesh-listen.service"
cp "$R/boot/firmware/config.txt" "$TMP/mesh.orig"
if TRILL_ROOT="$R" bash "$HERE/install.sh" > "$TMP/mesh.txt"; then fail "installed on the mesh Pi"; fi
grep -q 'mesh base station' "$TMP/mesh.txt" || fail "no explanation"
cmp -s "$R/boot/firmware/config.txt" "$TMP/mesh.orig" || fail "mesh Pi was touched"
[ ! -e "$R/opt/trill-creature" ] || fail "mesh Pi was touched"
pass "refuses the mesh base station and touches nothing"

# 5. I2C switched on the usual way (uncommented line) is recognised
R=$(mkroot i2c)
sed 's/^#dtparam=i2c_arm=on/dtparam=i2c_arm=on/' "$TMP/fresh.orig" > "$R/boot/firmware/config.txt"
TRILL_ROOT="$R" bash "$HERE/install.sh" > /dev/null
[ "$(grep -c '^dtparam=i2c_arm=on' "$R/boot/firmware/config.txt")" = 1 ] || fail "i2c line duplicated"
pass "existing I2C setting is left alone"

echo "all install tests passed"
