#!/bin/sh
# Install the score display on a Raspberry Pi, and set it running at boot.
#
# Run this on the Pi, from a checkout of this repository:
#
#   sudo ./deploy/install.sh --host stord-pk --range 1-10 --lane 9
#
# Everything after the flags is optional; a display with no club configured
# comes up showing "not configured" and serves its web page, so it can be
# pointed at a range from a phone afterwards.
set -eu

PREFIX=/opt/megalink
CONFIG_DIR=/etc/megalink
CONFIG="$CONFIG_DIR/display.json"
SERVICE_USER=megalink
MODE=""

HOST=""; RANGE=""; LANE=""; NAME=""; TOKEN=""; WEB_PORT=""; URL=""

usage() {
    cat >&2 <<'USAGE'
usage: install.sh [options]

  --host SLUG        club to display, e.g. stord-pk
  --range KEY        range key, name, or the slug from a Live URL
  --lane N           firing point to display
  --name NAME        what to call this display (default: hostname)
  --mode gui|terminal|browser
                     draw it ourselves, use the bare console, or show
                     Megalink's own page in a full-screen browser
  --url ADDRESS      what browser mode shows (default: Megalink's own page
                     for this firing point)
                     (default: leave as configured, or gui on a new install)
  --web-port PORT    port for the configuration page (default: 8080)
  --token SECRET     require this secret to change anything. Give every
                     display on a range the same one: each serves the fleet
                     dashboard at /fleet and pushes changes to the others
                     with it, and the pages send it back for you.
  --no-service       install but do not enable the boot service
  --autologin        start the display from a real login session on tty1 rather
                     than from a systemd service. Slower to restart, but it is
                     the only way some Pis will let X drive the screen: X needs
                     a logind session, and a login is what reliably makes one.
  --service          go back to the systemd service (the default)
USAGE
    exit 2
}

INSTALL_SERVICE=yes
AUTOLOGIN=no
while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --range) RANGE="$2"; shift 2 ;;
        --lane) LANE="$2"; shift 2 ;;
        --name) NAME="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --url) URL="$2"; shift 2 ;;
        --web-port) WEB_PORT="$2"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --no-service) INSTALL_SERVICE=no; shift ;;
        --autologin) AUTOLOGIN=yes; shift ;;
        --service) AUTOLOGIN=no; shift ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "this needs root; try: sudo $0 $*" >&2
    exit 1
fi

SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
echo "installing from $SOURCE"

# Only change the mode when asked to. Re-installing used to reset it to "gui",
# which silently undid a switch to the console made from the web page or the
# fleet dashboard -- and on a machine whose X server does not work, that put the
# screen back to black on every push.
EFFECTIVE_MODE="$MODE"
if [ -z "$EFFECTIVE_MODE" ] && [ -r "$CONFIG" ]; then
    EFFECTIVE_MODE="$(
        python3 - "$CONFIG" 2>/dev/null <<'PYTHON' || true
import json, sys
try:
    with open(sys.argv[1]) as handle:
        print(json.load(handle).get("display", {}).get("mode") or "")
except Exception:
    print("")
PYTHON
    )"
    [ -n "$EFFECTIVE_MODE" ] && echo "keeping the configured mode: $EFFECTIVE_MODE"
fi
[ -n "$EFFECTIVE_MODE" ] || EFFECTIVE_MODE=gui

