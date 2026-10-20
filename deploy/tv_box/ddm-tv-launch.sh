#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# deploy/tv_box/ddm-tv-launch.sh - the TV box: DDM's board full screen, for ever
# -----------------------------------------------------------------------------
# Installed on the TV box (ddm-tv) by deploy/tv_box.sh, run on DevPi, into
# ~/.local/share/ddm-tv/, and started with the desktop session by
# ~/.config/autostart/ddm-tv-box.desktop. The box runs no DDM code: Chromium shows
# the box's own holding.html (from the SD card), which holds DevPi's board in a
# frame and shows "Waiting for DDM..." whenever the board can't be reached.
#
#   - One launcher at a time (a lock in ~/.config/ddm-tv/).
#   - The board's address: DDM_TV_URL in ~/.config/ddm-tv/ddm-tv.conf, the one
#     config file, written by the installer.
#   - Screen blanking off: the session's swayidle (Pi OS's Screen Blanking) is
#     stopped now and every 30 s.
#   - The screen: whichever HDMI port it is on, at 1920x1080 when it offers that,
#     else its own preferred mode (ddm_tv_outputs.py, from `wlr-randr --json`).
#     Nothing is forced at boot; every 5 s the launcher looks again, and when a
#     screen comes, goes or is switched on later it sets it and starts the browser
#     again on it. What it chose is in ~/.config/ddm-tv/screen (for --status).
#   - The pointer off: the board and the holding page draw none (cursor: none),
#     and Alt+Super+H, labwc's HideCursor keybind the installer puts in
#     ~/.config/labwc/rc.xml, is pressed with wtype once the browser is up.
#   - Chromium crashed, killed or closed: started again 3 s later, for ever.
#   - Logs: `journalctl -t ddm-tv -b` (logger) on the box.
#
#   ddm-tv-launch.sh            the TV (what the autostart runs)
#   ddm-tv-launch.sh --status   what the box shows, for deploy/tv_box.sh status
#   ddm-tv-launch.sh --command  the browser's command line, nothing started (tests)
# -----------------------------------------------------------------------------

# No -e: nothing that fails may keep the browser off the TV.
set -uo pipefail

DIR="${DDM_TV_DIR:-$HOME/.local/share/ddm-tv}"
CONF_DIR="${DDM_TV_CONF_DIR:-$HOME/.config/ddm-tv}"
CONF="$CONF_DIR/ddm-tv.conf"
PROFILE="$CONF_DIR/chromium"
DEBUG_PORT=9222                    # localhost only: the status reads the page's title from it
SCREEN_FILE="$CONF_DIR/screen"     # the output and mode in use, written by the screen watcher
HIDE_POINTER=(-M alt -M logo -k h -m logo -m alt)

log() {
    echo "ddm-tv: $*" >&2
    logger -t ddm-tv -- "$*" 2>/dev/null || true
}

conf_url() {                       # DDM_TV_URL from the config file, read, never sourced
    [ -f "$CONF" ] || return 0
    sed -n 's/^DDM_TV_URL=//p' "$CONF" | tail -n 1 | tr -d '\r' | sed "s/^[\"']//; s/[\"']\$//"
}

