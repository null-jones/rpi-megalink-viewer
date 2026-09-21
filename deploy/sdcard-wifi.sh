#!/bin/sh
# Change the Wi-Fi a Raspberry Pi joins, by editing its SD card.
#
# For a Pi that is already installed and has dropped off the network -- moved to
# a new range, a changed password, a typo in the SSID. Run it on the machine
# with the card in it, with the card's *boot* partition mounted:
#
#   ./deploy/sdcard-wifi.sh --boot /Volumes/bootfs \
#       --ssid "range-wifi" --psk "secret" --country GB
#
# Then put the card back and power the Pi up. It joins the new network, and the
# second boot is the normal one.
#
# Why not just write wpa_supplicant.conf? Because Raspberry Pi OS moved to
# NetworkManager in Bookworm, and a boot-partition wpa_supplicant.conf has been
# silently ignored ever since -- it is still the first answer everywhere, and it
# does nothing. The Wi-Fi now lives in a NetworkManager keyfile on the *root*
# partition, which is ext4 and so invisible to macOS and Windows.
#
# So this leaves a script on the boot partition and points the kernel command
# line at it, the way Raspberry Pi Imager does its own first-run setup. Systemd
# runs it as root before the normal boot, it writes the keyfile where
# NetworkManager will find it, removes itself from the command line, and
# reboots. Nothing has to read ext4 from this end.
set -eu

BOOT=""; SSID=""; PSK=""; COUNTRY=""; HIDDEN="false"; PRIORITY=100

usage() {
    cat >&2 <<'USAGE'
usage: sdcard-wifi.sh --boot PATH --ssid NAME [--psk SECRET] --country CC

  --boot PATH     the card's mounted boot partition (e.g. /Volumes/bootfs)
  --ssid NAME     the network to join
  --psk SECRET    its password; omit for an open network
  --country CC    two-letter country code -- the radio stays off without one
  --hidden        the network does not broadcast its name
  --priority N    autoconnect priority (default 100, above anything preset)

Leaves the existing networks configured; this one is simply preferred.
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --boot) BOOT="$2"; shift 2 ;;
        --ssid) SSID="$2"; shift 2 ;;
        --psk) PSK="$2"; shift 2 ;;
        --country) COUNTRY="$2"; shift 2 ;;
        --hidden) HIDDEN="true"; shift ;;
        --priority) PRIORITY="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done

[ -n "$BOOT" ] || { echo "--boot is required" >&2; usage; }
[ -n "$SSID" ] || { echo "--ssid is required" >&2; usage; }
[ -n "$COUNTRY" ] || { echo "--country is required: the radio stays off without one" >&2; usage; }
[ -d "$BOOT" ] || { echo "no such directory: $BOOT" >&2; exit 1; }

# The boot partition, not the root one or the card's parent directory. Getting
# this wrong writes a script nothing will ever run, and the Pi comes up exactly
# as before with nothing to say why.
if [ ! -f "$BOOT/cmdline.txt" ] || [ ! -f "$BOOT/config.txt" ]; then
    echo "$BOOT does not look like a Raspberry Pi boot partition" >&2
    echo "  (expected cmdline.txt and config.txt in it)" >&2
    exit 1
fi

case "$COUNTRY" in
    [A-Za-z][A-Za-z]) ;;
    *) echo "--country must be two letters, e.g. GB, NO, US" >&2; exit 1 ;;
esac
COUNTRY=$(printf '%s' "$COUNTRY" | tr 'a-z' 'A-Z')

# WPA rejects these before the Pi ever tries the network, so say so here rather
# than let it fail silently on a machine with no screen.
if [ -n "$PSK" ]; then
    length=$(printf '%s' "$PSK" | wc -c | tr -d ' ')
    if [ "$length" -lt 8 ] || [ "$length" -gt 63 ]; then
        echo "--psk must be 8 to 63 characters (got $length)" >&2
        exit 1
    fi
fi

