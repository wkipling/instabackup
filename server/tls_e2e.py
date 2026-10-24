#!/usr/bin/env python3
"""Full HTTPS end-to-end test for inscloud_server.py.

Unlike selftest.py (plain HTTP, protocol logic), this runs the whole flow over
TLS with real hostname verification, exactly as the camera does:

    config -> ping -> derivatives -> entrance -> PUT -> report -> uploaded list

Usage:
    python3 tls_e2e.py --cert ../camera/certs/server.crt \
        --key ../camera/certs/server.key --ca ../camera/certs/ca.crt

The test maps service-test.insta360.com to 127.0.0.1 locally; it does not
modify /etc/hosts.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inscloud_server as srv  # noqa: E402
import selftest as st          # noqa: E402

HOST = "service-test.insta360.com"


def upload_over_tls(url: str, payload: bytes, ctx: ssl.SSLContext):
    hostport, path = url.split("://", 1)[1].split("/", 1)
    conn = http.client.HTTPSConnection(hostport, context=ctx, timeout=15)
    try:
        conn.request("PUT", "/" + path, body=payload,
                     headers={"Content-Length": str(len(payload))})
        resp = conn.getresponse()
        resp.read()
        return resp.status, dict(resp.getheaders())
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--ca", required=True)
    args = ap.parse_args()

    root = tempfile.mkdtemp(prefix="inscloud-tls-")
    content = bytes((i * 37) & 0xFF for i in range(3000))

    cfg = type("A", (), {
        "root": root, "bind": "127.0.0.1", "port": 0,
        "cert": args.cert, "key": args.key, "public_base": "",
        "part_size": 1024, "concurrency": 3,
    })()
    httpd = srv.make_server(cfg)
    port = httpd.server_address[1]
    srv.Handler.public_base = "https://%s:%d" % (HOST, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # Route the camera hostname to loopback without touching /etc/hosts.
    orig_gai = socket.getaddrinfo

    def gai(host, p, *a, **k):
        if host == HOST:
            host = "127.0.0.1"
        return orig_gai(host, p, *a, **k)

    socket.getaddrinfo = gai
    ctx = ssl.create_default_context(cafile=args.ca)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED

    conn = http.client.HTTPSConnection(HOST, port, context=ctx, timeout=15)
    try:
        # certificate really was verified against the hostname
        conn.connect()
        cert = conn.sock.getpeercert()
        assert cert, "no peer certificate"
        assert any(HOST in v for k, v in cert.get("subjectAltName", ())
                   if k == "DNS") or HOST in str(cert.get("subject")), cert

        r, cfg_body = st.call(conn, "GET",
                              st.API + "/cloud/service/camera/upload/config")
        assert r.status == 200 and cfg_body["data"]["concurrency"] == 3, cfg_body
        r, ping = st.call(conn, "GET", "/actuator/ping")
        assert ping.get("msg") == "Insta360 Cloud", ping

        data, body = st.derivatives(conn, "LMID_TLS", [
            {"name": "TLS_TEST.mp4", "suffix": "mp4", "size": len(content)},
        ])
        item = data["uncompletedItems"][0]
        assert item["mode"] == "mutipart", item

        for part in item["uncompletedParts"]:
            ent = st.entrance(conn, data["mediaId"], item["itemId"],
                              part["partNum"])
            off, length = part["offset"], part["length"]
            status, hdrs = upload_over_tls(ent["path"]["url"],
                                           content[off:off + length], ctx)
            assert status == 200 and hdrs.get("ETag"), (status, hdrs)

        r, rep = st.call(conn, "POST",
                         st.API + "/cloud/service/camera/upload/item/status/report",
                         {"mediaId": data["mediaId"], "itemId": item["itemId"],
                          "etag": "x", "partNum": 2})
        assert r.status == 200 and rep["code"] == 0, rep

        out = os.path.join(root, "LMID_TLS", "TLS_TEST.mp4")
        assert os.path.exists(out), out
        with open(out, "rb") as fh:
            assert fh.read() == content, "stored bytes differ"
        md5_path = out + ".md5"
        for _ in range(100):
            if os.path.exists(md5_path):
                break
            time.sleep(0.02)
        with open(md5_path, encoding="ascii") as fh:
            assert fh.read().split()[0] == hashlib.md5(content).hexdigest()

        r, lst = st.call(conn, "GET",
                         st.API + "/cloud/service/camera/media/uploaded/list"
                         "?pageNumber=1&pageSize=1000")
        assert lst["data"]["list"][0]["localMediaId"] == "LMID_TLS", lst

        print("TLS end-to-end OK: hostname verified, %d bytes uploaded, "
              "etag + md5 sidecar present" % len(content))
        return 0
    finally:
        socket.getaddrinfo = orig_gai
        conn.close()
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
