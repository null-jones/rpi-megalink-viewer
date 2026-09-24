#!/usr/bin/env bash
# Boot a built image and check that a display with no network falls back to
# its hotspot: that the fallback starts at boot, counts down, and asks for the
# hotspot when it should.
#
#     sudo image/boot-test.sh dist/image/megalink-display-0.1.0.img.xz
#
# The image's own systemd is booted in a container (systemd-nspawn) with no
# network at all, as a Pi with no Wi-Fi set and no cable would be. There is no
# Wi-Fi chip in a container, so the hotspot itself is not started; everything
# up to asking NetworkManager for it is. Needs root, systemd-nspawn and nsenter,
# and an arm64 machine.
#
# check.sh looks at the files in an image. This exists because an image whose
# files were all right once shipped with a fallback that never started: its
# service was enabled, and systemd dropped it from every boot over a loop in
# the order things start in, which only booting it shows.

set -euo pipefail

IMAGE="${1:?usage: boot-test.sh IMAGE[.xz]}"
[ "$(id -u)" = 0 ] || { echo "boot-test.sh boots the image: run it as root" >&2; exit 2; }
case "$(uname -m)" in
    aarch64 | arm64) ;;
    *) echo "boot-test.sh runs the image's own programs: it needs an arm64 machine" >&2; exit 2 ;;
esac

# How long the fallback waits for a network before starting the hotspot, and
# how much later than that it may be and still pass: the boot itself, and the
# look round for networks it takes first.
BOOT_GRACE=30
SLACK=60
LIMIT=$((BOOT_GRACE + SLACK + 60))

WORK="$(mktemp -d)"
R="$WORK/root"
B="$WORK/boot"
NSPAWN=""
cleanup() {
    if [ -n "$NSPAWN" ] && kill -0 "$NSPAWN" 2>/dev/null; then
        kill "$NSPAWN" 2>/dev/null || true
        sleep 2
        kill -9 "$NSPAWN" 2>/dev/null || true
    fi
    umount "$B" 2>/dev/null || true
    umount "$R" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# A copy, always: booting changes the image, and this is only a test of it.
case "$IMAGE" in
    *.xz) xz -dc "$IMAGE" > "$WORK/image.img" ;;
    *) cp "$IMAGE" "$WORK/image.img" ;;
esac
RAW="$WORK/image.img"
partition() {
    partx -g -o START,SECTORS -n "$1" "$RAW" \
        | awk '{ printf "offset=%d,sizelimit=%d", $1 * 512, $2 * 512 }'
}
mkdir -p "$R" "$B"
mount -o "loop,$(partition 2)" "$RAW" "$R"
mount -o "loop,$(partition 1)" "$RAW" "$B"

echo "booting $IMAGE with no network"
START=$(date +%s)
# --register=no and --keep-unit so that nspawn asks nothing of the host's
# systemd, which in a container there is none of.
systemd-nspawn --quiet --directory="$R" --boot --private-network \
    --register=no --keep-unit \
    --bind="$B:/boot/firmware" > "$WORK/console.log" 2>&1 &
NSPAWN=$!

# The container's init is nspawn's child. Commands are run inside it with
# nsenter, which needs nothing from the image or from systemd-machined.
LEADER=""
for _ in $(seq 1 30); do
    LEADER="$(pgrep -P "$NSPAWN" | head -1 || true)"
    [ -n "$LEADER" ] && break
    sleep 1
done
[ -n "$LEADER" ] || { echo "the image did not boot:" >&2; cat "$WORK/console.log" >&2; exit 1; }
inside() { nsenter --all --target "$LEADER" -- "$@"; }
elapsed() { echo $(($(date +%s) - START)); }

field() {  # field NAME: one value from the status file, or empty
    # Read with the image's own Python, so this machine needs none.
    inside python3 -c "import json
try: v = json.load(open('/run/megalink/network.json')).get('$1')
except (OSError, ValueError): v = None
print('' if v is None else v)" 2>/dev/null || true
}

FAILED=0
pass() { echo "ok      $1"; }
fail() { echo "FAILED  $1"; FAILED=$((FAILED + 1)); }

COUNTED="" STATUS_AT="" HOTSPOT_AT=""
while [ "$(elapsed)" -lt "$LIMIT" ]; do
    mode="$(field mode)"
    if [ -n "$mode" ] && [ -z "$STATUS_AT" ]; then
        STATUS_AT="$(elapsed)"
        echo "        status after ${STATUS_AT}s: $mode"
    fi
    left="$(field hotspot_in)"
    if [ "$mode" = waiting ] && [ -n "$left" ] && [ -z "$COUNTED" ]; then
        COUNTED="$left"
        echo "        counting down: hotspot in ${left}s"
    fi
    if [ "$mode" = hotspot ]; then
        HOTSPOT_AT="$(elapsed)"
        echo "        hotspot asked for after ${HOTSPOT_AT}s"
        break
    fi
    sleep 2
done

# From the console as well as the journal: systemd finds the loops while it
# plans the boot, before the journal is there to be told.
cycles="$(
    { cat "$WORK/console.log"; inside journalctl -b --no-pager 2>/dev/null; } \
        | grep -i "ordering cycle" | sort -u || true
)"
if [ -z "$cycles" ]; then pass "nothing is dropped from the boot for an ordering cycle"
else fail "ordering cycles at boot:"; echo "$cycles" | sed 's/^/        /'; fi

if [ "$(inside systemctl is-active megalink-netwatch 2>/dev/null)" = active ]; then
    pass "the hotspot fallback is running"
else
    fail "the hotspot fallback is not running"
    { inside systemctl status megalink-netwatch --no-pager 2>&1 || true; } \
        | head -15 | sed 's/^/        /'
fi

if [ -n "$COUNTED" ]; then pass "the fallback counts down to the hotspot"
else fail "no countdown to the hotspot in the status file"; fi

if [ -n "$HOTSPOT_AT" ]; then
    pass "the hotspot is asked for ${HOTSPOT_AT}s after power-on"
else
    fail "no hotspot within ${LIMIT}s of power-on"
    { inside journalctl -b -u megalink-netwatch --no-pager 2>&1 || true; } \
        | tail -15 | sed 's/^/        /'
fi

name="$(inside hostname 2>/dev/null || true)"
case "$name" in
    megalink-????) pass "the display named itself $name" ;;
    *) fail "the display did not name itself: it is ${name:-unknown}" ;;
esac

inside systemctl poweroff >/dev/null 2>&1 || true
for _ in $(seq 1 30); do kill -0 "$NSPAWN" 2>/dev/null || break; sleep 1; done

echo
if [ "$FAILED" -eq 0 ]; then
    echo "the image boots and falls back to its hotspot"
else
    echo "$FAILED check(s) failed"
    exit 1
fi