# --- packages -------------------------------------------------------------
# python3-tk is the toolkit the window needs; it is not part of the stdlib
# install on Debian. xinit gives us a screen without a desktop. Nothing here
# needs a compiler, which is the point of a dependency-free package.
PACKAGES="python3 python3-venv"
if [ "$EFFECTIVE_MODE" = gui ] || [ "$EFFECTIVE_MODE" = browser ]; then
    PACKAGES="$PACKAGES xserver-xorg xinit x11-xserver-utils"
    [ "$EFFECTIVE_MODE" = gui ] && PACKAGES="$PACKAGES python3-tk"
    # A window manager. Without one a bare X server maps a window and leaves it
    # there, with nothing to size, raise or focus it.
    PACKAGES="$PACKAGES matchbox-window-manager"
    # gldriver-test is what makes X work on a Pi at all. It ships the script
    # that writes /etc/X11/xorg.conf.d/99-v3d.conf, which is the file that tells
    # X the display driver is vc4. The Desktop images have it; the Lite images
    # deliberately do not, because they are meant to be used without a desktop.
    # Without it X starts, reads the monitor's EDID, reports an active mode --
    # and puts nothing on the screen, for any client.
    PACKAGES="$PACKAGES gldriver-test"
fi
if [ "$EFFECTIVE_MODE" = browser ]; then
    # Chromium is what Raspberry Pi OS ships and what the kiosk switches suit.
    # It is a few hundred megabytes in use: fine on a Pi 4, tight on a Pi Zero,
    # which is why the display warns about it at startup rather than here.
    PACKAGES="$PACKAGES chromium-browser"
fi
echo "installing packages: $PACKAGES"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
# shellcheck disable=SC2086
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $PACKAGES

# --- user -----------------------------------------------------------------
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "creating the $SERVICE_USER user"
    useradd --system --create-home --home-dir "$PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# Owning the console and the GPU is what lets it draw without a desktop.
for group in video render input tty; do
    getent group "$group" >/dev/null 2>&1 && adduser "$SERVICE_USER" "$group" >/dev/null 2>&1 || true
done

# --- the package ----------------------------------------------------------
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$PREFIX"

VENV="$PREFIX/venv"
PY="$VENV/bin/python"

make_venv() {
    # --system-site-packages so the venv can see the system tkinter, which pip
    # cannot install.
    python3 -m venv --system-site-packages "$VENV"
}

# Everything goes through `python -m`, never through the console scripts in
# venv/bin. A truncated console script -- a zero-byte file with the execute bit
# still set -- runs as an empty shell script: no output, exit 0. That makes pip
# appear to install successfully while doing nothing, and makes the display
# appear to start and stop cleanly while never running at all. `python -m` reads
# the installed package instead, so it cannot be fooled the same way.
works() {
    [ -x "$PY" ] && [ -n "$("$PY" -m megalink_viewer --version 2>/dev/null)" ]
}

if [ ! -x "$PY" ]; then
    echo "creating the virtual environment"
    make_venv
fi
# Prefer a wheel built elsewhere. A Pi Zero 2 W is slow at building, and
# installing from source makes pip fetch a build backend from the network --
# which is a poor thing to depend on at a range. deploy/push.sh builds the wheel
# on the machine you push from and leaves it here.
WHEEL="$(ls -1t "$SOURCE"/dist/*.whl 2>/dev/null | head -1 || true)"

# --force-reinstall because the version rarely changes between pushes, and
# without it pip would decide the old copy was good enough.
do_install() {
    if [ -n "$WHEEL" ]; then
        echo "installing $(basename "$WHEEL")"
        "$PY" -m pip install --quiet --force-reinstall --no-index "$WHEEL"
    else
        echo "no wheel found; building from source (slower)"
        "$PY" -m pip install --quiet --upgrade pip
        "$PY" -m pip install --quiet --force-reinstall "$SOURCE"
    fi
}

do_install || true

# Check it rather than assume it. An install that reports success and leaves
# nothing runnable behind is exactly the failure this is here to catch.
if ! works; then
    echo "the package did not install correctly; rebuilding the environment" >&2
    rm -rf "$VENV"
    make_venv
    do_install || true
fi

if ! works; then
    echo >&2
    echo "ERROR: $PY -m megalink_viewer produces no output after installing." >&2
    echo "Disk space and the state of $VENV are the first things to check:" >&2
    df -h "$PREFIX" >&2 || true
    ls -l "$VENV/bin" >&2 || true
    exit 1
