#!/usr/bin/env bash
# Look inside a built image and check it is what it says it is: a display that
# starts on its own, names itself, and that nobody can log in to.
#
#     sudo image/check.sh dist/image/megalink-display-0.1.0.img.xz
#
# Needs root (it mounts the image, read-only), and an arm64 machine for the
# checks that run the image's own Python; elsewhere those are skipped.

set -euo pipefail

IMAGE="${1:?usage: check.sh IMAGE[.xz]}"
[ "$(id -u)" = 0 ] || { echo "check.sh mounts the image: run it as root" >&2; exit 2; }

WORK="$(mktemp -d)"
R="$WORK/root"
cleanup() {
    umount "$R/boot/firmware" 2>/dev/null || true
    umount "$R" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

case "$IMAGE" in
    *.xz) xz -dc "$IMAGE" > "$WORK/image.img"; RAW="$WORK/image.img" ;;
    *) RAW="$IMAGE" ;;
esac

# By offset rather than through losetup -P, whose partition devices do not
# appear inside a container. With the size too: two loop devices that both run
# to the end of the file overlap, and the kernel will not mount the second.
partition() {
    partx -g -o START,SECTORS -n "$1" "$RAW" \
        | awk '{ printf "offset=%d,sizelimit=%d", $1 * 512, $2 * 512 }'
}
mkdir -p "$R"
mount -o "ro,noload,loop,$(partition 2)" "$RAW" "$R"
mount -o "ro,loop,$(partition 1)" "$RAW" "$R/boot/firmware"

FAILED=0
pass() { echo "ok      $1"; }
fail() { echo "FAILED  $1"; FAILED=$((FAILED + 1)); }
check() {  # check DESCRIPTION COMMAND...
    local what="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi
}
enabled() { [ "$(systemctl --root="$R" is-enabled "$1" 2>/dev/null)" = enabled ]; }
not_enabled() { ! enabled "$1"; }
installed() {
    dpkg-query --admindir="$R/var/lib/dpkg" -W -f='${Status}' "$1" 2>/dev/null \
        | grep -q "install ok installed"
}

echo "checking $IMAGE"

# Starts on its own.
for unit in megalink-display megalink-netwatch megalink-firstboot; do
    check "$unit is enabled" enabled "$unit.service"
done
for package in python3-tk xserver-xorg xinit network-manager iw chromium; do
    check "$package is installed" installed "$package"
done
check "the display's user exists" grep -q '^megalink:' "$R/etc/passwd"
check "the renaming script is installed" test -x "$R/opt/megalink/megalink-firstboot"

# Names itself: the first boot renames "megalink" to megalink-XXXX.
check "the hostname is megalink until first boot" \
    test "$(cat "$R/etc/hostname")" = megalink

# Nobody can log in. Every account's password is locked or was never set, SSH
# is off, and the first-boot wizard -- which wants a keyboard -- does not run.
unlocked="$(awk -F: '$2 != "" && $2 !~ /^[!*]/ { print $1 }' "$R/etc/shadow")"
if [ -z "$unlocked" ]; then pass "no account has a usable password"
else fail "accounts with a usable password: $(echo "$unlocked" | tr '\n' ' ')"; fi
check "SSH is not enabled" not_enabled ssh.service
check "no ssh file on the boot partition" \
    test ! -e "$R/boot/firmware/ssh" -a ! -e "$R/boot/firmware/ssh.txt"
check "the first-boot user wizard is not enabled" not_enabled userconfig.service

# Imager's settings still arrive: they come in through cloud-init.
check "cloud-init is installed" installed rpi-cloud-init-mods
check "the cloud-init seed is on the boot partition" \
    test -e "$R/boot/firmware/meta-data" -a -e "$R/boot/firmware/user-data"

# And the display itself runs. The image's own binaries only run on arm64.
case "$(uname -m)" in
    aarch64 | arm64)
        check "the display runs" \
            chroot "$R" /opt/megalink/venv/bin/python -m megalink_viewer --version
        check "Tk is importable" chroot "$R" /opt/megalink/venv/bin/python -c "import tkinter"
        ;;
    *) echo "skipped running the display: this is not an arm64 machine" ;;
esac

echo
if [ "$FAILED" -eq 0 ]; then
    echo "the image looks right"
else
    echo "$FAILED check(s) failed"
    exit 1
fi
