#!/bin/sh
# Take the guide's screenshots again, in a Debian container with the X server,
# Tk and Chromium they need:
#
#     docs/guide/tools/screenshots.sh
#
# Needs Docker. The pictures land in docs/guide/images/, replacing the old ones.
set -eu
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
docker run --rm -v "$ROOT":/src -w /src debian:trixie sh -c '
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends python3 python3-tk python3-pytest \
        xvfb xauth imagemagick fonts-dejavu-core chromium xterm >/dev/null 2>&1
    python3 docs/guide/tools/screens.py docs/guide/images
    sh docs/guide/tools/console.sh docs/guide/images
    chown -R "$(stat -c %u:%g /src)" docs/guide/images
'
