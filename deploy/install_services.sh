#!/usr/bin/env bash
# DevPi starts itself: pi5 and the splash as the systemd services ddm-pi5 and ddm-splash
# (deploy/*.service), enabled at boot and started now; and the TV's kiosk with the desktop
# session (deploy/kiosk_autostart.py: ~/.config/autostart/ddm-tv.desktop, and on labwc the
# Alt+Super+H keybind that hides the pointer; wtype, which presses it, from apt if missing).
#
# Run it on DevPi as the user the services run as (joey), not with sudo: it asks for sudo
# itself where it needs it.
#
#   ~/DDM-Multimedia/deploy/install_services.sh               install, or update after a pull
#   ~/DDM-Multimedia/deploy/install_services.sh --dry-run     the checks, and what it would do
#   ~/DDM-Multimedia/deploy/install_services.sh --uninstall   stop and remove what it installed
#
# It checks first, and installs nothing if a check says STOP:
#   - systemd and /usr/bin/python3 are there, and the repo's path has no spaces;
#   - the user is in dialout (the gateway's serial port);
#   - /usr/bin/python3, run as that user with an environment as bare as the service's,
#     imports every package pi5 and the splash need;
#   - nothing started by hand holds pi5's or the splash's port (the service would fail to
#     start and try again every 3 s).
# A warning only: the gateway's port (LQ_SERIAL_PORT, pi5/config.py or pi5/.env) not there
# now (pi5 retries every 5 s until it appears), DDM_* settings that live in this shell only.
#
# Running it twice is fine: the units are rewritten only when they differ, and both services
# are restarted so they run the code that is checked out.

set -euo pipefail

PYTHON=/usr/bin/python3
UNIT_DIR=/etc/systemd/system
SERVICE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin   # systemd's PATH for a service
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$DEPLOY_DIR")"
OLD_SPLASH_UNIT=splash_display.service           # the splash's earlier unit, for user pi: replaced by ddm-splash

DRY=0
ACTION=install
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --uninstall) ACTION=uninstall ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (use --dry-run, --uninstall or --help)" >&2; exit 2 ;;
    esac
done

# --- Who and where ------------------------------------------------------------------

if [ "$(id -u)" -eq 0 ]; then
    RUN_USER="${SUDO_USER:-}"
    if [ -z "$RUN_USER" ] || [ "$RUN_USER" = root ]; then
        echo "Run this as the user the services run as (for example joey), not as root." >&2
        exit 1
    fi
    SUDO=""
else
    RUN_USER="$(id -un)"
    SUDO=sudo
fi
RUN_HOME="$HOME"
if command -v getent >/dev/null 2>&1; then
    RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
fi

HAVE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    HAVE_SYSTEMD=1
fi

STOPS=0
ok()   { echo "  ok    $*"; }
warn() { echo "  WARN  $*"; }
stop() { echo "  STOP  $*"; STOPS=$((STOPS + 1)); }
skip() { echo "  --    $*"; }
# Run a command that changes something, or only say it in a dry run.
act() {
    if [ "$DRY" = 1 ]; then
        echo "  would run: $*"
    else
        echo "  + $*"
        "$@"
    fi
}
# Run as the service's user, with the service's bare environment (no DDM_* from this shell).
as_user() {
    local envs=(env -i "HOME=$RUN_HOME" "USER=$RUN_USER" "LOGNAME=$RUN_USER" "PATH=$SERVICE_PATH" LANG=C.UTF-8)
    if [ "$(id -un)" = "$RUN_USER" ]; then
        "${envs[@]}" "$@"
    else
        sudo -u "$RUN_USER" "${envs[@]}" "$@"
    fi
}
# True if something accepts a connection on 127.0.0.1:$1.
port_answers() {
    timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$1" >/dev/null 2>&1
}
service_active() {
    [ "$HAVE_SYSTEMD" = 1 ] && systemctl is-active --quiet "$1"
}
render() {   # the unit with this repo's path and user filled in
    sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$RUN_USER|g" "$1"
}
# The TV kiosk's files in the user's home (deploy/kiosk_autostart.py install|uninstall),
# written as that user. Without systemd (a dry run on another machine) any python will do.
kiosk_autostart() {
    local args=("$@")
    [ "$DRY" = 1 ] && args+=(--dry-run)
    if [ "$HAVE_SYSTEMD" = 1 ] && [ -x "$PYTHON" ]; then
        as_user "$PYTHON" "$DEPLOY_DIR/kiosk_autostart.py" "${args[@]}"
    else
        local py
        py="$(command -v python3 || command -v python || true)"
        if [ -n "$py" ]; then
            "$py" "$DEPLOY_DIR/kiosk_autostart.py" "${args[@]}" --home "$RUN_HOME"
        else
            skip "no python here: the kiosk's autostart is not shown"
        fi
    fi
}

