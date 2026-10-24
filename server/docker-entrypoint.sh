#!/bin/sh
# Container entrypoint for the direct SD-card receiver.
#
# Environment:
#   SDCARD_ROOT          storage root               (default /data)
#   SDCARD_PORT          listen port                (default 8844)
#   SDCARD_BIND          listen address             (default 0.0.0.0)
#   SDCARD_TOKEN_FILE    optional token file        (default: no auth)
#   SDCARD_MIN_FREE_MB   refuse uploads below this  (receiver default)
set -eu

set -- --root "${SDCARD_ROOT:-/data}" --port "${SDCARD_PORT:-8844}"

if [ -n "${SDCARD_BIND:-}" ]; then
    set -- "$@" --bind "$SDCARD_BIND"
fi

if [ -n "${SDCARD_TOKEN_FILE:-}" ]; then
    if [ ! -f "$SDCARD_TOKEN_FILE" ]; then
        echo "instabackup: token file not found: $SDCARD_TOKEN_FILE" >&2
        exit 1
    fi
    set -- "$@" --token-file "$SDCARD_TOKEN_FILE"
fi

if [ -n "${SDCARD_MIN_FREE_MB:-}" ]; then
    set -- "$@" --min-free-mb "$SDCARD_MIN_FREE_MB"
fi

exec python3 /app/sdcard_receiver.py "$@"
