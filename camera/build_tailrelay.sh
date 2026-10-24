#!/bin/sh
# Build tailrelay for the camera (AArch64) and for the host (x86_64 test).
#
# Uses clang's cross-target support; no cross libc is needed because the
# program is freestanding and only uses raw Linux syscalls.
#
#   ./build_tailrelay.sh
#
# Outputs:
#   tailrelay        - AArch64 ELF for the camera
#   tailrelay-host   - x86_64 ELF used by test_tailrelay.py
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)

# Prefer Zig: it bundles LLD and can cross-link AArch64 on a plain x86_64
# host without a cross libc.  Fall back to clang only if its driver has a
# working AArch64 linker.
if [ -z "${ZIG:-}" ]; then
    if [ -x "$HERE/../vendor/pyzig/ziglang/zig" ]; then
        ZIG="$HERE/../vendor/pyzig/ziglang/zig"
    elif command -v zig >/dev/null 2>&1; then
        ZIG=zig
    fi
fi

COMMON="-O2 -ffreestanding -fno-builtin -fno-stack-protector \
        -fno-asynchronous-unwind-tables -fno-unwind-tables \
        -fno-ident -nostdlib -static -Wl,--build-id=none \
        -Wl,-e,_start -Wl,-z,noexecstack"

if [ -n "${ZIG:-}" ]; then
    # shellcheck disable=SC2086
    "$ZIG" cc -target aarch64-linux-musl $COMMON \
        -o "$HERE/tailrelay" "$HERE/tailrelay.c"
    # Host build is only for the offline relay test.
    # shellcheck disable=SC2086
    "$ZIG" cc $COMMON -no-pie \
        -o "$HERE/tailrelay-host" "$HERE/tailrelay.c"
else
    CC=${CC:-clang}
    # shellcheck disable=SC2086
    $CC --target=x86_64-linux-gnu $COMMON -no-pie \
        -o "$HERE/tailrelay-host" "$HERE/tailrelay.c"
    echo "ERROR: no Zig found; cannot cross-link tailrelay for AArch64" >&2
    exit 1
fi

chmod +x "$HERE/tailrelay" "$HERE/tailrelay-host"
echo "built:"
ls -l "$HERE/tailrelay" "$HERE/tailrelay-host"
file "$HERE/tailrelay" | cut -c1-120
