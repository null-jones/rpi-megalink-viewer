#!/bin/sh
# Report why a display is not working. Prints something for every check, so a
# silent failure cannot hide inside it.
#
#   ./deploy/diagnose.sh
#
# Paste the whole output when asking for help.
PREFIX=/opt/megalink
VENV="$PREFIX/venv"
PY="$VENV/bin/python"
BIN="$VENV/bin/megalink"
CONFIG=/etc/megalink/display.json
USER_NAME=megalink

line() { printf '\n=== %s\n' "$1"; }
try() {
    printf '$ %s\n' "$*"
    "$@" 2>&1
    printf '  [exit %s]\n' "$?"
}

line "who is asking"
try id

line "paths and permissions"
for path in "$PREFIX" "$VENV" "$VENV/bin" "$BIN" /etc/megalink "$CONFIG"; do
    if [ -e "$path" ]; then
        ls -ld "$path" 2>&1
    else
        echo "MISSING: $path"
    fi
done

line "console scripts (a zero-byte one runs silently and exits 0)"
for script in "$VENV/bin/megalink" "$VENV/bin/pip" "$VENV/bin/python"; do
    if [ -e "$script" ]; then
        printf '%s bytes  %s\n' "$(wc -c < "$script" 2>/dev/null)" "$script"
    else
        echo "MISSING: $script"
    fi
done

line "running the package, which does not depend on those scripts"
try "$PY" -m megalink_viewer --version

line "can the service user run it"
try sudo -n -u "$USER_NAME" "$PY" -m megalink_viewer --version

line "disk space"
try df -h "$PREFIX" /tmp

line "is the package importable, and which copy"
try "$PY" -c 'import megalink_viewer as m; print(m.__version__, m.__file__)'

line "what pip thinks is installed"
try "$PY" -m pip show megalink-viewer

line "can the service user read the configuration"
try sudo -n -u "$USER_NAME" cat "$CONFIG"

line "does the display start (5 seconds, as the service user)"
printf '$ timeout 5 sudo -u %s %s -m megalink_viewer display --config %s\n' "$USER_NAME" "$PY" "$CONFIG"
sudo -n -u "$USER_NAME" timeout 5 "$PY" -m megalink_viewer display --config "$CONFIG" 2>&1 | head -25
printf '  [exit %s]\n' "$?"

line "service"
try systemctl is-enabled megalink-display
try systemctl is-active megalink-display
try systemctl status megalink-display --no-pager -l -n 20

line "recent display log"
if [ -f /etc/systemd/system/getty@tty1.service.d/autologin.conf ]; then
    try journalctl -t megalink-display -n 25 --no-pager
else
    try journalctl -u megalink-display -n 25 --no-pager
fi

line "how the display is started"
if [ -f /etc/systemd/system/getty@tty1.service.d/autologin.conf ]; then
    echo "from a login session on tty1 (--autologin)"
    cat /etc/systemd/system/getty@tty1.service.d/autologin.conf
else
    echo "from the megalink-display service"
fi
try loginctl list-sessions
try loginctl show-seat seat0 -p Sessions -p CanGraphical

line "who owns the console"
try systemctl is-enabled getty@tty1.service
try systemctl is-active getty@tty1.service
DROPIN=/etc/systemd/system/megalink-display.service.d
if [ -d "$DROPIN" ]; then
    for f in "$DROPIN"/*; do
        [ -e "$f" ] || continue
        echo "--- $f"; cat "$f"
    done
else
    echo "no drop-ins in $DROPIN"
fi

line "X"
try sh -c 'command -v Xorg X xinit xsetroot'
echo "--- X driver configuration (gldriver-test writes this; Lite images lack it)"
if ls /etc/X11/xorg.conf.d/*.conf >/dev/null 2>&1; then
    for f in /etc/X11/xorg.conf.d/*.conf; do echo "  $f"; done
else
    echo "  EMPTY -- X will start and display nothing. sudo apt install gldriver-test"
fi
try dpkg -l gldriver-test

try systemctl show megalink-display -p PAMName

XLOG="$PREFIX/.local/share/xorg/Xorg.0.log"
if [ -r "$XLOG" ]; then
    echo "--- does X have a session? (no session means no DRM master, so no picture)"
    if grep -q "does not belong to any known session" "$XLOG"; then
        echo "NO SESSION: X never became DRM master; nothing it draws reaches the screen"
    else
        echo "ok: no logind session error"
    fi
    echo "--- errors and warnings"
    grep -E "\(EE\)" "$XLOG" | tail -10
    echo "--- the mode X selected"
    grep -E "modeset\(0\).*(Output|Modeline)" "$XLOG" | tail -6
else
    echo "no Xorg log at $XLOG"
fi
if [ -r /etc/X11/Xwrapper.config ]; then
    echo "--- /etc/X11/Xwrapper.config"; cat /etc/X11/Xwrapper.config
else
    echo "MISSING: /etc/X11/Xwrapper.config"
fi

line "files that should not be empty"
# Every failure on this project so far has had a zero-byte file behind it: an
# empty executable runs as an empty script and exits 0, an empty unit file reads
# to systemd as masked, an empty config reads as no config.
for path in \
    /etc/systemd/system/megalink-display.service \
    "$CONFIG" \
    "$PREFIX/megalink-launch" \
    "$VENV/bin/megalink" \
    "$VENV/bin/pip" \
    "$VENV/bin/python"
do
    if [ ! -e "$path" ]; then
        echo "MISSING  $path"
    elif [ ! -s "$path" ]; then
        echo "EMPTY    $path   <-- this is a fault"
    else
        printf 'ok %7s bytes  %s\n' "$(wc -c < "$path")" "$path"
    fi
done

line "filesystem"
try dmesg -T --level=err,warn
try findmnt -no SOURCE,FSTYPE,OPTIONS /

line "done"
