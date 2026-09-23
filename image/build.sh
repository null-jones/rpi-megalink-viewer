#!/usr/bin/env bash
# Build the Megalink display image: Raspberry Pi OS Lite with the display
# installed, for writing to an SD card with Raspberry Pi Imager.
#
#     image/build.sh
#
# Needs git, uv and Docker. The image is built by pi-gen, Raspberry Pi's own
# tool for building Raspberry Pi OS, in its Docker container. Natively on an
# arm64 machine -- an Apple silicon Mac, a Pi 5, GitHub's arm runners -- and
# under qemu elsewhere, which works but takes hours rather than half of one.
#
# The result is dist/image/megalink-display-<version>.img.xz and its .sha256.

set -euo pipefail

# The pi-gen release the image is built from. Its tags name the Raspberry Pi OS
# release they build; moving to a newer one is a change to this line.
PI_GEN_REF="${PI_GEN_REF:-2026-09-15-raspios-trixie-arm64}"
PI_GEN_URL="${PI_GEN_URL:-https://github.com/RPi-Distro/pi-gen.git}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="${WORK:-$ROOT/build/image}"
OUT="${OUT:-$ROOT/dist/image}"
PIGEN="$WORK/pi-gen"
PAYLOAD="$PIGEN/stage-megalink/00-megalink/files/megalink"
CONTAINER="megalink_pigen"

# --- the wheel ------------------------------------------------------------
# Built fresh, so the image can never carry a stale one left in dist/.
rm -rf "$WORK"
mkdir -p "$WORK/wheel" "$OUT"
(cd "$ROOT" && uv build --wheel --out-dir "$WORK/wheel" >/dev/null)
WHEEL="$(ls "$WORK"/wheel/*.whl)"
VERSION="$(basename "$WHEEL" | cut -d- -f2)"
echo "building the image for megalink $VERSION"

# --- pi-gen ---------------------------------------------------------------
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$PI_GEN_REF" \
    "$PI_GEN_URL" "$PIGEN"
cp -R "$HERE/stage-megalink" "$PIGEN/"
mkdir -p "$PAYLOAD/dist"
cp -R "$ROOT/deploy" "$PAYLOAD/"
cp "$WHEEL" "$PAYLOAD/dist/"
# stage2 is Raspberry Pi OS Lite, and would be exported as an image of its own.
touch "$PIGEN/stage2/SKIP_IMAGES"

# The first user's password only gets past pi-gen's refusal to skip the
# first-boot wizard without one. The stage locks the account, so the password
# is never usable; it is random so that it is not the same in every image
# either.
{
    cat "$HERE/config"
    echo
    echo "IMG_NAME=megalink-display-$VERSION"
    echo "FIRST_USER_PASS=$(openssl rand -hex 24)"
} > "$PIGEN/config"

# --- build ----------------------------------------------------------------
# A container left by a build that was stopped halfway would make pi-gen refuse
# to start. It has this script's own name, so it is only ever one of ours.
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    docker rm -v "$CONTAINER" >/dev/null
fi

# pi-gen decides whether it needs qemu from `uname -m`, and knows arm64 only by
# Linux's name for it. Docker on an Apple silicon Mac runs arm64 containers
# natively, so on one of those pi-gen is told what Linux would have said.
if [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ]; then
    mkdir -p "$WORK/bin"
    cat > "$WORK/bin/uname" <<'UNAME'
#!/bin/sh
[ "$*" = "-m" ] && { echo aarch64; exit 0; }
exec /usr/bin/uname "$@"
UNAME
    chmod 755 "$WORK/bin/uname"
    export PATH="$WORK/bin:$PATH"
fi

(cd "$PIGEN" && CONTAINER_NAME="$CONTAINER" ./build-docker.sh)

# --- result ---------------------------------------------------------------
IMAGE="$(find "$PIGEN/deploy" -name '*.img.xz' | head -1)"
[ -n "$IMAGE" ] || { echo "pi-gen finished without an image" >&2; exit 1; }
FINAL="$OUT/megalink-display-$VERSION.img.xz"
mv "$IMAGE" "$FINAL"
# In the file's own directory, so the .sha256 checks from wherever it is put.
(
    cd "$OUT"
    if command -v sha256sum >/dev/null; then sha256sum "$(basename "$FINAL")"
    else shasum -a 256 "$(basename "$FINAL")"; fi
) > "$FINAL.sha256"
echo
echo "built $FINAL"
cat "$FINAL.sha256"