# --- cloud-init cards ------------------------------------------------------
# Raspberry Pi Imager now provisions through cloud-init: the card carries
# user-data, meta-data and network-config, and cmdline.txt names the datasource
# (ds=nocloud). Where that is what is in use, edit it rather than working around
# it -- two mechanisms both configuring the network is how a card ends up doing
# something neither of them describes.
#
# The catch is that cloud-init applies per-instance settings once and then
# remembers the instance-id, so an edited network-config alone changes nothing.
# Bumping the instance-id is what makes it a new machine as far as cloud-init
# is concerned, and the network stage runs again.
if [ -f "$BOOT/network-config" ] && [ -f "$BOOT/meta-data" ]; then
    if ! grep -q "access-points:" "$BOOT/network-config"; then
        echo "$BOOT/network-config has no wifis: section to add a network to" >&2
        echo "  edit it by hand, or delete it to use the boot-script method" >&2
        exit 1
    fi

    cp "$BOOT/network-config" "$BOOT/network-config.megalink-backup" 2>/dev/null || true
    cp "$BOOT/meta-data" "$BOOT/meta-data.megalink-backup" 2>/dev/null || true

    # Added alongside whatever is already listed, not instead of it: the Pi
    # should still join the range network when it is carried back there.
    awk -v ssid="$SSID" -v psk="$PSK" -v country="$COUNTRY" '
        BEGIN { added = 0 }
        # Drop any previous entry for this SSID, and the lines belonging to it.
        skip {
            if ($0 ~ /^[ ]{0,10}"/ || $0 ~ /^[ ]{0,8}[a-z]/) { skip = 0 }
            else { next }
        }
        index($0, "\"" ssid "\":") { skip = 1; next }
        { print }
        /access-points:/ && !added {
            indent = $0; sub(/[^ ].*/, "", indent)
            if (psk != "") {
                printf "%s  \"%s\":\n", indent, ssid
                printf "%s    password: \"%s\"\n", indent, psk
            } else {
                # An open network still needs a value: a bare key parses as
                # null, and netplan rejects that rather than reading it as
                # "no security".
                printf "%s  \"%s\": {}\n", indent, ssid
            }
            added = 1
        }
        /regulatory-domain:/ && country != "" {
            # Already printed above; nothing to do -- kept for clarity.
        }
    ' "$BOOT/network-config.megalink-backup" > "$BOOT/network-config"

    if [ -n "$COUNTRY" ]; then
        sed "s/regulatory-domain: \".*\"/regulatory-domain: \"$COUNTRY\"/" \
            "$BOOT/network-config" > "$BOOT/network-config.tmp"
        mv "$BOOT/network-config.tmp" "$BOOT/network-config"
    fi

    printf 'instance-id: megalink-%s\n' "$(date +%Y%m%d%H%M%S)" > "$BOOT/meta-data"

    echo "added \"$SSID\" to $BOOT/network-config"
    echo "bumped the instance-id in $BOOT/meta-data so cloud-init applies it"
    echo "  (originals kept as *.megalink-backup)"
    echo
    echo "Eject the card, put it in the Pi and power up. Give it two minutes:"
    echo "cloud-init re-runs its setup, which takes longer than a normal boot."
    exit 0
fi

# --- everything else -------------------------------------------------------
if command -v uuidgen >/dev/null 2>&1; then
    UUID=$(uuidgen | tr 'A-Z' 'a-z')
else
    UUID=$(od -x /dev/urandom | head -1 | awk '{OFS="-"; print $2$3,$4,$5,$6,$7$8$9}')
fi

SCRIPT="$BOOT/megalink-wifi.sh"

# The keyfile is written by the script rather than by nmcli, because at the
# point systemd runs this NetworkManager is not up: the kernel command line
# sends the first boot to a minimal target, which is the whole reason this
# runs early enough to fix a machine that cannot reach the network.
{
    cat <<'HEAD'
#!/bin/bash
# Written by megalink sdcard-wifi.sh. Runs once, as root, before the normal
# boot, then removes itself. Every step is best-effort: a Pi that comes up on
# the old network is far better than one that does not come up at all.
set -u
exec >>/var/log/megalink-wifi.log 2>&1
echo "=== $(date -u +%FT%TZ) setting up Wi-Fi ==="

BOOTDIR=/boot/firmware
[ -d "$BOOTDIR" ] || BOOTDIR=/boot

HEAD
    printf 'SSID=%s\n' "$(printf '%s' "$SSID" | sed "s/'/'\\\\''/g; s/^/'/; s/\$/'/")"
    printf 'PSK=%s\n' "$(printf '%s' "$PSK" | sed "s/'/'\\\\''/g; s/^/'/; s/\$/'/")"
    printf 'COUNTRY=%s\n' "$COUNTRY"
    printf 'UUID=%s\n' "$UUID"
    printf 'HIDDEN=%s\n' "$HIDDEN"
    printf 'PRIORITY=%s\n' "$PRIORITY"
    cat <<'BODY'

PROFILE=/etc/NetworkManager/system-connections/megalink.nmconnection
mkdir -p /etc/NetworkManager/system-connections

{
    echo "[connection]"
    echo "id=megalink"
    echo "uuid=$UUID"
    echo "type=wifi"
    echo "autoconnect=true"
    echo "autoconnect-priority=$PRIORITY"
    echo
    echo "[wifi]"
    echo "mode=infrastructure"
    echo "ssid=$SSID"
    echo "hidden=$HIDDEN"
    echo
    if [ -n "$PSK" ]; then
        echo "[wifi-security]"
        echo "key-mgmt=wpa-psk"
        echo "psk=$PSK"
        echo
    fi
    echo "[ipv4]"
    echo "method=auto"
    echo
    echo "[ipv6]"
    echo "addr-gen-mode=default"
    echo "method=auto"
    echo
    echo "[proxy]"
} > "$PROFILE"

# NetworkManager refuses to load a keyfile that anyone else can read, and says
# so only in its own log -- which is unreachable on a machine with no network.
chmod 600 "$PROFILE"
chown root:root "$PROFILE" 2>/dev/null || true
echo "wrote $PROFILE for SSID $SSID"

# Without a regulatory domain the radio stays soft-blocked and every scan comes
# back empty, which looks exactly like a wrong password.
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_wifi_country "$COUNTRY" && echo "country set to $COUNTRY"
fi
command -v rfkill >/dev/null 2>&1 && rfkill unblock wifi && echo "radio unblocked"

# Put the command line back, so this is a one-time boot and not every boot.
if [ -f "$BOOTDIR/cmdline.txt" ]; then
    sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.unit=[^ ]*||g' "$BOOTDIR/cmdline.txt"
    echo "restored $BOOTDIR/cmdline.txt"
fi
rm -f "$BOOTDIR/megalink-wifi.sh"

echo "=== done; rebooting into the normal boot ==="
sync
BODY
} > "$SCRIPT"
chmod +x "$SCRIPT" 2>/dev/null || true

# One line, always. A cmdline.txt with a newline in it does not boot, and the
# only symptom is a Pi that never comes up.
CMDLINE="$BOOT/cmdline.txt"
[ -f "$CMDLINE.megalink-backup" ] || cp "$CMDLINE" "$CMDLINE.megalink-backup"
tr -d '\n' < "$CMDLINE.megalink-backup" \
    | sed 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.unit=[^ ]*||g' \
    > "$CMDLINE.tmp"
# /boot/firmware is where Bookworm and later mount this partition, and is the
# path the *running* Pi will see -- not the one it is mounted at here.
printf ' systemd.run=/boot/firmware/megalink-wifi.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target\n' \
    >> "$CMDLINE.tmp"
tr -d '\n' < "$CMDLINE.tmp" > "$CMDLINE"
printf '\n' >> "$CMDLINE"
rm -f "$CMDLINE.tmp"

echo "wrote $SCRIPT"
echo "patched $CMDLINE (original kept as cmdline.txt.megalink-backup)"
echo
echo "Eject the card, put it in the Pi and power up. It boots twice: once to"
echo "apply the Wi-Fi, then normally. Give it about two minutes."
echo
echo "If it still does not appear, put the card back in this machine and read"
echo "$BOOT/../var/log/megalink-wifi.log -- or on the Pi, /var/log/megalink-wifi.log"
