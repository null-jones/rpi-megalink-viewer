#!/bin/sh
# Build the printable guide in a Debian container, as CI does:
#
#     docs/guide/tools/pdf.sh
#
# Needs Docker. Writes dist/megalink-viewer-guide-<version>.pdf.
set -eu
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
docker run --rm -v "$ROOT":/src -w /src debian:trixie sh -c '
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends python3 python3-markdown weasyprint \
        fonts-dejavu-core fonts-inter >/dev/null 2>&1
    python3 docs/guide/build.py
    chown -R "$(stat -c %u:%g /src)" dist
'
