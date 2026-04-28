#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# DDM Splash Display — kiosk launcher
# -----------------------------------------------------------------------------
# Launches Chromium in fullscreen kiosk mode pointed at the local Flask app.
# Designed to be invoked from the desktop session autostart on a Pi 4B running
# Pi OS Bookworm. Wayfire/labwc autostart is handled in deploy/autostart_setup.md.
#
# Mark executable:   chmod +x deploy/kiosk.sh
# Manual launch:     ./deploy/kiosk.sh
# -----------------------------------------------------------------------------

set -euo pipefail

URL="${SPLASH_URL:-http://localhost:5000/display}"

# --- Disable screen blanking / DPMS / screensaver -----------------------------
# These are best-effort; they no-op if the binary isn't installed.
if command -v xset >/dev/null 2>&1; then
    xset s off          || true
    xset s noblank      || true
    xset -dpms          || true
fi
if command -v wlr-randr >/dev/null 2>&1; then
    : # nothing to do here yet, but a hook for Wayland tweaks if needed
fi

# --- Wait for the Flask service to come up ------------------------------------
# Don't race with splash_display.service on cold boot.
for i in $(seq 1 30); do
    if curl -fsS -o /dev/null "$URL"; then
        break
    fi
    sleep 1
done

# --- Pick the right Chromium binary -------------------------------------------
CHROMIUM="${CHROMIUM_BIN:-}"
if [ -z "$CHROMIUM" ]; then
    if command -v chromium-browser >/dev/null 2>&1; then
        CHROMIUM="chromium-browser"
    elif command -v chromium >/dev/null 2>&1; then
        CHROMIUM="chromium"
    else
        echo "kiosk.sh: no chromium binary found in PATH" >&2
        exit 1
    fi
fi

# --- Clear any previous "session crashed" / "restore tabs" prompts ------------
PROFILE_DIR="${HOME}/.config/chromium-kiosk"
mkdir -p "$PROFILE_DIR"
PREF_FILE="$PROFILE_DIR/Default/Preferences"
if [ -f "$PREF_FILE" ]; then
    sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' "$PREF_FILE" || true
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' "$PREF_FILE" || true
fi

# --- Launch Chromium in kiosk mode --------------------------------------------
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
exec "$CHROMIUM" \
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
    "$URL"
