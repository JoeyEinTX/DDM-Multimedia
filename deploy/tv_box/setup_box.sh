#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# deploy/tv_box/setup_box.sh - the TV box's own settings, run ON the box
# -----------------------------------------------------------------------------
# deploy/tv_box.sh install (on DevPi) copies this to ~/.local/share/ddm-tv/ and runs
# it over `ssh -t`, so sudo can ask for the password here (the box keeps sudo's
# password: nothing here adds NOPASSWD or stores one). Running it again changes
# nothing that is already right.
#
#   setup_box.sh [--overlay on|off]
#
#   - Desktop with autologin (raspi-config, B4).
#   - Screen blanking off (raspi-config).
#   - HDMI0 (HDMI-A-1, the port next to the USB-C power) at 1920x1080, 60 Hz:
#     video= in /boot/firmware/cmdline.txt, which the next boot applies.
#   - chromium and wtype from apt when missing.
#   - ~/.config/autostart/ddm-tv-box.desktop: the launcher with the session.
#   - labwc's HideCursor keybind (Alt+Super+H) in ~/.config/labwc/rc.xml, added the
#     way DevPi's kiosk adds it (kiosk_autostart.rc_with_keybind).
#   - --overlay on|off: Raspberry Pi's read-only root (overlay file system), so a
#     yanked power cord can't corrupt the SD card. Off by default. It has to be off
#     to install: with it on, nothing written here would survive a reboot, so this
#     refuses until `--overlay off` and a reboot.
# -----------------------------------------------------------------------------
set -uo pipefail

DIR="$HOME/.local/share/ddm-tv"
OVERLAY=keep
case "${1:-}" in
    --overlay)
        case "${2:-}" in on|off) OVERLAY="$2" ;; *) echo "--overlay takes on or off" >&2; exit 2 ;; esac ;;
    "") ;;
    *) echo "usage: setup_box.sh [--overlay on|off]" >&2; exit 2 ;;
esac

ok()   { echo "  ok    $*"; }
act()  { echo "  set   $*"; }
warn() { echo "  WARN  $*"; }
REBOOT=no

overlay_now() {                   # 0 when the read-only root is active now
    grep -qw "boot=overlay" /proc/cmdline 2>/dev/null || grep -q " overlay / " /proc/mounts 2>/dev/null \
        || grep -q "^overlayroot / " /proc/mounts 2>/dev/null
}

echo "DDM TV box setup on $(hostname)"
if overlay_now && [ "$OVERLAY" != off ]; then
    echo "  STOP  the read-only root (overlay) is on: nothing written now would survive a reboot."
    echo "        Turn it off first: deploy/tv_box.sh install ... --overlay off, reboot the box, then install again."
    exit 3
fi

echo "sudo may ask for joey's password on the box:"
sudo -v || { echo "  STOP  sudo did not work"; exit 4; }

if [ "$OVERLAY" = off ]; then
    if command -v raspi-config >/dev/null 2>&1 && sudo raspi-config nonint disable_overlayfs; then
        act "read-only root off (from the next boot)"; REBOOT=yes
    else
        warn "could not turn the read-only root off (sudo raspi-config: Performance -> Overlay File System)"
    fi
    if overlay_now; then
        echo "  Reboot the box, then run the install again."
        exit 0
    fi
fi

if command -v raspi-config >/dev/null 2>&1; then
    sudo raspi-config nonint do_boot_behaviour B4 && ok "desktop with autologin" || warn "autologin not set (raspi-config: System -> Boot / Auto Login)"
    sudo raspi-config nonint do_blanking 1 && ok "screen blanking off" || warn "blanking not changed (raspi-config: Display -> Screen Blanking)"
else
    warn "no raspi-config: set desktop autologin and blanking off by hand"
fi

CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
WANT="video=HDMI-A-1:1920x1080@60D"
if [ -f "$CMDLINE" ]; then
    if grep -qw -- "$WANT" "$CMDLINE"; then
        ok "HDMI0 at 1920x1080 ($CMDLINE)"
    else
        sudo cp "$CMDLINE" "$CMDLINE.ddm-tv.bak" 2>/dev/null || true
        sudo sed -i -E '1 s/ ?video=HDMI-A-1:[^ ]*//g; 1 s/$/ '"$WANT"'/' "$CMDLINE" \
            && { act "HDMI0 at 1920x1080 ($CMDLINE, from the next boot)"; REBOOT=yes; } \
            || warn "could not write $CMDLINE"
    fi
else
    warn "no cmdline.txt: the TV gets its own preferred mode"
fi

for pkg in chromium wtype; do
    if command -v "$pkg" >/dev/null 2>&1 || { [ "$pkg" = chromium ] && command -v chromium-browser >/dev/null 2>&1; }; then
        ok "$pkg is there"
    else
        sudo apt-get install -y "$pkg" && act "$pkg installed" || warn "$pkg did not install (sudo apt install $pkg)"
    fi
done

mkdir -p "$HOME/.config/autostart"
sed "s#@HOME@#$HOME#g" "$DIR/ddm-tv-box.desktop" > "$HOME/.config/autostart/ddm-tv-box.desktop.new" \
    && mv "$HOME/.config/autostart/ddm-tv-box.desktop.new" "$HOME/.config/autostart/ddm-tv-box.desktop" \
    && ok "the launcher starts with the desktop (~/.config/autostart/ddm-tv-box.desktop)"
chmod +x "$DIR/ddm-tv-launch.sh" 2>/dev/null || true

RC="$HOME/.config/labwc/rc.xml"
mkdir -p "$(dirname "$RC")"
if python3 - "$DIR" "$RC" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import kiosk_autostart as k
from pathlib import Path
rc = Path(sys.argv[2])
old = rc.read_text(encoding="utf-8") if rc.exists() else None
new = k.rc_with_keybind(old)
if new is None:
    sys.exit(1)                       # a shape it can't add to: left to a person
if new != old:
    rc.write_text(new, encoding="utf-8")
    print("  set   the pointer-hiding keybind (Alt+Super+H) in ~/.config/labwc/rc.xml")
else:
    print("  ok    the pointer-hiding keybind is there")
PY
then :; else warn "rc.xml not changed: the pointer may show until the board covers it (cursor: none)"; fi

if [ "$OVERLAY" = on ]; then
    if command -v raspi-config >/dev/null 2>&1 && sudo raspi-config nonint enable_overlayfs; then
        act "read-only root on (from the next boot; --overlay off before installing again)"; REBOOT=yes
    else
        warn "could not turn the read-only root on (sudo raspi-config: Performance -> Overlay File System)"
    fi
fi

if [ "$REBOOT" = yes ]; then
    echo "REBOOT: settings that apply at boot changed; reboot the box (deploy/tv_box.sh install ... --reboot does it)."
fi
echo "Done on $(hostname)."
