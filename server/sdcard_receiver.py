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
  GET  /api/status
       -> live totals, active uploads and resumable partials
  GET  /api/files
       -> every completed file with size and mtime
  GET  /
       -> the live dashboard (status routes are read-only and unauthenticated)

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
DEFAULT_MIN_FREE_MB = 1024
# A chunk must have been seen this recently to count as an in-flight job.
ACTIVE_GRACE = float(os.environ.get("SDCARD_ACTIVE_GRACE", "15"))


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
        # path -> (bytes, mtime)
        self.completed: dict[str, tuple[int, float]] = {}
        self.partials: dict[str, int] = {}
        self.last_activity = 0.0
        self._scan()

    def _scan(self) -> None:
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if name.endswith(".part"):
                    # The uploader addresses the target path, not the .part
                    # filename; keep both views consistent across restarts.
                    self.partials[rel[:-5]] = st.st_size
                else:
                    self.completed[rel] = (st.st_size, st.st_mtime)
        # A .part left beside an already-complete file is not resumable.
        self.partials = {path: size for path, size in self.partials.items()
                         if path not in self.completed}
        # After a restart there is no request history; the newest file mtime is
        # the most honest "last activity" we can show.
        mtimes = [mtime for _size, mtime in self.completed.values()]
        if mtimes:
            self.last_activity = max(mtimes)

    def _mtime(self, rel: str) -> float:
        try:
            return os.path.getmtime(os.path.join(self.root, rel))
        except OSError:
            return time.time()

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
            a["last"] = now
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
            self.completed[rel] = (size, self._mtime(rel))
            if a:
                return size / max(0.001, now - a["started"]) / (1 << 20)
            return 0.0

    def skip(self, rel: str, size: int) -> None:
        self.last_activity = time.time()
        with self.lock:
            self.active.pop(rel, None)
            self.partials.pop(rel, None)
            self.completed[rel] = (size, self._mtime(rel))

    def reset(self, rel: str) -> None:
        self.last_activity = time.time()
        with self.lock:
            self.active.pop(rel, None)
            self.partials.pop(rel, None)

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            # A dropped connection leaves its last chunk in `active` forever.
            # Only a chunk seen recently counts as an in-flight job; older ones
            # are just resumable partials.
            stale = [rel for rel, a in self.active.items()
                     if now - a.get("last", 0) > ACTIVE_GRACE]
            for rel in stale:
                self.active.pop(rel, None)
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
                "bytes_done": sum(v[0] for v in self.completed.values()),
                "bytes_partial": sum(self.partials.values()),
                "active": sorted(active, key=lambda x: x["path"]),
                "partials": [{"path": k, "bytes": v}
                             for k, v in sorted(self.partials.items())],
                "disk_free": shutil.disk_usage(self.root).free,
            }

    def file_list(self) -> dict:
        """Completed files, for the dashboard's file ledger."""
        with self.lock:
            rows = [{"path": path, "size": size, "mtime": mtime}
                    for path, (size, mtime) in self.completed.items()]
        rows.sort(key=lambda row: row["path"].lower())
        return {"files": rows, "count": len(rows),
                "total_bytes": sum(row["size"] for row in rows)}


