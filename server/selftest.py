#!/usr/bin/env python3
"""End-to-end self test for inscloud_server.py (no camera needed).

Simulates the request sequence libInsCloudSdk.so performs:
config -> derivatives -> entrance (per part) -> PUT -> status report,
then verifies bytes on disk, retries, simple-mode items, cross-media
isolation, and the exact JSON types the SDK parsers require.

The type checks below are derived from the decompiled parsers:
  QueryCapacity        data.total / data.usage must be JSON doubles
  queryUploadConfig    data.concurrency, data.reportUploadSpeedConfig.captureIntervalMs
  queryUploadRecord...  data.list[].uploadFinishTime must be int64
  QueryFirmworkConfig  data must be a JSON object
"""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inscloud_server as srv  # noqa: E402

# instaAIP's base URL is https://service-test.insta360.com/cloud-service and
# the SDK appends /cloud/service/... .
API = "/cloud-service"


def call(conn, method, path, body=None, headers=None):
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(data))
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        parsed = payload
    return resp, parsed


def upload_part(url, payload, extra_headers=None):
    hostport, path = url.split("://", 1)[1].split("/", 1)
    headers = {"Content-Length": str(len(payload))}
    headers.update(extra_headers or {})
    conn = http.client.HTTPConnection(hostport, timeout=10)
    try:
        conn.request("PUT", "/" + path, body=payload, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status, dict(resp.getheaders())
    finally:
        conn.close()


def upload_part_chunked(url, payload):
    """Same as upload_part but with Transfer-Encoding: chunked."""
    hostport, path = url.split("://", 1)[1].split("/", 1)
    half = max(1, len(payload) // 2)
    chunks = iter([payload[:half], payload[half:]])
    conn = http.client.HTTPConnection(hostport, timeout=10)
    try:
        conn.request("PUT", "/" + path, body=chunks,
                     headers={"Transfer-Encoding": "chunked"},
                     encode_chunked=True)
        resp = conn.getresponse()
        resp.read()
        return resp.status, dict(resp.getheaders())
    finally:
        conn.close()


def entrance(conn, media_id, item_id, part_num=None):
    path = (API + "/cloud/service/camera/upload/item/entrance"
            "?mediaId=%s&itemId=%s" % (media_id, item_id))
    if part_num is not None:
        path += "&partNum=%d" % part_num
    r, body = call(conn, "GET", path)
    assert r.status == 200 and body["code"] == 0, body
    return body["data"]


def derivatives(conn, local_media_id, items):
    body = {
        "localMediaId": local_media_id,
        "mediaType": "video",
        "shootTime": 1700000000,
        "mediaName": local_media_id,
        "items": items,
    }
    r, parsed = call(conn, "POST",
                     API + "/cloud/service/camera/upload/derivatives", body)
    assert r.status == 200 and parsed["code"] == 0, parsed
    return parsed["data"], body


def main() -> int:
    root = tempfile.mkdtemp(prefix="inscloud-test-")
    video = bytes((i * 37) & 0xFF for i in range(3000))
    photo = bytes((i * 11) & 0xFF for i in range(500))
    other = bytes((i * 5) & 0xFF for i in range(1200))

    args = type("A", (), {
        "root": root,
        "bind": "127.0.0.1",
        "port": 0,
        "cert": "",
        "key": "",
        "public_base": "",
        "part_size": 1024,
        "concurrency": 3,
    })()

    httpd = srv.make_server(args)
    port = httpd.server_address[1]
    args.public_base = "http://127.0.0.1:%d" % port
    srv.Handler.public_base = args.public_base
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        # --- config ---------------------------------------------------------
        r, cfg = call(conn, "GET", API + "/cloud/service/camera/upload/config")
        assert r.status == 200 and cfg["code"] == 0, cfg
        assert isinstance(cfg["data"]["concurrency"], int), cfg
        assert isinstance(
            cfg["data"]["reportUploadSpeedConfig"]["captureIntervalMs"], int), cfg
        print("config ok:", cfg["data"])

        # the bare form (no /cloud-service gateway prefix) must also work
        r, cfg2 = call(conn, "GET", "/cloud/service/camera/upload/config")
        assert r.status == 200 and cfg2["code"] == 0, cfg2
        # a runtime base URL with a trailing slash produces a doubled path
        r, cfg3 = call(conn, "GET",
                       "/cloud-service//cloud/service/camera/upload/config")
        assert r.status == 200 and cfg3["code"] == 0, cfg3
        print("bare-path and doubled-slash config ok")

        # --- firmware config: data must be an object ------------------------
        r, fw = call(conn, "GET",
                     API + "/cloud/service/camera/upload/firmwareConfig")
        assert r.status == 200 and isinstance(fw["data"], dict), fw
        print("firmwareConfig ok:", fw["data"])

        # --- ping: ping_handler requires msg == "Insta360 Cloud" -----------
        r, ping = call(conn, "GET", "/actuator/ping")
        assert r.status == 200 and ping.get("msg") == "Insta360 Cloud", ping
        print("ping ok:", ping)

        # --- derivatives: multipart + simple in one media group --------------
        d1, body1 = derivatives(conn, "LMID_TEST_1", [
            {"name": "VID_20240101_000001_00_001.mp4",
             "suffix": "mp4", "size": len(video)},
            # no extension in `name`: server must append the suffix
            {"name": "PHOTO_20240101_000001_00_001",
             "suffix": "insp", "size": len(photo)},
        ])
        assert d1["mediaId"], d1
        assert d1["totalParts"] == 3 + 1 and d1["completeParts"] == 0, d1
        by_name = {it["name"]: it for it in d1["uncompletedItems"]}
        assert set(by_name) == {
            "VID_20240101_000001_00_001.mp4",
            "PHOTO_20240101_000001_00_001",
        }, by_name
        vid = by_name["VID_20240101_000001_00_001.mp4"]
        pic = by_name["PHOTO_20240101_000001_00_001"]
        assert vid["mode"] == "mutipart" and len(vid["uncompletedParts"]) == 3, vid
        assert pic["mode"] == "simple" and "uncompletedParts" not in pic, pic
        assert [p["partNum"] for p in vid["uncompletedParts"]] == [0, 1, 2], vid
        print("derivatives ok: mediaId=%s parts=%d+1"
              % (d1["mediaId"], len(vid["uncompletedParts"])))

        # --- a second in-progress media must not leak into media 1 ----------
        d2, _ = derivatives(conn, "LMID_TEST_2", [
            {"name": "OTHER_0002.mp4", "suffix": "mp4", "size": len(other)},
        ])
        assert len(d2["uncompletedItems"]) == 1, d2
        d1b, _ = derivatives(conn, "LMID_TEST_1", body1["items"])
        assert {it["name"] for it in d1b["uncompletedItems"]} == set(by_name), d1b
        print("cross-media isolation ok")

        # --- upload multipart out of order + idempotent retry ---------------
        parts = {p["partNum"]: p for p in vid["uncompletedParts"]}
        etags = {}
        for num in (2, 0, 1):
            ent = entrance(conn, d1["mediaId"], vid["itemId"], num)
            assert ent["path"]["method"] == "PUT", ent
            off, length = parts[num]["offset"], parts[num]["length"]
            status, hdrs = upload_part(ent["path"]["url"], video[off:off + length])
            assert status == 200, (num, status)
            assert "ETag" in hdrs and hdrs["ETag"].startswith('"'), hdrs
            etags[num] = hdrs["ETag"]
        # retry an already-stored part: entrance must still hand out a URL
        ent = entrance(conn, d1["mediaId"], vid["itemId"], 1)
        off, length = parts[1]["offset"], parts[1]["length"]
        status, _ = upload_part(ent["path"]["url"], video[off:off + length])
        assert status == 200, status
        # and accept a chunked retry of another part (no Content-Length)
        ent = entrance(conn, d1["mediaId"], vid["itemId"], 0)
        off, length = parts[0]["offset"], parts[0]["length"]
        status, _ = upload_part_chunked(ent["path"]["url"], video[off:off + length])
        assert status == 200, status
        print("multipart upload ok: etags=%s (chunked retry ok)" % etags)

        # --- report (SDK forwards the ETag it saw) --------------------------
        r, rep = call(conn, "POST",
                      API + "/cloud/service/camera/upload/item/status/report",
                      {"mediaId": d1["mediaId"], "itemId": vid["itemId"],
                       "etag": etags[2].strip('"'), "partNum": 2})
        assert r.status == 200 and rep["code"] == 0, rep

        # --- simple-mode item (whole file, no partNum) ----------------------
        ent = entrance(conn, d1["mediaId"], pic["itemId"])
        status, hdrs = upload_part(ent["path"]["url"], photo)
        assert status == 200 and "ETag" in hdrs, (status, hdrs)
        print("simple upload ok")

        # --- bytes on disk --------------------------------------------------
        vid_path = os.path.join(root, "LMID_TEST_1",
                                "VID_20240101_000001_00_001.mp4")
        pic_path = os.path.join(root, "LMID_TEST_1",
                                "PHOTO_20240101_000001_00_001.insp")
        for path, expected in ((vid_path, video), (pic_path, photo)):
            assert os.path.exists(path), path
            with open(path, "rb") as fh:
                got = fh.read()
            assert got == expected, "stored bytes differ: %s" % path
        assert abs(os.path.getmtime(vid_path) - 1700000000) < 2, \
            os.path.getmtime(vid_path)
        # the background checksum sidecar must appear and match
        md5_path = vid_path + ".md5"
        for _ in range(100):
            if os.path.exists(md5_path):
                break
            time.sleep(0.02)
        assert os.path.exists(md5_path), md5_path
        with open(md5_path, encoding="ascii") as fh:
            sidecar = fh.read().split()[0]
        assert sidecar == hashlib.md5(video).hexdigest(), sidecar
        # no leftover part staging directories for completed items
        leftovers = [p for p in os.listdir(os.path.join(root, ".parts"))
                     if p == d1["mediaId"]]
        if leftovers:
            assert os.listdir(os.path.join(root, ".parts", leftovers[0])) == [], leftovers
        print("stored files ok:", os.path.basename(vid_path),
              len(video), "bytes;", os.path.basename(pic_path), len(photo), "bytes")

        # --- resume/complete report ----------------------------------------
        d1c, _ = derivatives(conn, "LMID_TEST_1", body1["items"])
        assert d1c["uncompletedItems"] == [], d1c
        assert d1c["completeParts"] == d1c["totalParts"] == 4, d1c
        print("resume/complete ok:", d1c["completeParts"], "/", d1c["totalParts"])

        # --- re-upload a completed item (retry after lost response) ---------
        ent = entrance(conn, d1["mediaId"], pic["itemId"])
        status, _ = upload_part(ent["path"]["url"], photo)
        assert status == 200, status
        print("completed-item retry ok")

        # --- uploaded list: uploadFinishTime is int64, other media absent ---
        r, lst = call(conn, "GET", API + "/cloud/service/camera/media/uploaded/list"
                                "?pageNumber=1&pageSize=20")
        assert lst["code"] == 0, lst
        assert lst["data"]["pageNumber"] == lst["data"]["totalPage"] == 1, lst
        assert len(lst["data"]["list"]) == 1, lst
        rec = lst["data"]["list"][0]
        assert rec["localMediaId"] == "LMID_TEST_1", rec
        assert isinstance(rec["uploadFinishTime"], int), rec
        assert not isinstance(rec["uploadFinishTime"], bool), rec
        print("uploaded list ok:", rec)

        # --- uploading list is an array of strings --------------------------
        r, upl = call(conn, "GET",
                      API + "/cloud/service/camera/media/uploading/list")
        assert upl["code"] == 0 and upl["data"] == ["LMID_TEST_2"], upl
        print("uploading list ok:", upl["data"])

        # --- capacity: both values must be JSON doubles ---------------------
        r, cap = call(conn, "GET",
                      API + "/cloud/service/camera/userInfo/usage/detail")
        assert cap["code"] == 0, cap
        assert isinstance(cap["data"]["total"], float), cap
        assert isinstance(cap["data"]["usage"], float) and cap["data"]["usage"] > 0, cap
        assert cap["data"]["total"] > cap["data"]["usage"], cap
        print("capacity ok:", cap["data"])

        # --- binding --------------------------------------------------------
        r, bind = call(conn, "GET", API +
                       "/cloud/service/camera/userInfo/binding/status?deviceId=ABC")
        assert bind["code"] == 0, bind
        print("binding ok:", bind.get("data"))

        # --- persisted state uses absolute sizes and completion time --------
        with open(os.path.join(root, "state.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        item_states = st["media"]["LMID_TEST_1"]["items"]
        assert all(it.get("completed_at") for it in item_states.values()), item_states
        assert all(it.get("etag") for it in item_states.values()), item_states
        print("state.json ok: completed_at + etag recorded")

        print("\nALL TESTS PASSED")
        return 0
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
