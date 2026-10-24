#!/bin/sh
# Build insupload for the camera (AArch64) and for host tests (x86_64).
#
#   ./build_insupload.sh
#
# Outputs:
#   insupload       - AArch64 static musl ELF for the camera
#   insupload-host  - x86_64 static musl ELF for test_sdcard_push.py
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)

if [ -z "${ZIG:-}" ]; then
    if [ -x "$HERE/../vendor/pyzig/ziglang/zig" ]; then
        ZIG="$HERE/../vendor/pyzig/ziglang/zig"
    elif command -v zig >/dev/null 2>&1; then
        ZIG=zig
    fi
fi

if [ -z "${ZIG:-}" ]; then
    echo "ERROR: zig not found (needed for static aarch64 + x86_64 builds)" >&2
    exit 1
fi

COMMON="-O2 -static -Wall -Wextra -Wno-unused-parameter"

# shellcheck disable=SC2086
"$ZIG" cc -target aarch64-linux-musl $COMMON -o "$HERE/insupload" "$HERE/insupload.c"
# shellcheck disable=SC2086
"$ZIG" cc -target x86_64-linux-musl $COMMON -o "$HERE/insupload-host" "$HERE/insupload.c"

chmod +x "$HERE/insupload" "$HERE/insupload-host"
echo "built:"
ls -l "$HERE/insupload" "$HERE/insupload-host"
file "$HERE/insupload" | cut -c1-120
