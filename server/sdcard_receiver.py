#!/usr/bin/env python3
"""Resumable HTTP sink for the Insta360 SD-card uploader.

The camera joins a phone hotspot, brings up Tailscale, and pushes new media
here over the tailnet.  No port forwarding is involved: the camera dials the
tailnet address directly, and WireGuard provides the encryption.

Endpoints (all require X-Auth-Token when --token-file/--token is used):

  GET  /api/ping
  GET  /api/stat?path=DCIM/Camera01/VID_....insv
       -> {"path":..., "size":N, "final":true|false}
  PUT  /api/upload?path=...      X-Offset: N  X-Total-Size: M
       body is the next M-N bytes at offset N.  Partial data lives in
       <root>/<path>.part and is renamed to <root>/<path> when complete.
  POST /api/reset?path=...
       -> discard a partial upload (the camera does this if its local file
          is shorter than the server's partial copy)
  GET  /api/list?prefix=...
       -> finished files, for debugging

Design notes:
  * offsets are absolute; a dropped hotspot connection resumes from the
    server-reported size instead of restarting a multi-gigabyte file;
  * the camera is the only writer, but per-path locks keep retries safe;
  * a complete file is never overwritten unless it is size-identical.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

MAX_CHUNK = 32 << 20
CHUNK_COPY = 256 << 10
DEFAULT_MIN_FREE_MB = 512


def log(msg: str) -> None:
    sys.stdout.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stdout.flush()


class Store:
    def __init__(self, root: str, min_free_mb: int) -> None:
        self.root = os.path.abspath(root)
        self.min_free_mb = min_free_mb
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def lock(self, rel: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(rel)
            if lock is None:
                lock = threading.Lock()
                self._locks[rel] = lock
            return lock

    def resolve(self, rel: str) -> str | None:
        if not rel or len(rel) > 4096 or "\x00" in rel:
            return None
        rel = rel.replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            return None
        full = os.path.abspath(os.path.join(self.root, *parts))
        if full != self.root and not full.startswith(self.root + os.sep):
            return None
        return full

    def final_path(self, full: str) -> str:
        return full

    def part_path(self, full: str) -> str:
        return full + ".part"

    def state(self, full: str) -> tuple[int, bool]:
        if os.path.isfile(full):
            return os.path.getsize(full), True
        part = self.part_path(full)
        if os.path.isfile(part):
            return os.path.getsize(part), False
        return 0, False

    def free_ok(self, need: int) -> bool:
        free = shutil.disk_usage(self.root).free
        return free >= need + self.min_free_mb * (1 << 20)

    def reset(self, full: str, all_copies: bool = False) -> None:
        targets = [self.part_path(full)]
        if all_copies:
            targets.append(full)
        for p in targets:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


class Status:
    """Live upload state for the dashboard and for readable log lines."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.started = time.time()
        self.lock = threading.Lock()
        self.active: dict[str, dict] = {}
        self.completed: dict[str, int] = {}
        self.partials: dict[str, int] = {}
        self.last_activity = 0.0
        self._scan()

    def _scan(self) -> None:
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if name.endswith(".part"):
                    self.partials[rel] = size
                else:
                    self.completed[rel] = size

    def part(self, rel: str, offset: int, total: int, chunk: int) -> float:
        now = time.time()
        self.last_activity = now
        with self.lock:
            self.partials[rel] = offset
            a = self.active.get(rel)
            if a is None:
                a = {"offset": 0, "total": total, "started": now,
                     "t0": now, "b0": max(0, offset - chunk), "speed": 0.0}
                self.active[rel] = a
            a["offset"] = offset
            a["total"] = total
            dt = now - a["t0"]
            if dt >= 1.0:
                a["speed"] = (offset - a["b0"]) / dt
            return a["speed"]

    def done(self, rel: str, size: int) -> float:
        now = time.time()
        self.last_activity = now
        with self.lock:
            a = self.active.pop(rel, None)
            self.partials.pop(rel, None)
            self.completed[rel] = size
            if a:
                return size / max(0.001, now - a["started"]) / (1 << 20)
            return 0.0

    def skip(self, rel: str, size: int) -> None:
        self.last_activity = time.time()
        with self.lock:
            self.active.pop(rel, None)
            self.partials.pop(rel, None)
            self.completed[rel] = size

    def reset(self, rel: str) -> None:
        self.last_activity = time.time()
        with self.lock:
            self.active.pop(rel, None)
            self.partials.pop(rel, None)

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            active = []
            for rel, a in self.active.items():
                pct = (a["offset"] * 100.0 / a["total"]) if a["total"] else 0.0
                eta = None
                if a["speed"] > 0 and a["total"] > a["offset"]:
                    eta = int((a["total"] - a["offset"]) / a["speed"])
                active.append({"path": rel, "offset": a["offset"],
                               "total": a["total"], "percent": round(pct, 1),
                               "mbps": round(a["speed"] / (1 << 20), 2),
                               "eta_s": eta})
            return {
                "uptime_s": int(now - self.started),
                "last_activity_s_ago": int(now - self.last_activity) if self.last_activity else None,
                "files_done": len(self.completed),
                "bytes_done": sum(self.completed.values()),
                "active": sorted(active, key=lambda x: x["path"]),
                "partials": [{"path": k, "bytes": v}
                             for k, v in sorted(self.partials.items())],
                "disk_free": shutil.disk_usage(self.root).free,
            }


