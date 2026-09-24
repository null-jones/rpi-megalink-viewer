#!/bin/sh
# The text console's login screen, as agetty shows it, in an xterm with the
# Linux console's colours. Run by screenshots.sh.
set -eu
OUT="${1:-docs/guide/images}"
export PYTHONPATH=src
cat > /tmp/show.py <<'PY'
import sys, time
from megalink_viewer import banner
text = banner.issue(8080).replace("\\4", "192.168.1.23").replace("\\n", "megalink-a199")
sys.stdout.write(text + "megalink-a199 login: ")
sys.stdout.flush()
time.sleep(30)
PY
Xvfb :8 -screen 0 1100x330x24 -nolisten tcp >/dev/null 2>&1 &
XVFB=$!
sleep 1.5
DISPLAY=:8 xterm -geometry 120x18+0+0 -bg black -fg '#aaaaaa' -fa 'DejaVu Sans Mono' -fs 11 \
    -xrm 'XTerm*color6: #00aaaa' -xrm 'XTerm*color7: #aaaaaa' -xrm 'XTerm*color1: #aa0000' \
    -e python3 /tmp/show.py &
TERM_PID=$!
sleep 3
DISPLAY=:8 import -window root "$OUT/console-login.png"
kill "$TERM_PID" "$XVFB" 2>/dev/null || true
echo "   console-login"
