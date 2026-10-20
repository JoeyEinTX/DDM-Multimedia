#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# deploy/tv_box.sh - put DDM's board on a TV box, from DevPi
# -----------------------------------------------------------------------------
# The TV board has its own box behind the TV (ddm-tv, a Raspberry Pi 5 on Pi OS with
# desktop) that does nothing but show DevPi's board full screen. The box runs no DDM
# code and needs no git or repo: this script, run on DevPi, copies the few files it
# needs over SSH and sets it up. Two boxes (Joey's three screens, 2026-10-19): the
# TV by the cups, ddm-tv, shows the La Quiniela page (/board, the default); the
# living room's TV, ddm-tv2, the slideshow that switches to the board for the race
# (--server http://joeydevpi.local:5001/living-room). DevPi's own kiosk (install_services.sh
# --kiosk) stays off; it is the fallback.
#
#   deploy/tv_box.sh install joey@ddm-tv.local [--server URL] [--overlay on|off] [--reboot] [--dry-run]
#   deploy/tv_box.sh status  joey@ddm-tv.local [--dry-run]
#
# install (run it again after every `git pull` on DevPi: it brings the box up to date):
#   - copies deploy/tv_box/{holding.html,ddm-tv-launch.sh,setup_box.sh,ddm-tv-box.desktop}
#     and deploy/kiosk_autostart.py to ~/.local/share/ddm-tv/ on the box;
#   - writes the one config file, ~/.config/ddm-tv/ddm-tv.conf: DDM_TV_URL, the board's
#     address. --server sets it; the default is DevPi as this script sees it,
#     http://<DevPi's hostname>.local:5001/board (the name, not the IP: the IP comes
#     from DHCP and can change; mDNS follows it. With a DHCP reservation for DevPi,
#     --server http://<its IP>:5001/board drops the dependence on mDNS);
#   - runs setup_box.sh on the box over `ssh -t`, so sudo can ask for the password there;
#   - restarts the box's launcher when its desktop is up; --reboot reboots the box
#     instead (needed after the first install: autologin and the HDMI mode apply at boot).
# status: the box's settings and what its TV shows, and DevPi's page revision beside it.
#
# The box logs with `journalctl -t ddm-tv -b`:  ssh joey@ddm-tv.local journalctl -t ddm-tv -b
# SSH logs in with DevPi's key (BatchMode: no password prompts but sudo's, in setup).
# -----------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOX_DIR="$HERE/tv_box"
FILES=("$BOX_DIR/holding.html" "$BOX_DIR/ddm-tv-launch.sh" "$BOX_DIR/setup_box.sh" "$BOX_DIR/ddm-tv-box.desktop"
       "$BOX_DIR/ddm_tv_outputs.py" "$HERE/kiosk_autostart.py")
REMOTE_DIR='.local/share/ddm-tv'
REMOTE_CONF_DIR='.config/ddm-tv'
SPLASH_PORT="${DDM_SPLASH_PORT:-5001}"

usage() {
    echo "usage: deploy/tv_box.sh install USER@HOST [--server URL] [--overlay on|off] [--reboot] [--dry-run]" >&2
    echo "       deploy/tv_box.sh status  USER@HOST [--dry-run]" >&2
    exit 2
}

ACTION="${1:-}"
HOST="${2:-}"
[ -n "$ACTION" ] || usage
case "$ACTION" in install|status) ;; -h|--help) usage ;; *) echo "unknown command: $ACTION" >&2; usage ;; esac
[ -n "$HOST" ] || { echo "$ACTION needs the box, e.g. joey@ddm-tv.local" >&2; exit 2; }
shift 2

SERVER=""
OVERLAY=""
REBOOT=no
DRY=no
while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="${2:-}"; [ -n "$SERVER" ] || { echo "--server needs a URL" >&2; exit 2; }; shift 2 ;;
        --overlay)
            case "${2:-}" in on|off) OVERLAY="$2" ;; *) echo "--overlay takes on or off" >&2; exit 2 ;; esac
            shift 2 ;;
        --reboot) REBOOT=yes; shift ;;
        --dry-run) DRY=yes; shift ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)