def _gb(n: int) -> str:
    return "%.2f GB" % (n / (1 << 30)) if n >= (1 << 30) else "%.1f MB" % (n / (1 << 20))


def dashboard_html(s: dict) -> str:
    rows = []
    for a in s["active"]:
        eta = ""
        if a["eta_s"] is not None:
            eta = "%dm %02ds" % (a["eta_s"] // 60, a["eta_s"] % 60)
        rows.append(
            "<tr><td class=p>%s</td><td>%.1f%%</td>"
            "<td><div class=bar><i style=\"width:%.1f%%\"></i></div></td>"
            "<td>%.2f MB/s</td><td>%s</td></tr>"
            % (a["path"], a["percent"], a["percent"], a["mbps"], eta))
    active = "".join(rows) or "<tr><td colspan=5 class=idle>idle</td></tr>"
    partials = "".join(
        "<li>%s <span class=muted>%s stored</span></li>" % (p["path"], _gb(p["bytes"]))
        for p in s["partials"][:20])
    if not partials:
        partials = "<li class=idle>none</li>"
    last = ("%ds ago" % s["last_activity_s_ago"]
            if s["last_activity_s_ago"] is not None else "never")
    return """<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=2>
<title>Insta360 backup</title><style>
body{font:14px system-ui,sans-serif;background:#111;color:#ddd;margin:24px}
 h1{font-size:18px;font-weight:600;margin:0 0 4px}
 .sum{color:#999;margin-bottom:18px} .sum b{color:#eee;font-weight:600}
 table{width:100%%;border-collapse:collapse} td{padding:6px 8px;border-bottom:1px solid #222}
 .p{font-family:ui-monospace,monospace;font-size:12px;max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .bar{background:#222;height:8px;border-radius:4px;overflow:hidden;min-width:140px}
 .bar i{display:block;height:8px;background:#4ade80}
 .idle{color:#666} .muted{color:#888} ul{padding-left:18px;color:#bbb}
</style></head><body>
<h1>Insta360 SD backup</h1>
<div class=sum><b>%d</b> files &middot; <b>%s</b> stored &middot;
 free <b>%s</b> &middot; last activity %s &middot; up %dm</div>
<table><tr><td class=muted>file</td><td class=muted>done</td><td></td>
<td class=muted>speed</td><td class=muted>eta</td></tr>%s</table>
<h1 style="margin-top:22px">Resumable partials</h1><ul>%s</ul>
</body></html>""" % (
        s["files_done"], _gb(s["bytes_done"]), _gb(s["disk_free"]),
        last, s["uptime_s"] // 60, active, partials)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "insta360-sdcard-sink/1"

    store: Store
    token: str | None
    status: Status

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def setup(self) -> None:
        super().setup()
        # A camera whose Wi-Fi drops mid-chunk can leave a half-open socket.
        # Without a timeout the handler (and the per-path lock it holds) waits
        # forever, and every retry queues behind it.
        self.connection.settimeout(60)

    # -- helpers ---------------------------------------------------------
    def _url(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return parts.path, parse_qs(parts.query)

    def _rel(self, query: dict[str, list[str]]) -> str | None:
        return query.get("path", [None])[0]

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _html(self, body: str) -> None:
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _authorized(self) -> bool:
        if not self.token:
            return True
        supplied = self.headers.get("X-Auth-Token", "")
        return hmac.compare_digest(supplied, self.token)

    def _deny(self) -> None:
        log("%s DENY %s %s" % (self.client_address[0], self.command, self.path))
        self._json(401, {"error": "unauthorized"})

    # -- verbs -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path, query = self._url()
        # Read-only status is deliberately unauthenticated so a phone can open
        # the dashboard; it exposes only media names and byte counts.
        if path in ("/", "/index.html"):
            self._html(dashboard_html(self.status.snapshot()))
            return
        if path == "/api/status":
            self._json(200, self.status.snapshot())
            return
        if not self._authorized():
            self._deny()
            return
        if path in ("/api/ping", "/actuator/ping"):
            self._json(200, {"ok": True, "service": "insta360-sdcard-sink"})
            return
        if path == "/api/stat":
            rel = self._rel(query)
            full = self.store.resolve(rel or "")
            if full is None:
                self._json(400, {"error": "bad path"})
                return
            size, final = self.store.state(full)
            self._json(200, {"path": rel, "size": size, "final": final})
            return
        if path == "/api/list":
            prefix = query.get("prefix", [""])[0].lstrip("/")
            out = []
            base = self.store.resolve(prefix) if prefix else self.store.root
            if base and os.path.isdir(base):
                for dirpath, _dirs, files in os.walk(base):
                    for name in files:
                        if name.endswith(".part"):
                            continue
                        full = os.path.join(dirpath, name)
                        rel = os.path.relpath(full, self.store.root).replace(os.sep, "/")
                        out.append({"path": rel, "size": os.path.getsize(full)})
            self._json(200, {"files": out})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path, query = self._url()
        if not self._authorized():
            self._deny()
            return
        if path == "/api/reset":
            rel = self._rel(query)
            full = self.store.resolve(rel or "")
            if full is None:
                self._json(400, {"error": "bad path"})
                return
            all_copies = query.get("all", ["0"])[0] in ("1", "true", "yes")
            with self.store.lock(rel or ""):
                self.store.reset(full, all_copies=all_copies)
            self.status.reset(rel or "")
            log("RESET %s (all=%d)" % (rel, all_copies))
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        path, query = self._url()
        if not self._authorized():
            self._deny()
            return
        if path != "/api/upload":
            self._json(404, {"error": "not found"})
            return
        rel = self._rel(query) or ""
        full = self.store.resolve(rel)
        if full is None:
            self._json(400, {"error": "bad path"})
            return
        try:
            offset = int(self.headers.get("X-Offset", "0"))
            total = int(self.headers.get("X-Total-Size", "-1"))
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "bad headers"})
            return
        if total < 0 or offset < 0 or length < 0 or length > MAX_CHUNK:
            self._json(400, {"error": "bad ranges"})
            return
        if offset + length > total:
            self._json(400, {"error": "chunk exceeds total"})
            return

        part = self.store.part_path(full)
        final = self.store.final_path(full)
        os.makedirs(os.path.dirname(full) or self.store.root, exist_ok=True)

        with self.store.lock(rel):
            if os.path.isfile(final):
                size = os.path.getsize(final)
                if size == total:
                    log("SKIP(final) %s %d" % (rel, size))
                    self.status.skip(rel, size)
                    self._json(200, {"path": rel, "size": size, "final": True})
                else:
                    self._json(409, {"error": "final-size-mismatch", "size": size, "final": True})
                return

            current = os.path.getsize(part) if os.path.isfile(part) else 0
            if offset != current:
                self._json(409, {"error": "offset-mismatch", "size": current, "final": False})
                return
            if not self.store.free_ok(total - offset):
                self._json(507, {"error": "insufficient-storage"})
                return

            written = 0
            mode = "r+b" if current else "w+b"
            try:
                with open(part, mode) as fh:
                    fh.seek(offset)
                    while written < length:
                        chunk = self.rfile.read(min(CHUNK_COPY, length - written))
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                log("ERROR %s: %s" % (rel, exc))
                self._json(500, {"error": "write-failed"})
                return

            if written != length:
                # Roll back the short write so the next attempt is clean.
                try:
                    with open(part, "r+b") as fh:
                        fh.truncate(offset)
                except OSError:
                    pass
                log("SHORT %s %d/%d" % (rel, written, length))
                self._json(400, {"error": "short-body", "size": offset})
                return

            newsize = offset + written
            speed = self.status.part(rel, newsize, total, length)
            if newsize == total:
                os.replace(part, final)
                avg = self.status.done(rel, newsize)
                log("DONE %s %d (%.2f MB/s avg)" % (rel, newsize, avg))
                self._json(200, {"path": rel, "size": newsize, "final": True})
            else:
                # One line per 32 MiB per file keeps the log readable while a
                # 5 GB clip is in flight.
                if (offset >> 25) != (newsize >> 25):
                    pct = newsize * 100.0 / total if total else 0.0
                    log("PART %s %d/%d (%.1f%%) %.2f MB/s"
                        % (rel, newsize, total, pct, speed / (1 << 20)))
                self._json(200, {"path": rel, "size": newsize, "final": False})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def load_token(args: argparse.Namespace) -> str | None:
    if args.token:
        return args.token
    if args.token_file:
        with open(args.token_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    env = os.environ.get("SDCARD_TOKEN")
    return env.strip() if env else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8844)
    ap.add_argument("--root", required=True)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--min-free-mb", type=int, default=DEFAULT_MIN_FREE_MB)
    args = ap.parse_args()

    os.makedirs(args.root, exist_ok=True)
    Handler.store = Store(args.root, args.min_free_mb)
    Handler.token = load_token(args)
    Handler.status = Status(Handler.store.root)

    srv = Server((args.bind, args.port), Handler)
    log("listening on %s:%d root=%s auth=%s" % (
        args.bind, args.port, Handler.store.root, "on" if Handler.token else "off"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