urlencode() {                      # for the board's address in the holding page's query
    local s="$1" out="" c i
    for ((i = 0; i < ${#s}; i++)); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9.~_-]) out+="$c" ;;
            *) out+="$(printf '%%%02X' "'$c")" ;;
        esac
    done
    printf '%s' "$out"
}

browser_bin() {
    local b
    for b in "${CHROMIUM_BIN:-}" chromium chromium-browser; do
        [ -n "$b" ] && command -v "$b" >/dev/null 2>&1 && { echo "$b"; return 0; }
    done
    return 1
}

page_url() {
    printf 'file://%s/holding.html?board=%s' "$DIR" "$(urlencode "$(conf_url)")"
}

# Chosen for a TV nobody touches: full screen, no first-run, no restore bubble, no
# translate bar, no error dialogs, no keyring prompt (Trixie stops at one on autologin:
# --password-store=basic), nothing kept between runs (--incognito).
browser_args() {
    printf '%s\n' \
        --kiosk \
        --start-fullscreen \
        --window-position=0,0 \
        "--user-data-dir=$PROFILE" \
        --password-store=basic \
        --incognito \
        --noerrdialogs \
        --disable-infobars \
        --disable-session-crashed-bubble \
        --disable-features=Translate,InfiniteSessionRestore,AutofillServerCommunication \
        --disable-translate \
        --no-first-run \
        --no-default-browser-check \
        --check-for-update-interval=31536000 \
        --overscroll-history-navigation=0 \
        --disable-pinch \
        --hide-scrollbars \
        --autoplay-policy=no-user-gesture-required \
        "--remote-debugging-port=$DEBUG_PORT" \
        "$(page_url)"
}

server_base() {                   # http://host:port of the board's address
    printf '%s' "$1" | sed -E 's|^(https?://[^/?]+).*|\1|'
}

# The screen: choose the output and mode (ddm_tv_outputs.py) and set them when they
# are not set that way. Prints the choice ("HDMI-A-2 1024x600@60.000" or "none").
screen_once() {
    local plan choice state
    if ! command -v wlr-randr >/dev/null 2>&1; then
        echo "unknown (no wlr-randr)"
        return 0
    fi
    plan="$( { wlr-randr --json 2>/dev/null || wlr-randr 2>/dev/null; } | python3 "$DIR/ddm_tv_outputs.py" 2>/dev/null)"
    choice="$(printf '%s\n' "$plan" | sed -n 1p)"
    state="$(printf '%s\n' "$plan" | sed -n 2p)"
    if [ "$state" = change ]; then
        local args=()
        mapfile -t args < <(printf '%s\n' "$plan" | tail -n +3)
        wlr-randr "${args[@]}" >/dev/null 2>&1 || log "wlr-randr could not set ${args[*]}"
    fi
    echo "${choice:-none}"
}

status() {
    local url base showing
    url="$(conf_url)"
    base="$(server_base "$url")"
    echo "board address:   ${url:-(none: deploy/tv_box.sh install)}"
    if [ -n "$url" ]; then
        if curl -fsS -o /dev/null --max-time 4 "$base/api/page_rev" 2>/dev/null; then
            echo "DevPi answers:   yes"
            echo "DevPi serves:    $(curl -fsS -D - -o /dev/null --max-time 4 "$base/api/page_rev" 2>/dev/null \
                                     | sed -n 's/^[Xx]-[Pp]age-[Rr]ev: *//p' | tr -d '\r')"
        else
            echo "DevPi answers:   no"
        fi
    fi
    if [ -s "$SCREEN_FILE" ]; then
        echo "screen:          $(LC_ALL=C tr -cd '\11\12\40-\176' < "$SCREEN_FILE" | head -n 1)"
    else
        echo "screen:          (not chosen yet: no screen, or the launcher has not run)"
    fi
    if pgrep -u "$(id -u)" -f -- "--user-data-dir=$PROFILE" >/dev/null 2>&1; then
        echo "Chromium:        running"
        showing="$(curl -fsS --max-time 3 "http://127.0.0.1:$DEBUG_PORT/json/list" 2>/dev/null \
                   | sed -n 's/.*"title": *"\(DDM TV[^"]*\)".*/\1/p' | head -n 1)"
        echo "the TV shows:    $(printf '%s' "${showing:-(unknown)}" | LC_ALL=C tr -cd '\40-\176')"
    else
        echo "Chromium:        not running"
    fi
    if pgrep -u "$(id -u)" -f "ddm-tv-launch.sh" >/dev/null 2>&1; then
        echo "launcher:        running"
    else
        echo "launcher:        not running (it starts with the desktop: reboot the box)"
    fi
    echo "logs:            journalctl -t ddm-tv -b"
}

case "${1:-}" in
    --status) status; exit 0 ;;
    --command)
        b="$(browser_bin || echo chromium)"
        printf '%s\n' "$b"
        browser_args
        exit 0 ;;
    "") ;;
    *) echo "usage: ddm-tv-launch.sh [--status | --command]" >&2; exit 2 ;;
esac

mkdir -p "$CONF_DIR" "$PROFILE"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$CONF_DIR/launch.lock"
    if ! flock -n 9; then
        log "another launcher is running"
        exit 0
    fi
fi
log "started; the board is at ${DDM_TV_URL:-$(conf_url)}"
[ -n "$(conf_url)" ] || log "no DDM_TV_URL in $CONF: the TV says so (deploy/tv_box.sh install)"

# Screen blanking off for as long as the launcher runs.
(
    while kill -0 "$$" 2>/dev/null; do
        pkill -u "$(id -u)" -x swayidle >/dev/null 2>&1 || true
        sleep 30
    done
) 9>&- &

# The screen, before the browser starts, and then every 5 s: a screen plugged into
# the other port, switched on after the box, or swapped for another gets its mode and
# the browser again (the supervisor below starts it on the screen that is there).
chosen="$(screen_once)"
printf '%s (since %s)\n' "$chosen" "$(date '+%H:%M:%S')" > "$SCREEN_FILE"
log "screen: $chosen"
(
    last="$chosen"
    while kill -0 "$$" 2>/dev/null; do
        sleep 5
        now="$(screen_once)"
        if [ "$now" != "$last" ]; then
            printf '%s (since %s)\n' "$now" "$(date '+%H:%M:%S')" > "$SCREEN_FILE"
            log "screen: $now (was $last)"
            if [ "$now" != none ]; then
                pkill -u "$(id -u)" -f -- "--user-data-dir=$PROFILE" >/dev/null 2>&1 || true
            fi
            last="$now"
        fi
    done
) 9>&- &

BROWSER="$(browser_bin)" || { log "no chromium in PATH (sudo apt install chromium)"; exit 1; }
while true; do
    # A run that ended badly leaves "restore pages?" behind: say it ended well.
    for prefs in "$PROFILE/Default/Preferences"; do
        [ -f "$prefs" ] || continue
        sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"Crashed"/"exit_type":"Normal"/' "$prefs" || true
    done
    mapfile -t ARGS < <(browser_args)
    log "starting $BROWSER on $(page_url)"
    "$BROWSER" "${ARGS[@]}" >/dev/null 2>&1 9>&- &
    pid=$!
    (
        sleep 6
        if [ -n "${WAYLAND_DISPLAY:-}" ] && command -v wtype >/dev/null 2>&1; then
            wtype "${HIDE_POINTER[@]}" || log "wtype could not press Alt+Super+H"
        fi
    ) 9>&- &
    wait "$pid"
    code=$?
    log "chromium ended (exit $code): starting it again in 3 s"
    sleep 3
done