echo "DDM services: repo $REPO, user $RUN_USER$( [ "$DRY" = 1 ] && echo ', dry run: nothing is changed' )"

# --- Uninstall ----------------------------------------------------------------------

if [ "$ACTION" = uninstall ]; then
    if [ "$HAVE_SYSTEMD" = 1 ]; then
        for s in ddm-pi5 ddm-splash; do
            if [ -f "$UNIT_DIR/$s.service" ]; then
                act $SUDO systemctl disable --now "$s"
                act $SUDO rm -f "$UNIT_DIR/$s.service"
            else
                skip "$s is not installed"
            fi
        done
        act $SUDO systemctl daemon-reload
    else
        skip "no systemd here: nothing to remove"
    fi
    echo
    echo "TV kiosk"
    kiosk_autostart uninstall
    echo "Done. pi5 and the splash no longer start on their own, nor the kiosk with the desktop; RACE_NIGHT.md, \"Working on it by hand\", starts them by hand."
    exit 0
fi

# --- Checks -------------------------------------------------------------------------

echo
echo "Checks"
case "$REPO" in
    *[!A-Za-z0-9/._-]*) stop "the repo's path ($REPO) has a space or another character a unit file can't take: clone it somewhere plain, e.g. ~/DDM-Multimedia" ;;
    *) ok "repo path $REPO" ;;
esac
[ -f "$REPO/pi5/main.py" ] && [ -f "$REPO/splash_display/server.py" ] \
    || stop "pi5/main.py or splash_display/server.py is missing under $REPO"

if [ "$HAVE_SYSTEMD" = 0 ]; then
    if [ "$DRY" = 1 ]; then
        skip "no systemd here: the checks below need DevPi (this dry run only shows the units and the steps)"
    else
        stop "no systemd here (systemctl and /run/systemd/system): run this on DevPi"
    fi
else
    ok "systemd"
    if [ -x "$PYTHON" ]; then
        ok "$PYTHON ($("$PYTHON" -V 2>&1))"
    else
        stop "$PYTHON is missing: the services run it (sudo apt install python3)"
    fi

    if id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx dialout; then
        ok "$RUN_USER is in dialout (the gateway's serial port)"
    else
        stop "$RUN_USER is not in dialout, so pi5 can't open the gateway's port: sudo usermod -aG dialout $RUN_USER, then run this again"
    fi

    if [ -x "$PYTHON" ]; then
        # Every top-level package pi5 and the splash import (their requirements.txt), as the
        # service will run them: this user, a bare environment, packages from the system or ~/.local.
        missing="$(cd "$REPO" && as_user "$PYTHON" - <<'PY' 2>&1
import importlib
needed = [  # (module, pip name, who needs it)
    ("flask", "Flask", "pi5 and the splash"), ("werkzeug", "Werkzeug", "pi5 and the splash"),
    ("jinja2", "Jinja2", "pi5 and the splash"), ("flask_socketio", "Flask-SocketIO", "pi5"),
    ("socketio", "python-socketio", "pi5"), ("engineio", "python-engineio", "pi5"),
    ("requests", "requests", "pi5"), ("serial", "pyserial", "pi5 (the gateway)"),
    ("dotenv", "python-dotenv", "pi5 (pi5/.env)"),
]
for module, pip_name, who in needed:
    try:
        importlib.import_module(module)
    except Exception as exc:
        print(f"{module}|{pip_name}|{who}|{exc}")
try:
    importlib.import_module("anthropic")
except Exception:
    print("anthropic|anthropic|optional: the track's odds poller|")
PY
)" || true
        if [ -z "$missing" ]; then
            ok "$PYTHON imports flask, flask_socketio, socketio, engineio, requests, serial, dotenv, jinja2, werkzeug as $RUN_USER"
        else
            while IFS='|' read -r module pip_name who error; do
                [ -n "$module" ] || continue
                if [ "$module" = anthropic ]; then
                    warn "$PYTHON can't import anthropic as $RUN_USER: the odds poller won't start (everything else runs)"
                else
                    stop "$PYTHON can't import $module as $RUN_USER (needed by $who${error:+: $error}): pip3 install --user --break-system-packages $pip_name"
                fi
            done <<< "$missing"
        fi

        # The gateway's port and pi5's own port, read the way pi5 reads them.
        read -r PI5_PORT SERIAL <<< "$(cd "$REPO/pi5" && as_user "$PYTHON" -c \
            'import config; print(getattr(config, "FLASK_PORT", 5000), getattr(config, "LQ_SERIAL_PORT", "") or "-")' 2>/dev/null || echo "5000 ?")"
        if [ "$SERIAL" = "?" ]; then
            warn "pi5/config.py could not be read as $RUN_USER, so the gateway's port is not checked"
        elif [ "$SERIAL" = "-" ]; then
            warn "LQ_SERIAL_PORT is empty: pi5's bridge will idle (set it in pi5/config.py or pi5/.env)"
        elif [ -e "$SERIAL" ]; then
            ok "the gateway's port is there: $SERIAL"
        else
            warn "the gateway's port is not there now ($SERIAL): unplugged? pi5 starts anyway and opens it when it appears (it tries every 5 s)"
        fi
        SPLASH_PORT="$(cd "$REPO/splash_display" && as_user "$PYTHON" -c \
            'import config; print(getattr(config, "FLASK_PORT", 5001))' 2>/dev/null || echo 5001)"
    else
        PI5_PORT=5000
        SPLASH_PORT=5001
    fi

    for pair in "ddm-pi5:${PI5_PORT:-5000}:pi5 (python main.py)" "ddm-splash:${SPLASH_PORT:-5001}:the splash (python3 server.py)"; do
        IFS=: read -r s port who <<< "$pair"
        if port_answers "$port" && ! service_active "$s"; then
            stop "port $port is in use and $s isn't running: $who started by hand? Ctrl+C it in its terminal, then run this again"
        else
            ok "port $port: free, or held by $s itself"
        fi
    done