fi
echo "installed $("$PY" -m megalink_viewer --version)"
install -m 0755 "$SOURCE/deploy/megalink-launch" "$PREFIX/megalink-launch"
install -m 0755 "$SOURCE/deploy/megalink-session" "$PREFIX/megalink-session"
sync
chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"

# --- configuration --------------------------------------------------------
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$CONFIG_DIR"

# A configuration that cannot be read would stop the installer from writing the
# one it was asked to write. Move it aside rather than fail: the operator gets a
# working display and the old file is still there to look at.
if [ -e "$CONFIG" ] && ! "$PY" -m megalink_viewer config --config "$CONFIG" >/dev/null 2>&1; then
    BACKUP="$CONFIG.unreadable.$(date +%Y%m%d-%H%M%S)"
    mv "$CONFIG" "$BACKUP"
    echo "existing configuration could not be read; moved it to $BACKUP" >&2
fi

set -- config --config "$CONFIG"
[ -n "$MODE" ] && set -- "$@" --mode "$MODE"
[ -n "$HOST" ] && set -- "$@" --host "$HOST"
[ -n "$RANGE" ] && set -- "$@" --range "$RANGE"
[ -n "$LANE" ] && set -- "$@" --lane "$LANE"
[ -n "$NAME" ] && set -- "$@" --name "$NAME"
[ -n "$TOKEN" ] && set -- "$@" --token "$TOKEN"
[ -n "$URL" ] && set -- "$@" --url "$URL"
[ -n "$WEB_PORT" ] && set -- "$@" --web-port "$WEB_PORT"
"$PY" -m megalink_viewer "$@" >/dev/null
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"
chmod 0640 "$CONFIG"
sync
if [ ! -s "$CONFIG" ]; then
    echo "ERROR: $CONFIG is empty after writing it. Suspect the SD card." >&2
    exit 1
fi
echo "wrote $CONFIG"

