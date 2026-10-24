#!/usr/bin/env python3
"""Self-hosted replacement for the Insta360 camera cloud-upload service.

Implements the subset of
https://<cloud-host>/cloud-service/cloud/service/camera/... that
libInsCloudSdk.so (firmware X5, SDK 1.0.2) uses when the camera performs its
"auto cloud backup".  The bare /cloud/service/... form is accepted too.
Files are stored on local disk; uploads are resumable and multipart-compatible.

Protocol details: ../analysis/PROTOCOL.md

Usage:
    sudo ./inscloud_server.py --root /srv/insta360-backup \
        --cert ../camera/certs/server.crt --key ../camera/certs/server.key

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PART_SIZE_DEFAULT = 16 * 1024 * 1024
CONCURRENCY_DEFAULT = 3
SPEED_REPORT_INTERVAL_MS = 10_000
API_PREFIX = "/cloud/service/camera"


# --------------------------------------------------------------------------- #
# Persistent state
# --------------------------------------------------------------------------- #

class State:
    """Thread-safe, atomically persisted upload state.

    Layout:
      media[localMediaId] = {
          mediaId, created,
          items[name] = {
              itemId, size, mode, part_size, total_parts,
              parts: [bool...], completed, etag, stored_name
          }
      }
    """

    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(root, "state.json")
        self.lock = threading.RLock()
        os.makedirs(root, exist_ok=True)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (OSError, ValueError):
                # Never refuse to start because of a damaged state file.  Keep
                # the bad copy for inspection and begin a fresh upload state.
                try:
                    os.replace(self.path, self.path + ".corrupt")
                except OSError:
                    pass
                self.data = {"media": {}}
        else:
            self.data = {"media": {}}
        self.data.setdefault("media", {})

        # A crash during finalisation can leave an item with all parts stored
        # but still marked finalizing.  Let the next upload retry finalise it.
        changed = False
        for m in self.data["media"].values():
            for it in m.get("items", {}).values():
                if it.get("finalizing") and not it.get("completed"):
                    it["finalizing"] = False
                    changed = True
        if changed:
            self.save()

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    # -- helpers ----------------------------------------------------------- #

    @property
    def media(self) -> dict:
        return self.data["media"]

    def get_media(self, local_media_id: str, create: bool = True) -> dict | None:
        with self.lock:
            m = self.media.get(local_media_id)
            if m is None and create:
                media_id = "m" + hashlib.sha1(local_media_id.encode()).hexdigest()[:16]
                m = {"mediaId": media_id, "created": int(time.time()), "items": {}}
                self.media[local_media_id] = m
                self.save()
            return m

    def find_item(self, media_id: str, item_id: str):
        with self.lock:
            for lmid, m in self.media.items():
                if m.get("mediaId") != media_id:
                    continue
                for name, it in m.get("items", {}).items():
                    if it.get("itemId") == item_id:
                        return lmid, name, it
        return None, None, None

    def media_complete(self, m: dict) -> bool:
        items = m.get("items", {})
        return bool(items) and all(it.get("completed") for it in items.values())

    def used_bytes(self) -> int:
        total = 0
        for base, _dirs, files in os.walk(self.root):
            for f in files:
                if f in ("state.json", "state.json.tmp", "requests.log"):
                    continue
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
        return total


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def safe_name(name: str, fallback: str = "file") -> str:
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._()@+-]+", "_", name)
    return name or fallback


def stored_filename(name: str, suffix: str = "") -> str:
    """Build the on-disk name from the SDK's item name + suffix.

    The camera sends the basename (usually with the extension) and the
    extension separately; add the suffix only when it is missing so files
    keep a usable extension either way.
    """
    base = safe_name(name)
    suf = (suffix or "").strip().lstrip(".")
    if suf and not base.lower().endswith("." + suf.lower()):
        base += "." + suf
    return safe_name(base)


def json_bytes(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def make_parts(size: int, part_size: int) -> list[tuple[int, int]]:
    if size <= 0:
        return [(0, 0)]
    parts = []
    off = 0
    while off < size:
        length = min(part_size, size - off)
        parts.append((off, length))
        off += length
    return parts


def epoch_seconds(value: int) -> int:
    """Accept either epoch seconds or milliseconds; reject implausible values."""
    if 1_000_000_000 <= value <= 4_000_000_000:
        return value
    if 1_000_000_000_000 <= value <= 4_000_000_000_000:
        return value // 1000
    return 0


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "InsCloudLocal/1.0"
    # Drop stalled uploads / idle keep-alive connections eventually.
    timeout = 120

    # injected by make_server()
    state: State
    part_size: int
    concurrency: int
    public_base: str
    log_path: str

    # set when the request body has been read or discarded
    _body_consumed = False

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt, *args):  # noqa: D401
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _log(self, status: int, extra: dict) -> None:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": self.command,
            "path": self.path,
            "status": status,
        }
        rec.update(extra)
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _read_body(self) -> bytes:
        self._body_consumed = True
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                line = self.rfile.readline(1024).strip()
                if not line:
                    break
                n = int(line.split(b";")[0], 16)
                if n == 0:
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(n))
                self.rfile.read(2)
            return b"".join(chunks)

        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json",
              headers: dict | None = None, log_extra: dict | None = None) -> None:
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)
        self._log(code, log_extra or {})
        if (not self._body_consumed
                and (self.headers.get("Content-Length")
                     or self.headers.get("Transfer-Encoding"))):
            # Do not let an unread request body corrupt the next keep-alive
            # request on this connection.
            self.close_connection = True

    def _json(self, code: int, obj, log_extra: dict | None = None,
              headers: dict | None = None) -> None:
        self._send(code, json_bytes(obj), "application/json", headers, log_extra)

    def _maybe_100_continue(self) -> None:
        if (self.headers.get("Expect") or "").lower() == "100-continue":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()

    # -- routing ----------------------------------------------------------- #

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def _dispatch(self):
        self._body_consumed = False
        parsed = urlparse(self.path)
        path = re.sub(r"/{2,}", "/", parsed.path)
        # instaAIP configures the base URL as
        # https://service-test.insta360.com/cloud-service and the SDK appends
        # /cloud/service/... , so the real request path is
        # /cloud-service/cloud/service/camera/... .  Accept the bare form too.
        if path.startswith("/cloud-service/"):
            path = path[len("/cloud-service"):]
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if path.startswith("/upload/"):
                return self.handle_upload(path)
            if path == "/actuator/ping":
                # ping_handler requires msg to be the string "Insta360 Cloud".
                return self._json(200, {"code": 0, "msg": "Insta360 Cloud"})
            if path == API_PREFIX + "/upload/config":
                return self.handle_config()
            if path == API_PREFIX + "/upload/derivatives":
                return self.handle_derivatives()
            if path == API_PREFIX + "/upload/item/entrance":
                return self.handle_entrance(query)
            if path == API_PREFIX + "/upload/item/status/report":
                return self.handle_report()
            if path == API_PREFIX + "/upload/speed/report":
                return self.handle_speed_report()
            if path == API_PREFIX + "/userInfo/usage/detail":
                return self.handle_capacity()
            if path == API_PREFIX + "/userInfo/binding/status":
                return self.handle_binding(query)
            if path == API_PREFIX + "/media/uploaded/list":
                return self.handle_uploaded_list(query)
            if path == API_PREFIX + "/media/uploading/list":
                return self.handle_uploading_list()
            if path == API_PREFIX + "/upload/firmwareConfig":
                # QueryFirmworkConfig requires data to be a JSON object.
                return self._json(200, {"code": 0, "msg": "ok", "data": {}})
            if path == "/favicon.ico":
                return self._send(404)
            self._send(404, json_bytes({"code": 404, "msg": "not found"}),
                       log_extra={"unknown": True})
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self.close_connection = True
            self._send(500, json_bytes({"code": 500, "msg": "server error"}))

    # -- endpoints --------------------------------------------------------- #

    def handle_config(self):
        # queryUploadConfig reads data.concurrency and
        # data.reportUploadSpeedConfig.captureIntervalMs (nested, not top level).
        self._json(200, {
            "code": 0,
            "msg": "ok",
            "data": {
                "concurrency": self.concurrency,
                "reportUploadSpeedConfig": {
                    "captureIntervalMs": SPEED_REPORT_INTERVAL_MS,
                },
            },
        })

    def handle_derivatives(self):
        raw = self._read_body()
        try:
            req = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._json(400, {"code": 400, "msg": "bad json"},
                              log_extra={"body": raw[:2000].decode("utf-8", "replace")})

        local_media_id = str(req.get("localMediaId") or "")
        if not local_media_id:
            return self._json(400, {"code": 400, "msg": "missing localMediaId"})

        items_req = req.get("items") or []
        st = self.state
        with st.lock:
            media = st.get_media(local_media_id)
            shoot_time = int(req.get("shootTime") or 0)
            if shoot_time:
                media["shootTime"] = shoot_time
            media_id = media["mediaId"]

            # Only items named in this request belong to this media group.
            # Returning other media's pending items would make the SDK try to
            # upload them under the wrong mediaId.
            requested = []
            for src in items_req:
                raw_name = str(src.get("name") or "file")
                suffix = str(src.get("suffix") or "")
                size = int(src.get("size") or 0)
                if raw_name not in requested:
                    requested.append(raw_name)

                cur = media["items"].get(raw_name)
                if cur is None or int(cur.get("size", -1)) != size:
                    item_id = "i" + hashlib.sha1(
                        (media_id + "/" + raw_name).encode()).hexdigest()[:16]
                    stored = stored_filename(raw_name, suffix)
                    used = {other.get("stored_name")
                            for other_name, other in media["items"].items()
                            if other_name != raw_name}
                    if stored in used:
                        stem, ext = os.path.splitext(stored)
                        stored = "%s_%s%s" % (stem, item_id[:8], ext)
                    mode = "simple" if size <= self.part_size else "mutipart"
                    total = 1 if mode == "simple" else len(make_parts(size, self.part_size))
                    cur = {
                        "itemId": item_id,
                        "size": size,
                        "mode": mode,
                        "part_size": self.part_size,
                        "total_parts": total,
                        "parts": [False] * total,
                        "completed": False,
                        "etag": None,
                        "stored_name": stored,
                        "suffix": suffix,
                    }
                    media["items"][raw_name] = cur
                elif suffix and cur.get("suffix") != suffix:
                    cur["suffix"] = suffix
                    cur["stored_name"] = stored_filename(raw_name, suffix)
            st.save()

            total_units = 0
            complete_units = 0
            uncompleted = []
            for name in requested:
                it = media["items"][name]
                parts = make_parts(int(it["size"]), int(it["part_size"]))
                total_units += len(parts)
                done = [i for i, v in enumerate(it["parts"]) if v]
                complete_units += len(done)
                if it.get("completed"):
                    continue
                entry = {
                    "name": name,
                    "itemId": it["itemId"],
                    "mode": it["mode"],
                    "signType": "NONE",
                }
                if it["mode"] != "simple":
                    entry["uncompletedParts"] = [
                        {"partNum": i, "offset": parts[i][0], "length": parts[i][1]}
                        for i in range(len(parts)) if not it["parts"][i]
                    ]
                uncompleted.append(entry)

            data = {
                "localMediaId": local_media_id,
                "mediaId": media_id,
                "concurrency": self.concurrency,
                "totalParts": total_units,
                "completeParts": complete_units,
                "uncompletedItems": uncompleted,
            }

        self._json(200, {"code": 0, "msg": "ok", "data": data},
                   log_extra={"localMediaId": local_media_id,
                              "items": len(requested),
                              "left": len(uncompleted)})

    def handle_entrance(self, query: dict):
        media_id = query.get("mediaId") or ""
        item_id = query.get("itemId") or ""
        try:
            part_num = int(query.get("partNum") or 0)
        except ValueError:
            return self._json(400, {"code": 400, "msg": "bad partNum"})

        st = self.state
        with st.lock:
            lmid, name, it = st.find_item(media_id, item_id)
            if it is None:
                return self._json(404, {"code": 404, "msg": "unknown item"})
            parts = make_parts(int(it["size"]), int(it["part_size"]))
            if part_num < 0 or part_num >= len(parts):
                return self._json(400, {"code": 400, "msg": "bad partNum"})

            # Deliberately allow re-fetching a part that is already stored: a
            # dropped response makes the camera retry, and 2xx is what it needs.
            url = "%s/upload/%s/%s/%d" % (self.public_base, media_id, item_id, part_num)
            data = {
                "localMediaId": lmid,
                "mediaId": media_id,
                "path": {
                    "url": url,
                    "method": "PUT",
                    "headers": [
                        {"name": "Content-Type", "value": "application/octet-stream"},
                    ],
                },
                "partNum": part_num,
            }
        self._json(200, {"code": 0, "msg": "ok", "data": data},
                   log_extra={"mediaId": media_id, "itemId": item_id,
                              "partNum": part_num, "url": url,
                              "already": bool(it["parts"][part_num])})

    def handle_upload(self, path: str):
        # /upload/<mediaId>/<itemId>/<partNum>
        m = re.match(r"^/upload/([^/]+)/([^/]+)/(\d+)$", path)
        if not m:
            return self._json(400, {"code": 400, "msg": "bad upload path"})
        media_id, item_id, part_num = m.group(1), m.group(2), int(m.group(3))

        st = self.state
        with st.lock:
            lmid, name, it = st.find_item(media_id, item_id)
            if it is None:
                self.close_connection = True
                return self._json(404, {"code": 404, "msg": "unknown item"})
            parts = make_parts(int(it["size"]), int(it["part_size"]))
            if part_num >= len(parts):
                self.close_connection = True
                return self._json(400, {"code": 400, "msg": "bad partNum"})
            offset, length = parts[part_num]
            completed = bool(it.get("completed"))
            finalizing = bool(it.get("finalizing"))

        self._maybe_100_continue()
        cl = self.headers.get("Content-Length")
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        if cl is None and not chunked and length != 0:
            self.close_connection = True
            return self._json(411, {"code": 411, "msg": "length required"})
        if cl is not None:
            try:
                cl = int(cl)
            except ValueError:
                cl = -1
            if cl != length:
                self.close_connection = True
                return self._json(400, {
                    "code": 400,
                    "msg": "length mismatch (got %s expected %d)" % (cl, length),
                })

        if completed or finalizing:
            # A retry raced with finalisation: consume the body and confirm.
            self._discard_body(length)
            etag = it.get("etag")
            headers = {"ETag": '"%s"' % etag} if etag else {}
            return self._send(200, b"", "application/octet-stream", headers=headers,
                              log_extra={"mediaId": media_id, "itemId": item_id,
                                         "partNum": part_num, "bytes": length,
                                         "already_complete": True})

        item_dir = os.path.join(st.root, ".parts", media_id, item_id)
        os.makedirs(item_dir, exist_ok=True)
        data_path = os.path.join(item_dir, "data")

        # Write straight to the part's final offset in a sparse file.  This
        # avoids buffering whole parts in RAM and avoids a second full copy
        # when the item completes.
        md5 = hashlib.md5()
        fd = os.open(data_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.fstat(fd).st_size < int(it["size"]):
                os.ftruncate(fd, int(it["size"]))
            try:
                written = self._stream_part_to_fd(fd, offset, length, md5)
            finally:
                os.fsync(fd)
        finally:
            os.close(fd)

        if written != length:
            self.close_connection = True
            return self._json(400, {
                "code": 400,
                "msg": "short body (got %d expected %d)" % (written, length),
            })

        etag = md5.hexdigest()   # per-part ETag, reported by the camera
        need_finalize = False
        with st.lock:
            it["parts"][part_num] = True
            if (not it.get("completed") and not it.get("finalizing")
                    and all(it["parts"])):
                it["finalizing"] = True
                need_finalize = True
            st.save()

        if need_finalize:
            try:
                etag = self._finalize(lmid, media_id, item_id, name, it)
            except Exception:
                traceback.print_exc()
                with st.lock:
                    it["finalizing"] = False
                    st.save()
                self.close_connection = True
                return self._json(500, {"code": 500, "msg": "finalize failed"})

        headers = {"ETag": '"%s"' % etag} if etag else {}
        self._send(200, b"", "application/octet-stream", headers=headers,
                   log_extra={"mediaId": media_id, "itemId": item_id,
                              "partNum": part_num, "bytes": written,
                              "etag": etag, "complete": bool(it.get("completed"))})

    def _discard_body(self, length: int) -> None:
        """Consume an idempotent retry without storing it."""
        self._body_consumed = True
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            while True:
                line = self.rfile.readline(1024).strip()
                if not line:
                    return
                n = int(line.split(b";")[0], 16)
                if n == 0:
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    return
                left = n
                while left:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        return
                    left -= len(chunk)
                self.rfile.read(2)
            return
        left = length
        while left:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                return
            left -= len(chunk)

    def _stream_part_to_fd(self, fd: int, offset: int, length: int, md5) -> int:
        """Copy one part from the request body into fd at its final offset."""
        self._body_consumed = True
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        written = 0
        if "chunked" in te:
            while True:
                line = self.rfile.readline(1024).strip()
                if not line:
                    raise ValueError("empty chunk header")
                n = int(line.split(b";")[0], 16)
                if n == 0:
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                left = n
                while left:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        raise ValueError("short chunk")
                    self._pwrite_all(fd, chunk, offset + written)
                    md5.update(chunk)
                    written += len(chunk)
                    left -= len(chunk)
                if written > length:
                    raise ValueError("part larger than announced")
                if self.rfile.read(2) != b"\r\n":
                    raise ValueError("bad chunk terminator")
            return written

        left = length
        while left:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                break
            self._pwrite_all(fd, chunk, offset + written)
            md5.update(chunk)
            written += len(chunk)
            left -= len(chunk)
        return written

    @staticmethod
    def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
        if hasattr(os, "pwrite"):
            done = 0
            while done < len(data):
                done += os.pwrite(fd, data[done:], offset + done)
            return
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, data)

    def _finalize(self, local_media_id: str, media_id: str, item_id: str,
                  name: str, it: dict) -> str:
        """Move the completed sparse file into the backup tree immediately.

        The whole-file checksum is written asynchronously as a `.md5` sidecar,
        so a multi-GB file never delays the final upload response past the
        camera's HTTP timeout.
        """
        st = self.state
        item_dir = os.path.join(st.root, ".parts", media_id, item_id)
        data_path = os.path.join(item_dir, "data")
        out_dir = os.path.join(st.root, safe_name(local_media_id))
        out_name = it.get("stored_name") or stored_filename(name, it.get("suffix", ""))
        out_path = os.path.join(out_dir, out_name)

        os.makedirs(out_dir, exist_ok=True)
        if os.path.exists(data_path):
            os.replace(data_path, out_path)
        elif not os.path.exists(out_path):
            raise IOError("completed part data is missing: %s" % data_path)

        if os.path.getsize(out_path) != int(it["size"]):
            raise IOError("assembled size mismatch: %s" % out_path)

        try:
            os.rmdir(item_dir)
        except OSError:
            pass

        # Preserve the camera's capture time when it looks like a timestamp.
        with st.lock:
            media = st.media.get(local_media_id) or {}
            mtime = epoch_seconds(int(media.get("shootTime") or 0))
        if mtime:
            try:
                os.utime(out_path, (mtime, mtime))
            except OSError:
                pass

        # Opaque-but-stable object token; the camera only forwards it back.
        etag = hashlib.md5(
            ("inscloud:%s:%s:%s" % (media_id, item_id, it["size"])).encode()
        ).hexdigest()
        with st.lock:
            it["completed"] = True
            it["completed_at"] = int(time.time())
            it["etag"] = etag
            it["finalizing"] = False
            st.save()

        threading.Thread(target=self._write_md5_sidecar, args=(out_path,),
                         daemon=True).start()
        return etag

    @staticmethod
    def _write_md5_sidecar(path: str) -> None:
        """Write a `md5sum`-compatible sidecar without blocking uploads."""
        tmp = path + ".md5.tmp"
        try:
            md5 = hashlib.md5()
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    md5.update(chunk)
            with open(tmp, "w", encoding="ascii") as out:
                out.write("%s  %s\n" % (md5.hexdigest(), os.path.basename(path)))
            os.replace(tmp, path + ".md5")
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def handle_report(self):
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        self._json(200, {"code": 0, "msg": "ok"},
                   log_extra={"report": body})

    def handle_speed_report(self):
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        self._json(200, {"code": 0, "msg": "ok"}, log_extra={"speed": body})

    def handle_capacity(self):
        used = self.state.used_bytes()
        try:
            free = shutil.disk_usage(self.state.root).free
        except OSError:
            free = 0
        # Report the space this backup may still consume, so the camera stops
        # before the host disk fills up.  Values are gigabytes (doubles).
        total = used + free
        self._json(200, {
            "code": 0,
            "msg": "ok",
            "data": {"total": float(total) / 1e9, "usage": float(used) / 1e9},
        })

    def handle_binding(self, query: dict):
        self._json(200, {"code": 0, "msg": "ok", "data": 1},
                   log_extra={"deviceId": query.get("deviceId")})

    def handle_uploaded_list(self, query: dict):
        page = int(query.get("pageNumber") or 1)
        size = int(query.get("pageSize") or 20)
        st = self.state
        with st.lock:
            done = []
            for lmid, m in st.media.items():
                if not st.media_complete(m):
                    continue
                finish = 0
                for it in m["items"].values():
                    finish = max(finish, int(it.get("completed_at") or m["created"]))
                done.append({
                    "localMediaId": lmid,
                    "mediaId": m["mediaId"],
                    # UploadedServerRecord reads this as int64 (milliseconds).
                    "uploadFinishTime": int(finish) * 1000,
                })
        done.sort(key=lambda rec: rec["uploadFinishTime"])
        start = max(0, (page - 1) * size)
        page_items = done[start:start + size]
        total_page = max(1, (len(done) + size - 1) // size)
        self._json(200, {
            "code": 0,
            "msg": "ok",
            "data": {
                "pageNumber": min(page, total_page),
                "totalPage": total_page,
                "list": page_items,
            },
        })

    def handle_uploading_list(self):
        st = self.state
        with st.lock:
            pending = [lmid for lmid, m in st.media.items()
                       if not st.media_complete(m)]
        self._json(200, {"code": 0, "msg": "ok", "data": pending})


# --------------------------------------------------------------------------- #
# Server bootstrap
# --------------------------------------------------------------------------- #

class QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not dump client resets as tracebacks."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_server(args) -> ThreadingHTTPServer:
    state = State(os.path.abspath(args.root))
    Handler.state = state
    Handler.part_size = args.part_size
    Handler.concurrency = args.concurrency
    Handler.public_base = args.public_base.rstrip("/")
    Handler.log_path = os.path.join(state.root, "requests.log")

    httpd = QuietServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True

    if args.cert and args.key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"

    print("InsCloud local server listening on %s://%s:%d"
          % (scheme, args.bind, args.port))
    print("  storage     : %s" % state.root)
    print("  public base : %s" % Handler.public_base)
    print("  part size   : %d bytes" % args.part_size)
    print("  concurrency : %d" % args.concurrency)
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="./backup", help="storage directory")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--cert", default="", help="TLS certificate (PEM)")
    ap.add_argument("--key", default="", help="TLS private key (PEM)")
    ap.add_argument("--generate-certs", metavar="DIR", default="",
                    help="generate a local CA and server certificate in DIR "
                         "when --cert/--key are not supplied")
    ap.add_argument("--generate-certs-only", action="store_true",
                    help="generate the certificates and exit")
    ap.add_argument("--public-base", default="https://service-test.insta360.com",
                    help="base URL handed to the camera for uploads")
    ap.add_argument("--part-size", type=int, default=PART_SIZE_DEFAULT)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY_DEFAULT)
    args = ap.parse_args()

    if args.cert and not args.key or args.key and not args.cert:
        ap.error("--cert and --key must be given together")

    if args.generate_certs and not args.cert and not args.key:
        certdir = os.path.abspath(args.generate_certs)
        script = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "camera", "make_certs.sh"))
        if not os.path.isfile(script):
            ap.error("--generate-certs needs camera/make_certs.sh "
                     "next to server/")
        crt = os.path.join(certdir, "server.crt")
        key = os.path.join(certdir, "server.key")
        if not (os.path.isfile(crt) and os.path.isfile(key)):
            os.makedirs(certdir, exist_ok=True)
            try:
                subprocess.run(["sh", script, certdir], check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                ap.error("certificate generation failed: %s" % exc)
        args.cert, args.key = crt, key
        print("using generated certificate: %s" % crt)
        print("copy %s and %s to the SD card root"
              % (os.path.join(certdir, "ca.crt"),
                 os.path.join(certdir, "ca.hash")))

    if args.generate_certs_only:
        if not args.generate_certs:
            ap.error("--generate-certs-only requires --generate-certs DIR")
        return 0

    httpd = make_server(args)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
