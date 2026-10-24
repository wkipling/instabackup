#!/usr/bin/env python3
"""Offline test for x4_bootstrap.py against a mock instaAIP /upload_file.

This does not run X4 code.  It reproduces the recovered handler semantics:
  - GET /upload_file returns the upload form;
  - POST /upload_file reads a multipart body, uses its first line as the
    boundary, takes the filename verbatim, and writes
    <root>/tmp/SD0/DCIM/<filename>;
  - GET /algorithm/... serves <root>/tmp/SD0/<suffix>.

Run: python3 test_x4_bootstrap.py
"""

from __future__ import annotations

import http.server
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BOOTSTRAP = os.path.join(HERE, "x4_bootstrap.py")
LIB = os.path.join(HERE, "libcloudbootstrap.so")


class State:
    root = ""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _send(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/upload_file":
            self._send(200, b"<html><body>Upload File."
                            b"<form method=\"POST\" action=\"/upload_file\">"
                            b"</form></body></html>")
            return
        if path.startswith("/algorithm/"):
            suffix = path[len("/algorithm"):]
            target = os.path.abspath(
                os.path.join(State.root, "tmp", "SD0", suffix.lstrip("/")))
            root = os.path.abspath(State.root) + os.sep
            if not (target + os.sep).startswith(root):
                self._send(403, b"outside")
                return
            try:
                with open(target, "rb") as fh:
                    data = fh.read()
            except OSError:
                self._send(404, b"not found")
                return
            self._send(200, data)
            return
        self._send(404, b"not found")

    def do_POST(self):  # noqa: N802
        if urllib.parse.urlsplit(self.path).path != "/upload_file":
            self._send(404, b"not found")
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        if n < 0x400:
            self._send(400, b"too small")
            return
        if not body.startswith(b"--"):
            self._send(400, b"no multipart")
            return
        first = body.split(b"\r\n", 1)[0]
        boundary = first[2:]
        marker = b'filename="'
        p = body.find(marker)
        if p < 0:
            self._send(400, b"no filename")
            return
        p += len(marker)
        q = body.find(b'"', p)
        if q < 0:
            self._send(400, b"bad filename")
            return
        filename = body[p:q].decode("utf-8", "replace")
        start = q + 1
        # Skip the remainder of the part headers, then the blank line.
        hdr_end = body.find(b"\r\n\r\n", start)
        if hdr_end < 0:
            self._send(400, b"no body")
            return
        data_start = hdr_end + 4
        data_end = body.find(b"\r\n--" + boundary, data_start)
        if data_end < 0:
            self._send(400, b"no close")
            return
        payload = body[data_start:data_end]
        target = os.path.abspath(
            os.path.join(State.root, "tmp", "SD0", "DCIM", filename))
        root = os.path.abspath(State.root) + os.sep
        if not (target + os.sep).startswith(root):
            self._send(400, b"outside")
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        with open(target, "wb") as fh:
            fh.write(payload)
        # The real handler returns an error-shaped 400 "1002" after success.
        self._send(400, b"1002")


def main() -> int:
    if not os.path.isfile(LIB):
        print("build the library first: ./build_x4preload.sh", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        State.root = tmp
        for d in ("tmp/SD0/DCIM", "usr/lib", "etc"):
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            cmd = [sys.executable, BOOTSTRAP, "--camera", "127.0.0.1",
                   "--port", str(port), "--library", LIB, "--timeout", "5"]
            print("+", " ".join(cmd))
            res = subprocess.run(cmd, text=True, capture_output=True)
            print(res.stdout, end="")
            print(res.stderr, end="", file=sys.stderr)
            if res.returncode != 0:
                return res.returncode
            lib = open(LIB, "rb").read()
            got_lib = open(os.path.join(tmp, "usr/lib/libcloudbootstrap.so"),
                           "rb").read()
            assert got_lib == lib, "library was not written verbatim"
            preload = open(os.path.join(tmp, "etc/ld.so.preload"), "rb").read()
            first = preload.split(b"\n", 1)[0]
            assert first == b"/usr/lib/libcloudbootstrap.so", first
            assert len(preload) >= 0x400, len(preload)
            rest = preload.split(b"\n", 1)[1] if b"\n" in preload else b""
            assert rest.strip(b"\r\n") == b"", "unexpected preload data"
            probe = open(os.path.join(tmp, "tmp/SD0/DCIM/.x4-bootstrap-probe"),
                         "rb").read()
            assert probe.startswith(b"x4-bootstrap "), probe

            print("+ disable")
            res = subprocess.run(
                [sys.executable, BOOTSTRAP, "--camera", "127.0.0.1",
                 "--port", str(port), "--disable", "--timeout", "5"],
                text=True, capture_output=True)
            print(res.stdout, end="")
            if res.returncode != 0:
                print(res.stderr, file=sys.stderr)
                return res.returncode
            preload = open(os.path.join(tmp, "etc/ld.so.preload"), "rb").read()
            assert preload.strip(b"\r\n") == b"", "disable left a module"
        finally:
            server.shutdown()
            server.server_close()
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
