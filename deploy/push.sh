#!/bin/sh
# Copy this checkout to a Raspberry Pi over SSH and install it there.
#
#   ./deploy/push.sh pi@fp-09 --host stord-pk --range 1-10 --lane 9
#
# Nothing is fetched on the Pi: the wheel is built here and copied across, so
# the Pi needs no source control, no credentials of yours, and no build tools.
# Anything after the destination is passed straight to deploy/install.sh.
#
#   --no-install   copy only, do not install or restart
#   --no-build     skip building the wheel (installs from source on the Pi)
set -eu

DEST=""
BUILD=yes
INSTALL=yes
REMOTE_DIR=megalink-viewer

usage() {
    cat >&2 <<'USAGE'
usage: push.sh [user@]host[:path] [--no-build] [--no-install] [install.sh options]

  ./deploy/push.sh pi@fp-09
  ./deploy/push.sh pi@fp-09 --host stord-pk --range 1-10 --lane 9
  ./deploy/push.sh pi@fp-09 --no-install          # just copy the files
USAGE
    exit 2
}

[ $# -gt 0 ] || usage
DEST="$1"; shift

# Split an optional :path off the destination.
case "$DEST" in
    *:*) REMOTE_DIR="${DEST#*:}"; DEST="${DEST%%:*}" ;;
esac

INSTALL_ARGS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-build) BUILD=no; shift ;;
        --no-install) INSTALL=no; shift ;;
        -h|--help) usage ;;
        *) INSTALL_ARGS="$INSTALL_ARGS $1"; shift ;;
    esac
done

SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SOURCE"

if [ "$BUILD" = yes ]; then
    echo "building the wheel"
    rm -rf dist
    if command -v uv >/dev/null 2>&1; then
        uv build --wheel >/dev/null
    else
        python3 -m pip install --quiet --upgrade build >/dev/null 2>&1 || true
        python3 -m build --wheel >/dev/null
    fi
    # The wheel is py3-none-any, so the machine that built it does not matter.
    ls -1 dist/*.whl >/dev/null 2>&1 || { echo "no wheel was produced" >&2; exit 1; }
fi

echo "copying to $DEST:$REMOTE_DIR"
# The virtual environment is the bulk of the checkout and is useless on another
# machine, let alone another architecture. The caches are noise.
rsync -az --delete \
    --exclude '.venv/' \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache/' \
    --exclude '.ruff_cache/' \
    --exclude '.scratch/' \
    --exclude '*.egg-info/' \
    ./ "$DEST:$REMOTE_DIR/"

if [ "$INSTALL" = no ]; then
    echo "copied; install it with:"
    echo "  ssh $DEST 'cd $REMOTE_DIR && sudo ./deploy/install.sh'"
    exit 0
fi

echo "installing on $DEST"
# -t so sudo can prompt for a password on the Pi.
# shellcheck disable=SC2029
ssh -t "$DEST" "cd '$REMOTE_DIR' && chmod +x deploy/*.sh deploy/megalink-launch && sudo ./deploy/install.sh$INSTALL_ARGS"
