#!/bin/sh
# Build the X4 bootstrap preload library.
#
# The target is a freestanding AArch64 ELF shared object with no libc or
# dynamic dependencies.  It is loaded by the camera's glibc ld.so from
# /etc/ld.so.preload and only execs /bin/sh, so it does not care whether the
# card is mounted noexec.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
ZIG=${ZIG:-$ROOT/vendor/pyzig/ziglang/zig}

[ -x "$ZIG" ] || { echo "Zig not found at $ZIG" >&2; exit 1; }

mkdir -p "$ROOT/camera/build"
"$ZIG" cc -target aarch64-linux-gnu -Os -fPIC -shared -nostdlib \
    -fno-stack-protector -fno-builtin -Wl,-soname,libcloudbootstrap.so \
    -Wl,--build-id=none -o "$HERE/libcloudbootstrap.so" "$HERE/x4preload.c"

# Host build for the constructor/fork test.  It uses the x86_64 syscall table
# selected in x4preload.c.
"$ZIG" cc -target x86_64-linux-gnu -Os -fPIC -shared -nostdlib \
    -DX4PRELOAD_TEST -fno-stack-protector -fno-builtin \
    -Wl,-soname,libcloudbootstrap.so -Wl,--build-id=none \
    -o "$ROOT/camera/build/libcloudbootstrap-host.so" "$HERE/x4preload.c"

echo "built:"
ls -l "$HERE/libcloudbootstrap.so" "$ROOT/camera/build/libcloudbootstrap-host.so"
file "$HERE/libcloudbootstrap.so" "$ROOT/camera/build/libcloudbootstrap-host.so"
