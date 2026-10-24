#!/usr/bin/env python3
"""Offline end-to-end test for insupload + sdcard_receiver.

Builds nothing: run ./build_insupload.sh first.  Starts the receiver on an
ephemeral loopback port, feeds it a fake DCIM tree with the x86_64 build of
the uploader, and checks bytes, resume, filtering, auth, and repair of a
stale server copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPLOADER = HERE / "insupload-host"
RECEIVER = HERE.parent / "server" / "sdcard_receiver.py"
TOKEN = "test-token-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ConnectProxy(threading.Thread):
    """Minimal HTTP CONNECT proxy, standing in for tailscaled."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.targets: list[tuple[str, int]] = []
        self.stopped = False

    def run(self) -> None:
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn: socket.socket) -> None:
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(1)
                if not chunk:
                    return
                data += chunk
            request_line = data.split(b"\r\n", 1)[0].decode()
            method, target, _ = request_line.split()
            assert method == "CONNECT"
            host, port = target.rsplit(":", 1)
            self.targets.append((host, int(port)))
            upstream = socket.create_connection((host, int(port)), timeout=5)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            conn.setblocking(False)
            upstream.setblocking(False)
            while True:
                readable, _, _ = select.select([conn, upstream], [], [], 10)
                if not readable:
                    return
                for src in readable:
                    chunk = src.recv(65536)
                    if not chunk:
                        return
                    (upstream if src is conn else conn).sendall(chunk)
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self.stopped = True
        self.sock.close()


class Receiver:
    def __init__(self, root: Path) -> None:
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(RECEIVER), "--bind", "127.0.0.1",
             "--port", str(self.port), "--root", str(root),
             "--token", TOKEN, "--min-free-mb", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("receiver did not start")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class UploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="insupload-test-"))
        self.media = self.tmp / "DCIM"
        self.recv = self.tmp / "recv"
        self.media.mkdir()
        self.recv.mkdir()
        self.rx = Receiver(self.recv)

    def tearDown(self) -> None:
        self.rx.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_uploader(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(UPLOADER), "--url", self.rx.url, "--root", str(self.media),
             "--token", TOKEN, "--min-age", "0", "--wait", "8", *extra],
            capture_output=True, text=True, timeout=120,
        )

    def test_upload_and_filter(self) -> None:
        big = self.media / "VID_20250101_000001_001.insv"
        small = self.media / "IMG_20250101_000001.jpg"
        ignored = self.media / "notes.txt"
        big.write_bytes(os.urandom(3 * 1024 * 1024 + 123))
        small.write_bytes(os.urandom(200_000))
        ignored.write_text("not media")

        res = self.run_uploader("--once")
        self.assertEqual(res.returncode, 0, res.stderr)

        self.assertTrue((self.recv / big.name).is_file())
        self.assertEqual(sha256(big), sha256(self.recv / big.name))
        self.assertEqual(sha256(small), sha256(self.recv / small.name))
        self.assertFalse((self.recv / ignored.name).exists())
        self.assertFalse(list(self.recv.rglob("*.part")))

    def test_resume_partial(self) -> None:
        name = "VID_20250101_000002_001.insv"
        data = os.urandom(2 * 1024 * 1024 + 7)
        src = self.media / name
        src.write_bytes(data)

        # Simulate a connection that died after 1 MiB.
        part = self.recv / (name + ".part")
        part.write_bytes(data[: 1 << 20])

        res = self.run_uploader("--once")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("from 1048576/", res.stderr)
        self.assertEqual(data, (self.recv / name).read_bytes())
        self.assertFalse(part.exists())

    def test_repairs_stale_complete_copy(self) -> None:
        name = "IMG_20250101_000003.dng"
        src = self.media / name
        src.write_bytes(os.urandom(50_000))
        # Server thinks it already has this name, but with different content.
        (self.recv / name).write_bytes(b"stale")

        res = self.run_uploader("--once")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(sha256(src), sha256(self.recv / name))

    def test_wrong_token_is_rejected(self) -> None:
        (self.media / "IMG_1.jpg").write_bytes(b"x" * 100)
        res = subprocess.run(
            [str(UPLOADER), "--url", self.rx.url, "--root", str(self.media),
             "--token", "wrong", "--min-age", "0", "--wait", "3", "--once"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse((self.recv / "IMG_1.jpg").exists())

    def test_through_connect_proxy(self) -> None:
        proxy = ConnectProxy()
        proxy.start()
        try:
            name = "VID_20250101_000004_001.insv"
            data = os.urandom(1_500_000)
            (self.media / name).write_bytes(data)
            res = self.run_uploader("--once", "--proxy", f"127.0.0.1:{proxy.port}")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(data, (self.recv / name).read_bytes())
            self.assertIn(("127.0.0.1", self.rx.port), proxy.targets)
        finally:
            proxy.close()

    def test_lan_fallback_endpoint(self) -> None:
        name = "VID_20250101_000005_001.insv"
        data = os.urandom(300_000)
        (self.media / name).write_bytes(data)
        # First endpoint points at a closed port; the direct one is valid.
        res = subprocess.run(
            [str(UPLOADER), "--url", "http://127.0.0.1:1",
             "--url2", self.rx.url, "--root", str(self.media),
             "--token", TOKEN, "--min-age", "0", "--wait", "8", "--once"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(data, (self.recv / name).read_bytes())
    def test_dashboard_and_file_list(self) -> None:
        name = "VID_20250101_000006_001.insv"
        data = os.urandom(250_000)
        (self.media / name).write_bytes(data)
        res = self.run_uploader("--once")
        self.assertEqual(res.returncode, 0, res.stderr)

        files = json.loads(urllib.request.urlopen(
            self.rx.url + "/api/files", timeout=5).read())
        match = [row for row in files["files"] if row["path"] == name]
        self.assertEqual(len(match), 1, files)
        self.assertEqual(match[0]["size"], len(data))
        self.assertGreater(match[0]["mtime"], 0)

        html = urllib.request.urlopen(
            self.rx.url + "/", timeout=5).read().decode()
        self.assertNotIn("http-equiv=refresh", html)
        self.assertIn('id="bootstrap"', html)
        self.assertIn("Incoming", html)
        self.assertIn("Filter by path", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