def _json_for_script(value) -> str:
    """JSON that is safe inside a <script type=application/json> block."""
    return (json.dumps(value, separators=(",", ":"))
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


DASHBOARD_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insta360 backup</title>
<style>
:root{
  color-scheme:dark;
  --bg:#151412; --panel:#1b1916; --panel-2:#221f1a;
  --line:#332d25; --line-soft:#27231d;
  --ink:#ece5d8; --muted:#9b9183; --dim:#6f675c;
  --amber:#e0a45c; --sage:#9bb39a; --red:#cf7364;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:13px/1.5 var(--mono);-webkit-font-smoothing:antialiased}
.filmstrip{height:14px;background:var(--panel);border-bottom:1px solid var(--line);position:relative}
.filmstrip:before{content:"";position:absolute;inset:4px 0;background-image:repeating-linear-gradient(90deg,var(--bg) 0 7px,transparent 7px 17px)}
.shell{max-width:1120px;margin:0 auto;padding:28px 24px 44px}
.masthead{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;border-bottom:1px solid var(--line);padding-bottom:18px}
h1{font:400 38px/1.04 var(--serif);letter-spacing:-.015em;margin:0}
.sub{color:var(--muted);font-size:11px;margin:7px 0 0}
.live{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:11px;white-space:nowrap}
.dot{width:6px;height:6px;border-radius:50%;background:var(--dim);flex:none}
.dot.ok{background:var(--sage)}
.dot.busy{background:var(--amber);animation:pulse 1.8s ease-out infinite}
.dot.bad{background:var(--red)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(224,164,92,.55)}70%{box-shadow:0 0 0 7px rgba(224,164,92,0)}100%{box-shadow:0 0 0 0 rgba(224,164,92,0)}}
button{background:transparent;border:1px solid var(--line);color:var(--ink);font:inherit;font-size:10px;padding:5px 8px;border-radius:2px;cursor:pointer}
button:hover{border-color:var(--muted)}
:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.ledger{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--line);padding:20px 0}
.figure{padding:2px 20px;border-left:1px solid var(--line-soft)}
.figure:first-child{border-left:0;padding-left:0}
.figure b{display:block;font-weight:400;font-size:21px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.figure span{display:block;color:var(--muted);font-size:10px;margin-top:3px}
.section-head{display:flex;align-items:baseline;gap:12px;margin:30px 0 9px}
h2{font:400 21px/1.1 var(--serif);margin:0}
h3{font:400 12px/1.2 var(--serif);color:var(--muted);margin:20px 0 5px}
.count{color:var(--dim);font-size:10px}
#filter{margin-left:auto;width:210px;background:var(--panel);border:1px solid var(--line);color:var(--ink);font:inherit;font-size:10px;padding:6px 8px;border-radius:2px}
#filter::placeholder{color:var(--dim)}
.job{border-top:1px solid var(--line-soft);padding:13px 0 14px}
.job-top{display:flex;justify-content:space-between;gap:18px;align-items:baseline}
.job-path{overflow-wrap:anywhere;font-size:12px}
.job-pct{color:var(--amber);font-variant-numeric:tabular-nums;white-space:nowrap}
.bar{height:6px;background:var(--panel-2);margin:9px 0 7px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--amber);width:0;transition:width .35s ease}
.job-meta{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:10px;font-variant-numeric:tabular-nums}
.partial-row{display:flex;justify-content:space-between;gap:16px;border-top:1px dotted var(--line-soft);padding:6px 0;color:var(--muted);font-size:11px}
.partial-row span:first-child{color:var(--ink);overflow-wrap:anywhere}
.table-wrap{border-top:1px solid var(--line);overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;z-index:1;background:var(--bg);text-align:left;font-weight:400;color:var(--muted);font-size:10px;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--line-soft);vertical-align:top}
.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.when{color:var(--muted);white-space:nowrap;font-size:11px}
.path{max-width:620px;overflow-wrap:anywhere;font-size:11px}
.state{display:inline-block;border:1px solid var(--line);padding:1px 6px;border-radius:2px;font-size:9px;color:var(--muted);white-space:nowrap}
.state.complete{color:var(--sage);border-color:rgba(155,179,154,.35)}
.state.partial{color:var(--amber);border-color:rgba(224,164,92,.35)}
.state.uploading{color:var(--amber);background:rgba(224,164,92,.12);border-color:rgba(224,164,92,.35)}
.empty{color:var(--muted);padding:16px 10px;font:italic 13px/1.5 var(--serif);margin:0}
footer{display:flex;justify-content:space-between;gap:16px;border-top:1px solid var(--line);margin-top:26px;padding-top:9px;color:var(--dim);font-size:10px}
.noscript{position:fixed;left:0;right:0;bottom:0;margin:0;padding:10px 16px;background:var(--panel);border-top:1px solid var(--line);color:var(--muted)}
@media (max-width:720px){
  .shell{padding:20px 14px 34px}
  h1{font-size:30px}
  .masthead{flex-direction:column;align-items:flex-start;gap:12px}
  .ledger{grid-template-columns:1fr 1fr}
  .figure{padding:10px 12px;border-left:0;border-top:1px solid var(--line-soft)}
  .figure:nth-child(-n+2){border-top:0}
  .section-head{flex-wrap:wrap}
  #filter{margin-left:0;width:100%}
  .when{display:none}
}
@media (prefers-reduced-motion:reduce){
  .dot.busy{animation:none}
  .bar i{transition:none}
}
</style>
</head>
<body>
<div class="filmstrip" aria-hidden="true"></div>
<div class="shell">
<header class="masthead">
  <div>
    <h1>Insta360 backup</h1>
    <p class="sub">SD-card receiver for Insta360 X4 Air</p>
  </div>
  <div class="live" aria-live="polite">
    <span class="dot" id="dot" aria-hidden="true"></span>
    <span id="liveText">connecting</span>
    <button id="refresh" type="button">Refresh</button>
  </div>
