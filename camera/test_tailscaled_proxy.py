#!/usr/bin/env python3
"""Validate the exact tailscaled flags used on the camera, on this host.

Uses the x86_64 build of the same Tailscale version (vendor/tailscale/amd64).
It does not join a tailnet; it only proves that:

  * `--tun=userspace-networking` starts without /dev/net/tun,
  * `--outbound-http-proxy-listen` binds and speaks HTTP CONNECT,
  * the state/socket flags are accepted.

A real end-to-end tailnet test still needs a login (see README hardware
checks).  This test is skipped if the amd64 binaries are absent.

Usage: python3 test_tailscaled_proxy.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = [
    os.path.join(HERE, "..", "vendor", "tailscale", "amd64",
                 "tailscale_1.104.2_amd64", "tailscaled"),
    "/usr/sbin/tailscaled",
    "/usr/bin/tailscaled",
]


def find_tailscaled() -> str | None:
    for p in CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port: int, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("tailscaled exited with %s" % proc.returncode)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("proxy did not listen on %d" % port)


def main() -> int:
    tsd = find_tailscaled()
    if not tsd:
        print("SKIP: no x86_64 tailscaled found (vendor/tailscale/amd64)")
        return 0

    tmp = tempfile.mkdtemp(prefix="tsproxy-")
    state = os.path.join(tmp, "tailscaled.state")
    sock = os.path.join(tmp, "tailscaled.sock")
    logpath = os.path.join(tmp, "tailscaled.log")
    proxy_port = free_port()

    log = open(logpath, "wb")
    proc = subprocess.Popen([
        tsd,
        "--tun=userspace-networking",
        "--outbound-http-proxy-listen=127.0.0.1:%d" % proxy_port,
        "--state=" + state,
        "--socket=" + sock,
    ], stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_port(proxy_port, proc)

        # Validate the exact `up` flags used by camera/tailscale-up.sh.  Without
        # an auth key this prints a login URL and waits, so a timeout is
        # expected; flag-parsing errors are not.
        cli = os.path.join(os.path.dirname(tsd), "tailscale")
        if os.path.isfile(cli):
            for extra in (["status"], ["ip", "-4"]):
                r = subprocess.run(
                    [cli, "--socket=" + sock] + extra,
                    capture_output=True, text=True, timeout=10)
                out = (r.stdout + r.stderr).strip().replace("\n", " | ")
                print("   tailscale %s rc=%d: %s"
                      % (" ".join(extra), r.returncode, out[:160]))

            up = subprocess.Popen([
                cli, "--socket=" + sock, "up",
                "--hostname=insta360-cam",
                "--accept-dns=false",
                "--accept-routes=false",
                "--netfilter-mode=off",
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                up_out, _ = up.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                up.kill()
                up_out, _ = up.communicate()
            assert "flag provided but not defined" not in up_out, up_out
            assert "invalid value" not in up_out, up_out
            assert "unknown flag" not in up_out, up_out
            print("OK tailscale up flags accepted")

        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as s:
            s.sendall(
                b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n"
                b"Host: 127.0.0.1:443\r\n\r\n"
            )
            s.settimeout(10)
            data = s.recv(512)
        first = data.split(b"\r\n", 1)[0]
        assert first.startswith(b"HTTP/"), data
        print("OK tailscaled userspace proxy responded: %s"
              % first.decode("latin-1"))
        # Not logged in, so the CONNECT must not succeed.  Any HTTP status is
        # acceptable for this flag/plumbing test; record it.
        print("   (unauthenticated tailscaled; CONNECT status is expected to fail)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()
        with open(logpath, "rb") as f:
            text = f.read().decode("utf-8", "replace")
        for line in text.splitlines():
            if "proxy" in line.lower() or "error" in line.lower():
                print("   log: %s" % line)
        print("log: %s" % logpath)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
