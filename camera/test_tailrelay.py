#!/usr/bin/env python3
"""Offline end-to-end test for camera/tailrelay.

Exercises the exact data path used on the camera in Tailscale mode:

    TLS client (SNI service-test.insta360.com)
        -> tailrelay 127.0.0.1:PORT
        -> mock HTTP CONNECT proxy (stands in for tailscaled)
        -> inscloud_server.py over TLS

The relay is a freestanding binary; the host build (tailrelay-host) uses the
same protocol code as the AArch64 build with x86_64 syscall numbers, so this
test validates everything except the architecture-specific syscall stubs.

Usage:
    python3 test_tailrelay.py [--cert ../camera/certs/server.crt ...]
"""

from __future__ import annotations

import argparse
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import inscloud_server as srv  # noqa: E402

HOST = "service-test.insta360.com"
API_PATH = "/cloud-service/cloud/service/camera/upload/config"


class ConnectProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, reject=False):
        self.targets: list[str] = []
        self.reject = reject
        self._lock = threading.Lock()
        super().__init__(addr, _ProxyHandler)


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(10)
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 4096:
            chunk = sock.recv(1024)
            if not chunk:
                return
            buf += chunk
        line = buf.split(b"\r\n", 1)[0].decode("latin-1")
        if not line.startswith("CONNECT "):
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        target = line.split()[1]
        proxy = self.server
        with proxy._lock:
            proxy.targets.append(target)

        if proxy.reject:
            sock.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return

        try:
            host, port = target.rsplit(":", 1)
            upstream = socket.create_connection((host, int(port)), timeout=10)
        except OSError:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        upstream.settimeout(None)
        sock.settimeout(None)

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(sock, upstream), daemon=True)
        t.start()
        pump(upstream, sock)
        t.join(timeout=2)
        upstream.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def tls_get(relay_port: int, ca: str, timeout: float = 10.0) -> bytes:
    ctx = ssl.create_default_context(cafile=ca)
    with socket.create_connection(("127.0.0.1", relay_port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=HOST) as conn:
            req = (
                "GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
                % (API_PATH, HOST)
            ).encode()
            conn.sendall(req)
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            return data


def wait_listen(port: int, proc: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("tailrelay exited with %s" % proc.returncode)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("tailrelay did not listen on %d" % port)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default=os.path.join(HERE, "tailrelay-host"))
    ap.add_argument("--cert", default=os.path.join(HERE, "certs", "server.crt"))
    ap.add_argument("--key", default=os.path.join(HERE, "certs", "server.key"))
    ap.add_argument("--ca", default=os.path.join(HERE, "certs", "ca.crt"))
    args = ap.parse_args()

    for p in (args.relay, args.cert, args.key, args.ca):
        if not os.path.exists(p):
            print("missing %s" % p, file=sys.stderr)
            return 2

    root = tempfile.mkdtemp(prefix="tailrelay-test-")
    cfg = type("A", (), {
        "root": root, "bind": "127.0.0.1", "port": 0,
        "cert": args.cert, "key": args.key, "public_base": "",
        "part_size": 1024, "concurrency": 3,
    })()
    httpd = srv.make_server(cfg)
    api_port = httpd.server_address[1]
    srv.Handler.public_base = "https://%s:%d" % (HOST, api_port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    proxy = ConnectProxy(("127.0.0.1", 0))
    proxy_port = proxy.server_address[1]
    threading.Thread(target=proxy.serve_forever, daemon=True).start()

    relay_port = free_port()
    relay = subprocess.Popen([
        args.relay, str(relay_port), "127.0.0.1", str(api_port),
        "127.0.0.1", str(proxy_port), "127.0.0.1",
    ])
    try:
        wait_listen(relay_port, relay)

        # A connection that is opened only to probe the listener has already
        # produced one CONNECT; that is fine, but record the count now.
        before = len(proxy.targets)
        data = tls_get(relay_port, args.ca)
        head = data.split(b"\r\n", 1)[0]
        assert b"200" in head, head
        assert b"getUploadConfig" in data or b"reportUploadSpeedConfig" in data, data[:400]
        assert len(proxy.targets) > before, "no CONNECT was made"
        assert proxy.targets[-1] == "127.0.0.1:%d" % api_port, proxy.targets[-1]
        print("OK single request: %s" % head.decode())

        # Concurrent connections must each get their own CONNECT and reply.
        results: list[bytes] = []
        errors: list[BaseException] = []

        def worker():
            try:
                results.append(tls_get(relay_port, args.ca))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, errors
        assert len(results) == 4
        assert all(b"200" in r.split(b"\r\n", 1)[0] for r in results)
        print("OK 4 concurrent requests")

        # The watcher uses --probe to avoid starting an upload before the
        # tailnet path works.
        probe = subprocess.run([
            args.relay, "--probe", "127.0.0.1", str(api_port),
            "127.0.0.1", str(proxy_port),
        ], capture_output=True, text=True, timeout=10)
        assert probe.returncode == 0, probe
        assert "probe ok" in (probe.stdout + probe.stderr), (probe.stdout, probe.stderr)
        print("OK probe through proxy")

        # A refused CONNECT must not hand the client a working TLS session.
        bad_proxy = ConnectProxy(("127.0.0.1", 0), reject=True)
        bad_port = bad_proxy.server_address[1]
        threading.Thread(target=bad_proxy.serve_forever, daemon=True).start()
        bad_relay_port = free_port()
        bad_relay = subprocess.Popen([
            args.relay, str(bad_relay_port), "127.0.0.1", str(api_port),
            "127.0.0.1", str(bad_port), "127.0.0.1",
        ])
        try:
            wait_listen(bad_relay_port, bad_relay)
            try:
                tls_get(bad_relay_port, args.ca, timeout=5)
            except (ssl.SSLError, OSError):
                print("OK refused CONNECT is rejected")
            else:
                raise AssertionError("TLS succeeded despite refused CONNECT")
            bad_probe = subprocess.run([
                args.relay, "--probe", "127.0.0.1", str(api_port),
                "127.0.0.1", str(bad_port),
            ], capture_output=True, text=True, timeout=10)
            assert bad_probe.returncode == 1, bad_probe
            print("OK refused probe fails")
        finally:
            bad_relay.terminate()
            bad_relay.wait(timeout=5)
    finally:
        relay.terminate()
        relay.wait(timeout=5)
        proxy.shutdown()
        httpd.shutdown()

        # The relay forks one process per connection; after SIGTERM none may
        # remain listening.
        time.sleep(0.2)
        print("log: %s" % root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