</header>

<section class="ledger" aria-label="Summary">
  <div class="figure"><b id="filesDone">—</b><span>files stored</span></div>
  <div class="figure"><b id="bytesDone">—</b><span>on the server</span></div>
  <div class="figure"><b id="diskFree">—</b><span>free space</span></div>
  <div class="figure"><b id="lastActivity">—</b><span>last activity</span></div>
</section>

<section aria-labelledby="incomingTitle">
  <div class="section-head"><h2 id="incomingTitle">Incoming</h2><span class="count" id="incomingCount"></span></div>
  <div id="jobs"></div>
  <div id="partialWrap" hidden>
    <h3>Waiting to resume</h3>
    <div id="partials"></div>
  </div>
</section>

<section aria-labelledby="filesTitle">
  <div class="section-head">
    <h2 id="filesTitle">Files</h2><span class="count" id="fileCount"></span>
    <input id="filter" type="search" placeholder="Filter by path" autocomplete="off" aria-label="Filter files">
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Path</th><th class="num">Size</th><th class="when">Modified</th><th>State</th></tr></thead>
      <tbody id="fileBody"></tbody>
    </table>
    <p id="empty" class="empty" hidden></p>
  </div>
</section>

<footer><span id="uptime"></span><span id="updated"></span></footer>
</div>
<noscript><p class="noscript">This page needs JavaScript for live updates. Raw data: <a href="/api/status">/api/status</a> and <a href="/api/files">/api/files</a>.</p></noscript>
<script id="bootstrap" type="application/json">__BOOTSTRAP__</script>
<script>
(function () {
  "use strict";
  var boot = JSON.parse(document.getElementById("bootstrap").textContent);
  var status = boot.status || {};
  var files = boot.files || {files: [], count: 0, total_bytes: 0};
  var lastFilesDone = status.files_done == null ? -1 : status.files_done;
  var lastFilesAt = 0;
  var lastRowsKey = "";
  var lastLive = "";
  var filter = "";
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function span(text) { var s = document.createElement("span"); s.textContent = text; return s; }
  function fmtBytes(n) {
    if (n == null || isNaN(n)) return "—";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    var d = i === 0 ? 0 : v >= 100 ? 0 : v >= 10 ? 1 : 2;
    return v.toFixed(d) + " " + u[i];
  }
  function fmtSpeed(mbps) { return mbps > 0 ? Number(mbps).toFixed(2) + " MB/s" : "—"; }
  function fmtEta(sec) {
    if (sec == null) return "—";
    if (sec < 60) return sec + "s";
    return Math.floor(sec / 60) + "m " + String(sec % 60).padStart(2, "0") + "s";
  }
  function fmtAgo(sec) {
    if (sec == null) return "never";
    if (sec < 60) return Math.max(0, Math.floor(sec)) + "s ago";
    if (sec < 3600) return Math.floor(sec / 60) + "m ago";
    if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
    return Math.floor(sec / 86400) + "d ago";
  }
  function fmtDuration(sec) {
    if (sec == null) return "—";
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    if (m) return m + "m";
    return Math.floor(sec) + "s";
  }
  function fmtWhen(mtime) {
    if (!mtime) return {text: "—", title: ""};
    var d = new Date(mtime * 1000);
    return {text: fmtAgo((Date.now() - d.getTime()) / 1000), title: d.toLocaleString()};
  }
  function activeSet() {
    var set = {};
    (status.active || []).forEach(function (a) { set[a.path] = true; });
    return set;
  }
  function setLive(text, cls) {
    if (text !== lastLive) { $("liveText").textContent = text; lastLive = text; }
    $("dot").className = "dot" + (cls ? " " + cls : "");
  }
  function renderSummary() {
    $("filesDone").textContent = status.files_done == null ? "—" : status.files_done;
    $("bytesDone").textContent = fmtBytes(status.bytes_done);
    $("diskFree").textContent = fmtBytes(status.disk_free);
    $("lastActivity").textContent = status.last_activity_s_ago == null ? "never" : fmtAgo(status.last_activity_s_ago);
    $("uptime").textContent = "receiver up " + fmtDuration(status.uptime_s);
  }
  function renderJobs() {
    var wrap = $("jobs");
    wrap.replaceChildren();
    var active = status.active || [];
    $("incomingCount").textContent = active.length ? active.length + (active.length === 1 ? " upload" : " uploads") : "";
    if (!active.length) {
      var p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No upload in progress. The camera uploads on its next pass.";
      wrap.appendChild(p);
      return;
    }
    active.forEach(function (a) {
      var card = document.createElement("article");
      card.className = "job";
      var top = document.createElement("div");
      top.className = "job-top";
      var path = document.createElement("span");
      path.className = "job-path";
      path.textContent = a.path;
      path.title = a.path;
      var pct = document.createElement("span");
      pct.className = "job-pct";
      pct.textContent = Number(a.percent || 0).toFixed(1) + "%";
      top.appendChild(path);
      top.appendChild(pct);
      var bar = document.createElement("div");
      bar.className = "bar";
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-label", "Upload progress for " + a.path);
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(Number(a.percent || 0).toFixed(1)));
      var fill = document.createElement("i");
      fill.style.width = Math.max(0, Math.min(100, Number(a.percent || 0))) + "%";
      bar.appendChild(fill);
      var meta = document.createElement("div");
      meta.className = "job-meta";
      meta.appendChild(span(fmtBytes(a.offset) + " of " + fmtBytes(a.total)));
      meta.appendChild(span(fmtSpeed(a.mbps)));
      meta.appendChild(span(a.eta_s == null ? "ETA —" : "ETA " + fmtEta(a.eta_s)));
      card.appendChild(top);
      card.appendChild(bar);
      card.appendChild(meta);
      wrap.appendChild(card);
    });
  }
  function renderPartials() {
    var active = activeSet();
    var waiting = (status.partials || []).filter(function (p) { return !active[p.path]; });
    var wrap = $("partials");
    wrap.replaceChildren();
    $("partialWrap").hidden = waiting.length === 0;
    waiting.forEach(function (p) {
      var row = document.createElement("div");
      row.className = "partial-row";
      row.appendChild(span(p.path));
      row.appendChild(span(fmtBytes(p.bytes) + " stored"));
      wrap.appendChild(row);
    });
  }
  function buildRows() {
    var map = new Map();
    (files.files || []).forEach(function (f) {
      map.set(f.path, {path: f.path, size: f.size, mtime: f.mtime, state: "complete"});
    });
    (status.partials || []).forEach(function (p) {
      if (!map.has(p.path)) map.set(p.path, {path: p.path, size: p.bytes, mtime: null, state: "partial"});
    });
    (status.active || []).forEach(function (a) {
      var row = map.get(a.path);
      if (row) {
        row.state = "uploading";
        row.total = a.total;
      } else {
        map.set(a.path, {path: a.path, size: a.offset, mtime: null, state: "uploading", total: a.total});
      }
    });
    return Array.from(map.values()).sort(function (x, y) {
      return x.path.localeCompare(y.path, undefined, {numeric: true});
    });
  }
  function renderFiles(force) {
    var all = buildRows();
    var shown = filter ? all.filter(function (r) { return r.path.toLowerCase().indexOf(filter) !== -1; }) : all;
    var key = filter + "|" + shown.map(function (r) { return r.path + ":" + r.size + ":" + r.state; }).join("|");
    $("fileCount").textContent = filter ? shown.length + " of " + all.length : all.length + (all.length === 1 ? " file" : " files");
    if (!force && key === lastRowsKey) return;
    lastRowsKey = key;
    var body = $("fileBody");
    body.replaceChildren();
    var frag = document.createDocumentFragment();
    shown.forEach(function (r) {
      var tr = document.createElement("tr");
      var tdPath = document.createElement("td");
      tdPath.className = "path";
      tdPath.textContent = r.path;
      tdPath.title = r.path;
      var tdSize = document.createElement("td");
      tdSize.className = "num";
      tdSize.textContent = fmtBytes(r.size);
      if (r.state === "uploading" && r.total) tdSize.title = "of " + fmtBytes(r.total);
      var tdWhen = document.createElement("td");
      tdWhen.className = "when";
      var when = fmtWhen(r.mtime);
      tdWhen.textContent = when.text;
      if (when.title) tdWhen.title = when.title;
      var tdState = document.createElement("td");
      var pill = document.createElement("span");
      pill.className = "state " + r.state;
      pill.textContent = r.state === "complete" ? "stored" : r.state;
      tdState.appendChild(pill);
      tr.appendChild(tdPath);
      tr.appendChild(tdSize);
      tr.appendChild(tdWhen);
      tr.appendChild(tdState);
      frag.appendChild(tr);
    });
    body.appendChild(frag);
    var empty = $("empty");
    empty.hidden = shown.length !== 0;
    if (!shown.length) {
      empty.textContent = all.length ? "No files match that filter." : "No files yet. Power on the camera and enable the hotspot.";
    }
  }
  function renderAll() {
    renderSummary();
    renderJobs();
    renderPartials();
    renderFiles(false);
  }
  async function tick(force) {
    if (busy || document.hidden) return;
    busy = true;
    try {
      var res = await fetch("/api/status", {cache: "no-store"});
      if (!res.ok) throw new Error("status " + res.status);
      status = await res.json();
      if (force || status.files_done !== lastFilesDone || Date.now() - lastFilesAt > 20000) {
        var fr = await fetch("/api/files", {cache: "no-store"});
        if (fr.ok) {
          files = await fr.json();
          lastFilesDone = status.files_done;
          lastFilesAt = Date.now();
        }
      }
      renderAll();
      $("updated").textContent = "checked " + new Date().toLocaleTimeString();
      var active = status.active || [];
      var act = activeSet();
      var waiting = (status.partials || []).filter(function (p) { return !act[p.path]; }).length;
      if (active.length) setLive(active.length + " uploading", "busy");
      else setLive(waiting ? "idle, " + waiting + " waiting" : "idle", "ok");
    } catch (err) {
      setLive("receiver offline", "bad");
    } finally {
      busy = false;
    }
  }
  $("refresh").addEventListener("click", function () { tick(true); });
  $("filter").addEventListener("input", function (e) {
    filter = e.target.value.trim().toLowerCase();
    renderFiles(true);
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick(true);
  });
  renderAll();
  tick(true);
  setInterval(function () { tick(false); }, 2000);
})();
</script>
</body>
</html>
"""


def dashboard_html(status: dict, files: dict) -> str:
    payload = _json_for_script({"status": status, "files": files})
    return DASHBOARD_TEMPLATE.replace("__BOOTSTRAP__", payload)


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
            self._html(dashboard_html(self.status.snapshot(),
                                      self.status.file_list()))
            return
        if path == "/api/status":
            self._json(200, self.status.snapshot())
            return
        if path == "/api/files":
            self._json(200, self.status.file_list())
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
