#!/bin/sh
# Prepare a freshly flashed Raspberry Pi OS card so the Pi comes up on the
# network already knowing which firing point it is.
#
# Run this on the machine that flashed the card, with the card's boot partition
# mounted:
#
#   ./deploy/provision-sdcard.sh --boot /Volumes/bootfs \
#       --ssid "range-wifi" --psk "secret" --country NO \
#       --host stord-pk --range 1-10 --lane 9 --name firing-point-9
#
# What it writes:
#
#   ssh                  an empty file, which enables the SSH server
#   megalink.json        the display configuration, picked up on first boot
#   megalink-wifi.sh     Wi-Fi, applied by a one-shot boot script
#   firstrun-megalink    a script the first boot runs, if --repo is given
#
# Raspberry Pi Imager's own settings screen does the Wi-Fi, user and SSH parts
# properly, and is still the easiest route for a card you are flashing anyway.
# The --ssid option here is for a card flashed without it -- or one already in
# use, where Imager is no longer an option. See deploy/sdcard-wifi.sh.
set -eu

BOOT=""; SSID=""; PSK=""; COUNTRY=""
HOST=""; RANGE=""; LANE=""; NAME=""; MODE=gui; TOKEN=""
REPO=""

usage() {
    cat >&2 <<'USAGE'
usage: provision-sdcard.sh --boot PATH [options]

  --boot PATH     the card's mounted boot partition (required)
  --ssid NAME     Wi-Fi network                 --psk SECRET   Wi-Fi password
  --country CC    two-letter country code, required by the Wi-Fi regulations
  --host SLUG     club to display               --range KEY    range to display
  --lane N        firing point                  --name NAME    name for this display
  --mode gui|terminal                           --token SECRET config page secret
  --repo URL      clone this repository on first boot and install from it
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --boot) BOOT="$2"; shift 2 ;;
        --ssid) SSID="$2"; shift 2 ;;
        --psk) PSK="$2"; shift 2 ;;
        --country) COUNTRY="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --range) RANGE="$2"; shift 2 ;;
        --lane) LANE="$2"; shift 2 ;;
        --name) NAME="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done

[ -n "$BOOT" ] || usage
[ -d "$BOOT" ] || { echo "$BOOT is not a directory" >&2; exit 1; }
# Sanity check that this really is a Pi boot partition and not someone's home
# directory, because everything below writes into it.
if [ ! -e "$BOOT/config.txt" ] && [ ! -e "$BOOT/cmdline.txt" ]; then
    echo "$BOOT does not look like a Raspberry Pi boot partition" >&2
    exit 1
fi

# --- SSH ------------------------------------------------------------------
touch "$BOOT/ssh"
echo "enabled SSH"

# --- Wi-Fi ----------------------------------------------------------------
if [ -n "$SSID" ]; then
    # Handed to sdcard-wifi.sh, which writes a NetworkManager keyfile by way of
    # a one-shot boot script. This used to write wpa_supplicant.conf, which
    # Bookworm and later ignore in silence -- the Pi comes up, joins nothing,
    # and says nothing about why.
    # CDPATH is unset rather than assigned inline: a set CDPATH can send cd
    # somewhere other than the directory named, and the inline form reads as a
    # typo to anyone (and to shellcheck).
    HERE=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)
    set -- --boot "$BOOT" --ssid "$SSID" --country "$COUNTRY"
    [ -n "$PSK" ] && set -- "$@" --psk "$PSK"
    "$HERE/sdcard-wifi.sh" "$@"
fi

# --- the display configuration -------------------------------------------
# Written as JSON by hand rather than through the package, so this script works
# on a machine that does not have the package installed.
quote() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
cat > "$BOOT/megalink.json" <<JSON
{
  "host": "$(quote "$HOST")",
  "range": "$(quote "$RANGE")",
  "lane": "$(quote "$LANE")",
  "display": { "mode": "$(quote "$MODE")", "fullscreen": true, "interval": 0.5 },
  "web": { "enabled": true, "port": 8080, "bind": "0.0.0.0", "token": "$(quote "$TOKEN")" },
  "beacon": { "enabled": true, "port": 45455, "interval": 10, "name": "$(quote "$NAME")" }
}
JSON
echo "wrote megalink.json"

# --- first boot -----------------------------------------------------------
if [ -n "$REPO" ]; then
    cat > "$BOOT/firstrun-megalink.sh" <<FIRSTRUN
#!/bin/sh
# Runs once, on the Pi, the first time it boots with a network.
set -eu
BOOTDIR=/boot/firmware
[ -d "\$BOOTDIR" ] || BOOTDIR=/boot
apt-get update -qq
apt-get install -y --no-install-recommends git
rm -rf /opt/megalink-src
git clone --depth 1 "$REPO" /opt/megalink-src
install -d /etc/megalink
[ -f "\$BOOTDIR/megalink.json" ] && install -m 0640 "\$BOOTDIR/megalink.json" /etc/megalink/display.json
/opt/megalink-src/deploy/install.sh --mode "$MODE"
rm -f "\$BOOTDIR/firstrun-megalink.sh"
FIRSTRUN
    chmod +x "$BOOT/firstrun-megalink.sh"
    echo "wrote firstrun-megalink.sh"
    cat <<NOTE

  One manual step is left: Raspberry Pi OS only runs its own firstrun.sh, so
  add this line near the end of that file (before it deletes itself), or run
  the script yourself over SSH the first time:

      /boot/firmware/firstrun-megalink.sh

NOTE
fi

echo "card ready -- eject it and boot the Pi"