fi

leftover="$(env | grep -o '^DDM_[A-Za-z0-9_]*' || true)"
if [ -n "$leftover" ]; then
    warn "set in this shell only, so the services won't see them: $(echo "$leftover" | tr '\n' ' ')- put them in pi5/.env"
fi

if [ "$STOPS" -gt 0 ]; then
    echo
    echo "Nothing installed: $STOPS check(s) said STOP. Fix them and run this again."
    exit 1
fi

# --- Install ------------------------------------------------------------------------

echo
echo "Services"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for s in ddm-pi5 ddm-splash; do
    render "$DEPLOY_DIR/$s.service" > "$TMP/$s.service"
    if [ "$DRY" = 1 ]; then
        echo "  would write $UNIT_DIR/$s.service:"
        sed 's/^/      | /' "$TMP/$s.service"
    elif [ -f "$UNIT_DIR/$s.service" ] && cmp -s "$TMP/$s.service" "$UNIT_DIR/$s.service"; then
        ok "$UNIT_DIR/$s.service unchanged"
    else
        act $SUDO install -m 0644 "$TMP/$s.service" "$UNIT_DIR/$s.service"
    fi
done
if [ "$HAVE_SYSTEMD" = 1 ] && [ -f "$UNIT_DIR/$OLD_SPLASH_UNIT" ]; then
    echo "  the splash's earlier unit $OLD_SPLASH_UNIT is installed: ddm-splash replaces it"
    act $SUDO systemctl disable --now "$OLD_SPLASH_UNIT"
    act $SUDO rm -f "$UNIT_DIR/$OLD_SPLASH_UNIT"
fi
act $SUDO systemctl daemon-reload
act $SUDO systemctl enable ddm-pi5 ddm-splash
act $SUDO systemctl restart ddm-pi5 ddm-splash

# --- The TV kiosk -------------------------------------------------------------------

echo
echo "TV kiosk (it starts at the next login: a reboot, or log out and in)"
if command -v labwc >/dev/null 2>&1 || [ -d /etc/xdg/labwc ]; then
    if command -v wtype >/dev/null 2>&1; then
        ok "wtype is there (it presses the key that hides the pointer)"
    elif command -v apt-get >/dev/null 2>&1; then
        act $SUDO apt-get install -y wtype || warn "wtype did not install: the pointer stays on the TV (sudo apt install wtype)"
    else
        warn "no wtype and no apt-get: the pointer stays on the TV"
    fi
fi
kiosk_autostart install || warn "the kiosk's autostart was not written (above): start it by hand, splash_display/deploy/kiosk.sh"

if [ "$DRY" = 1 ]; then
    echo
    echo "Dry run: nothing changed."
    exit 0
fi

# --- Up? ----------------------------------------------------------------------------

echo
echo "Starting"
for pair in "ddm-pi5:${PI5_PORT:-5000}" "ddm-splash:${SPLASH_PORT:-5001}"; do
    IFS=: read -r s port <<< "$pair"
    up=0
    for _ in $(seq 1 30); do
        if port_answers "$port"; then up=1; break; fi
        sleep 1
    done
    if [ "$up" = 1 ]; then
        ok "$s is running and answers on port $port"
    else
        warn "$s does not answer on port $port after 30 s: its log follows (journalctl -u $s)"
        journalctl -u "$s" -n 30 --no-pager || true
    fi
done
echo
echo "Installed. Both start on their own at every boot, and the TV's kiosk with the desktop."
echo "  systemctl status ddm-pi5 ddm-splash      journalctl -u ddm-pi5 -f      sudo systemctl restart ddm-pi5 ddm-splash"