run() {                            # a command on DevPi's side, shown, not run with --dry-run
    if [ "$DRY" = yes ]; then printf '  would run: %s\n' "$*"; return 0; fi
    "$@"
}
on_box() { run ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

default_server() {
    local name
    name="$(hostname 2>/dev/null || echo devpi)"
    name="$(printf %s "${name%%.*}" | tr "[:upper:]" "[:lower:]")"
    printf 'http://%s.local:%s/board' "$name" "$SPLASH_PORT"
}

devpi_rev() {                      # what DevPi's splash serves now
    curl -fsS -D - -o /dev/null --max-time 4 "http://127.0.0.1:$SPLASH_PORT/api/page_rev" 2>/dev/null \
        | sed -n 's/^[Xx]-[Pp]age-[Rr]ev: *//p' | tr -d '\r'
}

if [ "$ACTION" = status ]; then
    echo "TV box $HOST"
    on_box "bash \$HOME/$REMOTE_DIR/ddm-tv-launch.sh --status" \
        || echo "  could not ask the box (is it on? ssh $HOST works?)"
    echo "DevPi serves:    $(devpi_rev || true)"
    echo "(the TV shows 'DDM TV - board <revision>': the same as DevPi's within a minute of a pull and restart)"
    exit 0
fi

# --- install ------------------------------------------------------------------------
[ -n "$SERVER" ] || SERVER="$(default_server)"
case "$SERVER" in http://*|https://*) ;; *) echo "--server must be a URL (http://...)" >&2; exit 2 ;; esac
for f in "${FILES[@]}"; do
    [ -f "$f" ] || { echo "missing $f (run this from the repo on DevPi)" >&2; exit 1; }
done

echo "TV box $HOST: the board at $SERVER"
if [ "$DRY" = no ] && ! ssh "${SSH_OPTS[@]}" "$HOST" true; then
    echo "  STOP  ssh $HOST does not log in with DevPi's key (RACE_NIGHT.md, the TV box: preparing it)" >&2
    exit 1
fi
on_box "mkdir -p \$HOME/$REMOTE_DIR \$HOME/$REMOTE_CONF_DIR" || exit 1
run scp -q "${SSH_OPTS[@]}" "${FILES[@]}" "$HOST:$REMOTE_DIR/" || { echo "  STOP  the files did not copy" >&2; exit 1; }
echo "  ok    files copied to ~/$REMOTE_DIR/"
CONF_TEXT="# The TV box's one setting, written by deploy/tv_box.sh on DevPi: the board's address.
DDM_TV_URL=$SERVER"
if [ "$DRY" = yes ]; then
    printf '  would write ~/%s/ddm-tv.conf:\n%s\n' "$REMOTE_CONF_DIR" "$CONF_TEXT"
else
    printf '%s\n' "$CONF_TEXT" | ssh "${SSH_OPTS[@]}" "$HOST" "cat > \$HOME/$REMOTE_CONF_DIR/ddm-tv.conf" \
        || { echo "  STOP  the config did not write" >&2; exit 1; }
fi
echo "  ok    ~/$REMOTE_CONF_DIR/ddm-tv.conf: DDM_TV_URL=$SERVER"

SETUP="bash \$HOME/$REMOTE_DIR/setup_box.sh${OVERLAY:+ --overlay $OVERLAY}"
if [ "$DRY" = yes ]; then
    printf '  would run: ssh -t %s %s\n' "$HOST" "$SETUP"
else
    ssh -t -o ConnectTimeout=8 "$HOST" "$SETUP"
    code=$?
    [ "$code" -eq 0 ] || { echo "  STOP  setup on the box ended with $code (above)" >&2; exit "$code"; }
fi

if [ "$REBOOT" = yes ]; then
    echo "  rebooting the box (sudo may ask for the password)"
    if [ "$DRY" = yes ]; then printf '  would run: ssh -t %s sudo reboot\n' "$HOST"; else ssh -t "$HOST" sudo reboot; fi
    exit 0
fi
# The launcher now, if the desktop is up: the old one stopped (its browser goes with
# it), a new one started in the session. Otherwise it starts with the next login.
RESTART='pkill -u "$(id -u)" -f ddm-tv-launch.sh; pkill -u "$(id -u)" -f -- "--user-data-dir=$HOME/.config/ddm-tv/chromium"; sleep 2;
sock=$(ls /run/user/$(id -u)/wayland-* 2>/dev/null | grep -v lock | head -n 1);
if [ -n "$sock" ]; then
  WAYLAND_DISPLAY=$(basename "$sock") XDG_RUNTIME_DIR=/run/user/$(id -u) nohup bash $HOME/.local/share/ddm-tv/ddm-tv-launch.sh >/dev/null 2>&1 &
  echo "  ok    the launcher restarted in the running desktop"
else
  echo "  note  no desktop session yet: the launcher starts at the next boot (reboot the box: --reboot)"
fi'
on_box "$RESTART"
echo "Installed. Status: deploy/tv_box.sh status $HOST    Logs: ssh $HOST journalctl -t ddm-tv -b"
