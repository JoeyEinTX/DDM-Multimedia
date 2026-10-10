#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# DDM Splash Display — the TV kiosk
# -----------------------------------------------------------------------------
# Chromium full screen on the splash's page, for the TV. DevPi starts it with the
# desktop session: ~/.config/autostart/ddm-tv.desktop, written by
# deploy/install_services.sh. By hand, after `pkill -f chromium`:
#     ~/DDM-Multimedia/splash_display/deploy/kiosk.sh &
#
#   - Waits for the splash (http://localhost:5001/, up to 2 minutes), then starts
#     the browser whatever happened, so the TV always shows something (the page
#     keeps trying the splash on its own).
#   - Screen blanking off for the session: X11's xset; on Wayland (labwc) the
#     session's swayidle, which Pi OS's Screen Blanking setting starts, is stopped.
#   - The pointer off: on labwc, Alt+Super+H, a keybind deploy/kiosk_autostart.py
#     puts in ~/.config/labwc/rc.xml (HideCursor, and the pointer to the corner),
#     pressed once with wtype when the browser is up; on X11, unclutter if there.
#   - Once: with the kiosk's browser already running it does nothing.
#
# SPLASH_URL     the page (default http://localhost:5001/display; ...?look=impact)
# CHROMIUM_BIN   the browser (default chromium, then chromium-browser)
# -----------------------------------------------------------------------------

# No -e: a helper that is missing or fails must never keep the browser off the TV.
set -uo pipefail

URL="${SPLASH_URL:-http://localhost:5001/display}"
WAIT_URL="${SPLASH_WAIT_URL:-http://localhost:5001/}"
WAIT_S="${SPLASH_WAIT_S:-120}"
PROFILE_DIR="${HOME}/.config/chromium-kiosk"
# The keybind in ~/.config/labwc/rc.xml (deploy/kiosk_autostart.py): Alt+Super+H.
HIDE_POINTER=(-M alt -M logo -k h -m logo -m alt)

log() { echo "kiosk.sh: $*" >&2; }

# --- Once -------------------------------------------------------------------------
# One kiosk.sh at a time (two autostarts at once would open two windows): a lock held
# until its browser exits. A copy started just after `pkill -f chromium` waits 5 s for it.
mkdir -p "$PROFILE_DIR"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$PROFILE_DIR.lock"
    if ! flock -w 5 9; then
        log "another kiosk.sh is running"
        exit 0
    fi
fi
running() { pgrep -u "$(id -u)" -f -- "--user-data-dir=$PROFILE_DIR" >/dev/null 2>&1; }
for _ in 1 2 3 4 5; do            # a browser just told to quit gets 5 s to go
    running || break
    sleep 1
done
if running; then
    log "the kiosk's browser is already running"
    exit 0
fi

# --- Wait for the splash ------------------------------------------------------------
splash_up() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -o /dev/null --max-time 3 "$WAIT_URL"
    else
        python3 -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3)' "$WAIT_URL"
    fi
}
waited=0
until splash_up >/dev/null 2>&1; do
    if [ "$waited" -ge "$WAIT_S" ]; then
        log "the splash did not answer at $WAIT_URL in ${WAIT_S} s: starting the browser anyway"
        break
    fi
    sleep 2
    waited=$((waited + 2))
done

# --- Screen blanking off -------------------------------------------------------------
if command -v xset >/dev/null 2>&1; then
    xset s off      || true
    xset s noblank  || true
    xset -dpms      || true
fi
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    # The session's autostart may start swayidle after this script: stop it now and
    # through the next minute.
    ( for _ in $(seq 1 30); do pkill -u "$(id -u)" -x swayidle; sleep 2; done ) >/dev/null 2>&1 9>&- &
fi

# --- The browser -------------------------------------------------------------------
CHROMIUM="${CHROMIUM_BIN:-}"
if [ -z "$CHROMIUM" ]; then
    for candidate in chromium chromium-browser; do
        if command -v "$candidate" >/dev/null 2>&1; then
            CHROMIUM="$candidate"
            break
        fi
    done
fi
if [ -z "$CHROMIUM" ]; then
    log "no chromium binary found in PATH"
    exit 1
fi

# Clear any previous "session crashed" / "restore tabs" prompts.
mkdir -p "$PROFILE_DIR"
PREF_FILE="$PROFILE_DIR/Default/Preferences"
if [ -f "$PREF_FILE" ]; then
    sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' "$PREF_FILE" || true
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' "$PREF_FILE" || true
fi

# Flags chosen to keep the display clean for an unattended TV:
#   --kiosk                     fullscreen, no chrome
#   --noerrdialogs              suppress error dialogs
#   --disable-infobars          no "you are using an unsupported flag" nag
#   --no-first-run              skip the welcome wizard
#   --disable-translate         no translate prompt on foreign-language pages
#   --disable-features=...      kill the session-restore bubble + autofill nags
#   --start-fullscreen          some Wayland builds need this in addition to --kiosk
#   --window-position=0,0       belt-and-suspenders for multi-monitor weirdness
#   --check-for-update-interval=...  long enough that we never see the update bar
"$CHROMIUM" \
    --kiosk \
    --start-fullscreen \
    --window-position=0,0 \
    --user-data-dir="$PROFILE_DIR" \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=Translate,InfiniteSessionRestore,AutofillServerCommunication \
    --disable-translate \
    --no-first-run \
    --no-default-browser-check \
    --check-for-update-interval=31536000 \
    --overscroll-history-navigation=0 \
    --autoplay-policy=no-user-gesture-required \
    --hide-scrollbars \
    --incognito \
    "$URL" 9>&- &
BROWSER=$!

# --- The pointer off ---------------------------------------------------------------
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    if command -v wtype >/dev/null 2>&1; then
        sleep 5
        wtype "${HIDE_POINTER[@]}" || log "wtype could not press Alt+Super+H: the pointer stays"
    else
        log "no wtype (sudo apt install wtype): the pointer stays on the TV"
    fi
elif command -v unclutter >/dev/null 2>&1; then
    unclutter -idle 1 -root >/dev/null 2>&1 9>&- &
fi

wait "$BROWSER"