# --- the driver configuration X needs on a Pi ------------------------------
if [ "$EFFECTIVE_MODE" = gui ]; then
    # The package writes the file from a boot-time script, so on a machine that
    # has not rebooted since installing it the file is not there yet. Run the
    # generator directly where we can find it, and say so plainly where we
    # cannot -- a missing driver config is the difference between a working
    # screen and a blank one.
    if ! ls /etc/X11/xorg.conf.d/*.conf >/dev/null 2>&1; then
        for script in /lib/systemd/scripts/rp1_test.sh /usr/lib/systemd/scripts/rp1_test.sh; do
            [ -x "$script" ] && "$script" >/dev/null 2>&1 && break
        done
    fi
    if ls /etc/X11/xorg.conf.d/*.conf >/dev/null 2>&1; then
        echo "X driver configuration present: $(ls /etc/X11/xorg.conf.d/*.conf | tr '\n' ' ')"
    else
        echo >&2
        echo "NOTE: /etc/X11/xorg.conf.d is still empty. The gldriver-test package" >&2
        echo "writes it from a service that runs at boot, so reboot this Pi:" >&2
        echo "    sudo reboot" >&2
        echo "Without that file X will start and show nothing at all." >&2
        echo >&2
    fi
fi

# --- letting a service start X --------------------------------------------
# Written whichever mode is configured. The mode can be changed at runtime
# from the web page and the fleet dashboard, so anything X needs has to be in
# place already -- a setting that only arrives when the installer happens to
# run with gui selected is missing exactly when somebody switches to a
# window. Both of these are harmless on the console.
if true; then
    # Debian ships allowed_users=console, which means only somebody logged in at
    # the keyboard may start X. A systemd service is not that, so without this
    # the display would fail with "Only console users are allowed to run the X
    # server" and nothing would appear on the screen.
    install -d /etc/X11
    cat > /etc/X11/Xwrapper.config <<'XWRAPPER'
# Written by megalink's installer: the score display starts X from a systemd
# service rather than from a login session.
allowed_users=anybody
needs_root_rights=yes
XWRAPPER

    # X puts its socket here. It normally exists from boot, but a non-root X
    # cannot create it if anything has taken it away, and it then exits with
    # "_XSERVTransmkdir: ERROR: euid != 0" before probing a screen.
    install -d -m 1777 /tmp/.X11-unix
    echo "allowed the display service to start X"
fi

# --- a login session, for X on machines that insist on one ----------------
#
# X will not drive a screen without a logind session: no session means no seat,
# and no seat means it never becomes DRM master. `PAMName=login` on the service
# asks systemd to make one, which is enough on most machines and not on all. A
# genuine autologin gives X the session a login creates, which nothing else has
# to synthesise.
#
# The session runs the same launcher the service does, so the mode, the web
# page, the beacon and the fleet dashboard all behave identically.
GETTY_DROPIN=/etc/systemd/system/getty@tty1.service.d
PROFILE="$PREFIX/.bash_profile"

if [ "$AUTOLOGIN" = yes ]; then
    # agetty needs a shell it can start.
    usermod --shell /bin/bash "$SERVICE_USER" >/dev/null 2>&1 || true

    install -d "$GETTY_DROPIN"
    cat > "$GETTY_DROPIN/autologin.conf" <<AUTOLOGIN_CONF
# Written by megalink's installer for --autologin.
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $SERVICE_USER --noclear %I \$TERM
AUTOLOGIN_CONF

    cat > "$PROFILE" <<PROFILE_EOF
# Written by megalink's installer for --autologin.
#
# Only on the console this display owns, so an SSH session is unaffected.
#
# Through systemd-cat, because a login session's output goes to the terminal --
# and X puts that terminal into graphics mode, so anything written there is
# invisible. Without this the display has no log at all, which is precisely the
# situation this whole exercise has been trying to get out of.
if [ "\$(tty)" = "/dev/tty1" ]; then
    exec systemd-cat --identifier=megalink-display $PREFIX/megalink-launch
fi
PROFILE_EOF
    chown "$SERVICE_USER:$SERVICE_USER" "$PROFILE"
    sync

    systemctl disable --now megalink-display >/dev/null 2>&1 || true
    systemctl daemon-reload
    systemctl enable getty@tty1.service >/dev/null 2>&1 || true
    systemctl restart getty@tty1.service || true
    echo "the display now starts from a login session on tty1"
    echo "  restart it with: sudo systemctl restart getty@tty1"
    INSTALL_SERVICE=no
else
    # Undo it, so --service really does go back.
    if [ -f "$GETTY_DROPIN/autologin.conf" ]; then
        rm -f "$GETTY_DROPIN/autologin.conf"
        rmdir "$GETTY_DROPIN" 2>/dev/null || true
        rm -f "$PROFILE"
        systemctl daemon-reload
        systemctl restart getty@tty1.service || true
        echo "stopped starting the display from a login session"
    fi
fi

# --- the service ----------------------------------------------------------
if [ "$INSTALL_SERVICE" = yes ]; then
    UNIT=/etc/systemd/system/megalink-display.service
    install -m 0644 "$SOURCE/deploy/megalink-display.service" "$UNIT"
    sync

    # systemd treats a zero-byte unit file exactly like one masked with a
    # symlink to /dev/null: it refuses to load it, the service never starts, and
    # `journalctl -u` has nothing to show because nothing ever ran. Files on
    # this machine have turned up empty before, so this is checked rather than
    # assumed.
    if [ ! -s "$UNIT" ]; then
        echo "the unit file was written empty; writing it again" >&2
        cat "$SOURCE/deploy/megalink-display.service" > "$UNIT"
        sync
    fi
    if [ ! -s "$UNIT" ]; then
        echo "ERROR: $UNIT is empty after two attempts. The filesystem is not" >&2
        echo "keeping what is written to it -- suspect the SD card." >&2
        exit 1
    fi

    # The login prompt on tty1 wants the same screen, and the unit declares a
    # conflict so systemd stops it while the display runs. It is deliberately
    # *not* disabled: if the display ever fails to start, the login prompt
    # coming back is the difference between a Pi you can walk up to and a Pi
    # showing a frozen boot message with no way in but the network. An earlier
    # version disabled it and produced exactly that.
    if systemctl is-enabled getty@tty1.service >/dev/null 2>&1; then
        :
    else
        systemctl enable getty@tty1.service >/dev/null 2>&1 || true
        echo "re-enabled the login prompt on tty1 as a fallback"
    fi

    # X needs a logind session, written whatever the mode is, for the same
    # reason as the wrapper above: the mode changes at runtime and this cannot.
    DROPIN=/etc/systemd/system/megalink-display.service.d
    if true; then
        install -d "$DROPIN"
        cat > "$DROPIN/pam-session.conf" <<'DROP'
# Written by megalink's installer for gui mode.
#
# Without a logind session X reports
#   (EE) systemd-logind: failed to get session: PID N does not belong to any
#        known session
# then reads the monitor's EDID, lists every mode it supports, reports success,
# and drives nothing: no session means no seat, and no seat means it never
# becomes DRM master.
[Service]
PAMName=login
DROP
        sync
        echo "asked for a login session, which X needs"
    fi

    systemctl daemon-reload

    # An earlier empty unit file leaves the service masked even once the file is
    # whole again, so clear that state explicitly.
    if [ "$(systemctl is-enabled megalink-display 2>/dev/null)" = masked ]; then
        systemctl unmask megalink-display >/dev/null 2>&1 || true
        systemctl daemon-reload
        echo "unmasked the service"
    fi

    systemctl enable megalink-display >/dev/null
    systemctl restart megalink-display
    echo "service enabled and started"

    # Give it a moment and say plainly whether it actually came up, rather than
    # leaving "enabled" to be mistaken for "working".
    sleep 3

    # If asking for a login session is what stopped it, take that back rather
    # than leaving a Pi with no display at all. A window is worth having; it is
    # not worth having instead of everything.
    if ! systemctl is-active --quiet megalink-display && [ -f "$DROPIN/pam-session.conf" ]; then
        echo "the service did not start; retrying without a login session" >&2
        rm -f "$DROPIN/pam-session.conf"
        rmdir "$DROPIN" 2>/dev/null || true
        systemctl daemon-reload
        systemctl restart megalink-display || true
        sleep 3
        if systemctl is-active --quiet megalink-display; then
            echo "running without a login session; X will probably not drive the screen." >&2
            echo "Use --mode terminal for a console display that does." >&2
        fi
    fi

    if systemctl is-active --quiet megalink-display; then
        echo "service is running"
    else
        echo
        echo "WARNING: the service is enabled but not running. Its log:" >&2
        journalctl -u megalink-display -n 60 --no-pager >&2 || true
        echo >&2
        echo "If X refused to start, see the notes in README.md." >&2
    fi
fi

# Read the port straight out of the file rather than back through the CLI, and
# fall back rather than fail: this block only prints a summary, and it has no
# business breaking an install that otherwise worked.
PORT="$(
    "$PY" - "$CONFIG" 2>/dev/null <<'PYTHON' || echo 8080
import json, sys
try:
    with open(sys.argv[1]) as handle:
        print(json.load(handle)["web"]["port"] or 8080)
except Exception:
    print(8080)
PYTHON
)"
ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<DONE

Done.

  configuration page  http://${ADDRESS:-<this pi>}:$PORT/
  configuration file  $CONFIG
  logs                journalctl -u megalink-display -f

Find every display on the network from a laptop with:

  megalink fleet --open
DONE
