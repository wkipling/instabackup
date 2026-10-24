# Recovered camera cloud-upload protocol (libInsCloudSdk.so)

All findings are from static analysis of
`vendor/firmware/extract/843464317/linux/usr/lib/libInsCloudSdk.so`
(X5 firmware rootfs, SDK version `1.0.2`, build path
`inscloud_sdk-0.1/src/module/network/UploadTaskManager.cc`), cross-checked
against the X4 Air 1.2.7 SDK (`libInsCloudSdk.so`, same SDK version/build path,
same endpoint strings and JSON field names).

Decompiled functions: `analysis/ins-functions/` (197 functions, Ghidra 12.2,
image base 0x100000). Annotated assembly with PLT/string resolution:
`analysis/asm-annotated/`. Helper: `tools/annotate_asm.py`.

## Runtime configuration (InsCloudConfig)

`UploadTaskManager` embeds `InsCloudConfig` at `this+0x3E0`. It is a struct of
`std::string`s (32 bytes each in libstdc++ ABI). Observed offsets:

| Config offset | Manager offset | Meaning | Used for |
|---|---|---|---|
| +0x00 | +0x3E0 | SQLite DB path | `SqliteDelegate::Init` |
| +0x20 | +0x400 | firmware version | `x-insta360-firmware-ver` |
| +0x40 | +0x420 | camera model | `x-insta360-camera-model` |
| +0x60 | +0x440 | account token | `Authentication` |
| +0x80 | +0x460 | **cloud base URL** | request base |
| +0xA0 | +0x480 | user agent | `User-Agent` |

The literal `https://cloud.insta360.com/` in `.rodata` (0x3172D0) is **only**
used as the `Referer` header value, not as the request base. The actual base
comes from the config, so it is a runtime value set by the caller
(`instaAIP`, which loads the SDK; its static initializer and runtime override
blob are described below).

`ClientConfig` additionally exposes (via curl in `CurlHttpClient::makeRequest`):

- `CURLOPT_SSL_VERIFYPEER = 1`
- `CURLOPT_SSL_VERIFYHOST = 2`
- `CURLOPT_CAINFO` / `CURLOPT_CAPATH`, set only when the corresponding
  runtime config string is non-empty; otherwise libcurl's build defaults are
  used (`/etc/ssl/certs/cacert.pem` and `/etc/ssl/certs`)
- `CURLOPT_DNS_SERVERS` (log: "specify nameserver: %s")
- optional `CURLOPT_PROXY` / port, `CURLOPT_SSL_CIPHER_LIST`
- **no** `CURLOPT_PINNEDPUBLICKEY`, no `sha256//` pin strings

So TLS is verified with the system CA bundle; a custom CA can be installed as
`CURLOPT_CAINFO` (`/etc/ssl/certs/cacert.pem`) and/or as a subject-hash file
under `CURLOPT_CAPATH` (`/etc/ssl/certs/<hash>.0`). Both redirected hosts
(`service-test.insta360.com`, `cloud.insta360.com`) are covered by one
certificate.

## HTTP methods

`ins::http::Method` maps to the index table at vaddr 0x3B0008:

```
0 GET, 1 HEAD, 2 POST, 3 PUT, 4 DELETE, 5 CONNECT, 6 OPTIONS, 7 PATCH, 8 TRACE
```

URL literals are built as `config.baseUrl + "/cloud/service/..."`.  The
base URL set by X5 `instaAIP` is
`https://service-test.insta360.com/cloud-service` (no trailing slash), so the
real X5 request path is **`/cloud-service/cloud/service/camera/...`**.
`inscloud_server.py` accepts both that path and the bare
`/cloud/service/camera/...` form.

X4 1.9.21 differs at the gateway prefix: the image contains no
`service-test.insta360.com` and no `/cloud-service` strings.  Its SDK
(`libinsCloudSdk.so`) has the origin `https://cloud.insta360.com/`, and
`instaAIP` has `https://cloud.insta360.com/actuator/ping`; the expected X4 API
path is therefore the bare `/cloud/service/camera/...`.  The DNS redirect and
certificate cover both origins, and the server accepts both forms, so the same
`bootup.sh`/server pair works on both models without selecting a base URL.

X4 Air 1.2.7 is a newer variant: its `instaAIP` initializer sets
`https://service-test.insta360.com/cloud-service`, exactly like X5, while still
using the `libInsCloudSdk.so` filename.  Its effective path is therefore
`/cloud-service/cloud/service/camera/...`.  The X4 Air SDK also keeps
`https://cloud.insta360.com/` as the `Referer`.  The server's dual-prefix
handling covers X4, X4 Air and X5.

Example:

```
POST https://service-test.insta360.com/cloud-service/cloud/service/camera/upload/derivatives
```

## Common headers

Upload endpoints (derivatives, entrance, report, config, capacity, ...):

```
x-insta360-sdk-ver:      1.0.2                     (built into SDK)
x-insta360-firmware-ver: <config+0x20>
x-insta360-camera-model: <config+0x40>
Authentication:          <config+0x60>             (account token)
User-Agent:              <config+0xA0>
Referer:                 https://cloud.insta360.com/
X-Insta360-Trace-Id:     <ins::GetTraceId()>
Content-Type:            application/json; charset=utf-8   (where a body exists)
```

`reportUploadStatus` uses `x-insta360-newtoken: <token>` instead of
`Authentication` (verified from string xrefs; re-check if it matters).

## Endpoint 1 — `GET /cloud/service/camera/upload/config`

Response parsed (`queryUploadConfig`):

```json
{
  "code": 0,
  "data": {
    "concurrency": 4,
    "reportUploadSpeedConfig": { "captureIntervalMs": 10000 }
  }
}
```

Only `data.concurrency` and
`data.reportUploadSpeedConfig.captureIntervalMs` are read.  Both are optional
but must be integers; note that `reportUploadSpeedConfig` is **inside `data`**,
not at the top level.

## Endpoint 2 — `POST /cloud/service/camera/upload/derivatives`

Request body (rapidjson) built by `buildUploadInfoHttpRequest`:

```json
{
  "localMediaId": "<InsCloudFileInfoList+0x00>",
  "mediaType":    "<+0x20>",
  "shootTime":    <uint64 +0x60>,
  "mediaName":    "<+0x40>",
  "duration":     "<string>",       // optional, only if video property present
  "framerate":    "<string>",
  "width":        "<string>",
  "height":       "<string>",
  "submediatype": "<string>",
  "items": [
    { "size": <uint64 file size>, "name": "<item+0x00>", "suffix": "<item+0x20>" }
  ]
}
```

Note: `duration`/`framerate`/`width`/`height`/`submediatype` are serialized as
JSON **strings** (`std::to_string`), not numbers. `items[]` maps to files of
the media group (e.g. video + LRV) and is used to build per-file maps keyed by
the item's `name`.

Response parsed by `parseUploadInfoHttpBodyByDOM` (SAX variant exists too):

```json
{
  "code": 0,
  "msg": "optional",
  "data": {
    "localMediaId": "...",
    "mediaId": "...",
    "concurrency": <uint>,
    "uncompletedItems": [
      {
        "name": "<file name, matches request item name>",
        "itemId": "...",
        "mode": "simple" | "mutipart",
        "signType": "MD5" | "NONE",
        "uncompletedParts": [                 // only when mode != "simple"
          { "partNum": <uint>, "offset": <uint64>, "length": <uint64> }
        ]
      }
    ],
    "totalParts": <int>,
    "completeParts": <uint>
  }
}
```

`totalParts - completeParts` is used to reserve the UploadInfo vector.
`uncompletedParts` is optional for `simple` mode.  Each `uncompletedParts`
entry must use JSON unsigned integers for `partNum` and uint64 for `offset` and
`length`.

A media group is the set of `items[]` in this request.  The server should
return only those items: returning pending items that belong to another
`localMediaId` makes the SDK upload them under the wrong media id.

## Endpoint 3 — `GET /cloud/service/camera/upload/item/entrance?...`

URL (built by `buildUrlInfoHttpRequest`):

```
{base}/cloud/service/camera/upload/item/entrance?mediaId=<mediaId>&itemId=<itemId>&
```

Then:

- if `signType == "MD5"`:
  - if `mode == "simple"`: `sign=<urlencode(md5(file, 0, fileSize))>`
  - else: `partNum=<n>&sign=<urlencode(md5(file, offset, length))>`
- otherwise no further query parameters (URL ends with `&`).

`signType` values seen as constants: `MD5`, `NONE`.
MD5 is computed by `ins::ComputeFileContentMD5(path, offset, length, out)`.
`mode` values: `simple`, `mutipart` (sic).

Response parsed by `parseUrlInfoHttpBody`:

```json
{
  "code": 0,
  "msg": "optional",
  "data": {
    "localMediaId": "...",
    "mediaId": "...",
    "path": {
      "url": "https://....",
      "method": "PUT" | "POST",
      "headers": [ { "name": "Content-Type", "value": "..." } ]
    },
    "partNum": <uint>        // required when mode != "simple"
  }
}
```

The SDK uses `path.method` (string) to select the HTTP method via a static map
`{"POST":2, "PUT":3}`.

A part that is already stored may still be requested again (for example when
the previous 200 response was lost).  The server must return a normal 2xx
response for that retry rather than an error.

## Endpoint 4 — file upload to the pre-signed URL

`buildUploadFileHttpRequest`:

- method = parsed from `path.method`
- URL = `path.url` (pre-signed, can be any host)
- copies **all** `path.headers` verbatim
- adds `Content-Length: <UrlInfo.length>`
- opens `filePath` as binary ifstream
- if `mode == "simple"`: body = whole file
- else: `seekg(offset)`, body = file from offset
  (no `Content-Range` header is added by the SDK — it must come from
  `path.headers` if the server wants it)
- optional progress callback

The SDK reads the **response `ETag` header** (case-insensitive), strips the
surrounding quotes (`ins::TrimQuotes`) and later sends that value in
`item/status/report`.  Any non-empty token is accepted by the mock server;
real object storage commonly returns `"<md5>"`.

## Endpoint 5 — `POST /cloud/service/camera/upload/item/status/report`

Body (`buildReportInfoHttpRequest`):

```json
{
  "mediaId": "<ReportInfo+0x28>",
  "itemId":  "<ReportInfo+0x48>",
  "etag":    "<ReportInfo+0x58>",
  "partNum": <uint ReportInfo+0x10>     // only if ReportInfo+0x38 == "mutipart"
}
```

Also sends `Content-Type: application/json; charset=utf-8`, `Authentication`,
`Referer`, `X-Insta360-Trace-Id`, `x-insta360-*` headers.

## Other endpoints

| Endpoint | Function | Notes |
|---|---|---|
| `/cloud/service/camera/userInfo/usage/detail` | `QueryCapacity` | requires `data` object with **double** `total` and `usage` |
| `/cloud/service/camera/userInfo/binding/status?deviceId=` | `QueryBindingStatus` | parser returns the top-level `code` as an int status |
| `/cloud/service/camera/media/uploaded/list?pageNumber=&pageSize=` | `queryUploadRecordFromServer` | `data` object with int `pageNumber`, int `totalPage`, `list[]`; each record needs string `localMediaId`, string `mediaId`, **int64** `uploadFinishTime` in **milliseconds** (`SetUploadRecordFromServer` divides by 1000 before storing seconds in SQLite) |
| `/cloud/service/camera/media/uploading/list` | `queryUploadingList` | `data` is an array of `localMediaId` strings |
| `/cloud/service/camera/upload/firmwareConfig` | `QueryFirmworkConfig` | `data` must be a JSON **object** (serialized verbatim) |
| `/cloud/service/camera/upload/speed/report` | `reportUploadSpeedInfo` | `throughput`, `latency`, `reqHost`, `region`, `timeZone`, `cpuUseRate`, `memUseRate`, `checkTime` |
| `https://cloud.insta360.com/actuator/ping` | `ping_handler` | response must be a JSON object whose `msg` is the **string** `"Insta360 Cloud"` |

## JSON key enum (`.data` vaddr 0x3B0008, used by a key-index helper)

Note: this enum is a *different* table from the per-endpoint key pointers used
by the DOM/SAX parsers.  The latter were resolved from the annotated assembly
for this document; do not infer response key names from the enum alone.

```
0 GET 1 HEAD 2 POST 3 PUT 4 DELETE 5 CONNECT 6 OPTIONS 7 PATCH 8 TRACE
...
37 code 38 data 39 localMediaId 40 mediaType 41 shootTime 42 mediaName
43 duration 44 framerate 45 width 46 height 47 submediatype 48 items
49 name 50 suffix 51 size 52 mediaId 53 concurrency 54 uncompletedItems
55 completeParts 56 totalParts 57 mode 58 simple 59 mutipart 60 itemId
61 signType 62 MD5 63 NONE 64 uncompletedParts 65 partNum 66 offset
67 length 68 sign 69 path 70 url 71 method 72 headers 73 value 74 etag
75 result 76 throughput 77 latency 78 reqHost 79 region 80 timeZone
81 cpuUseRate 82 memUseRate 83 checkTime 84 reportUploadSpeedConfig
85 captureIntervalMs
```

## Local persistence

SQLite table `tbl_media_upload_record(local_id, server_id, status, ctime, utime, reserve)`.
DB path comes from `InsCloudConfig+0x00`.

## Camera-side configuration source (instaAIP)

`instaAIP` (stripped, 5.7 MB) loads the SDK and owns a global
`ins::InsCloudConfig` object at vaddr `0x65F660` (Ghidra image base 0x100000).
It is built in the static initializer `_INIT_1` (0x10B208) as follows:

```
InsCloudRequestHeader (config+0x20 .. +0xC0):
  +0x00 firmware version "0.5.3"
  +0x20 camera model     "Insta360 X5"
  +0x40 account token    hardcoded JWT (test token, eyJhbGciOiJIUzI1NiJ9...)
  +0x60 base URL         "https://service-test.insta360.com/cloud-service"
  +0x80 user agent       "insta360/x5"
InsCloudConfig (0x65F660):
  +0x00 db path string
  +0x20 header (above)
  +0xC0 vector<string> name servers = ["8.8.8.8"]
  +0xE0 ReportUploadSpeedConfig
  ...  timeouts (30000 ms, 5000 ms, 1024, 30, ...)
```

Other static strings: `https://cloud.insta360.com/actuator/ping`, release
type `release`, build hash `a7b7a2e9518a2e227fde054fe465ab274b2770c4`.

### Runtime update blob

`FUN_0014EE40` (`ins_cloud_sdk_upload_linux_cfg`, reached from the wrapper at
0x14D068 / PLT handler 0x109F40..) copies a packed structure:

```
offset size  field
0x000  512   account token        -> config header +0x40
0x200  256   base URL             -> config header +0x60
0x300  128   ping domain URL      -> "https://cloud.insta360.com/actuator/ping" string
0x380    1   upload order (0/1)
0x381    1   delete-file flag
0x382    1   upload-order-present flag
0x383   32   firmware version     -> config header +0x00
```

After the update the five header strings are re-assigned into the global
config (`std::string::_M_assign` on 0x65F680/0x65F6A0/0x65F6C0/0x65F6E0/
0x65F700). This means the base URL can theoretically be changed at runtime by
whoever supplies this blob (RTOS/app). For the local redirect both hosts are
intercepted regardless.

### Boot-time code execution hook

`/etc/init.d/S99bootdone` contains:

```sh
if [ -x /tmp/SD0/instaAIP ]; then /tmp/SD0/instaAIP & ; else /usr/bin/instaAIP & ; fi
...
if [ -x /tmp/SD0/bootup.sh ]; then /tmp/SD0/bootup.sh ; fi
```

So the stock firmware runs a user script from the SD card as root on every
boot, and even allows overriding the `instaAIP` binary. This is used by
`camera/bootup.sh` to install the DNS redirect and local CA without modifying
firmware.

## RTOS -> instaAIP action queue (enabling without the app)

`instaAIP` starts a thread named `cloud_msg` (`FUN_0014cfec`, Ghidra
0x14cfec) that receives SysV messages and dispatches on one action byte
(`recv_action_handler`, 0x14d31c):

| field | value |
|---|---|
| queue key | `0x44455255` (`msgget(key, IPC_CREAT|0666)` = flags `0x3b6`) |
| `mtype` | `1` |
| text size | `0x408` |
| action | `mtext[4]` |
| `mtext[0..3]` | `1` (as sent by `FUN_0014cf40`) |

Actions relevant to backup:

| action | handler | effect |
|---|---|---|
| 0 | `ins_cloud_sdk_upload_start` | TCP tune + `InsCloudClient::upload()`; `setUploadFileInfoList(&DAT_00694d70)` is passed at 0x14f8f4 |
| 1 | `ins_cloud_sdk_upload_stop` | stop uploader |
| 2 | `ins_cloud_sdk_upload_pause` | pause |
| 3 | `ins_cloud_sdk_upload_restart` | restart |
| 6 | `ins_cloud_sdk_upload_get_capacity` | capacity |
| 7 | `ins_cloud_sdk_file_group_cfg` | `FUN_00152d8c(0)`: scan `/tmp/SD0/DCIM`, group media, fill `DAT_00694d70` (vector of `InsCloudFileInfoList`, 0x90 bytes each) |
| 8 | `ins_cloud_sdk_query_local_uploaded_file` | forced scan + `checkFile`, queries `/media/uploaded/list` and reconciles SQLite |
| 9/10 | `FUN_0015038c(1/0)` | uploaded-state updates |
| 11 | `FUN_001503f0` | clear uploaded list |
| 12 | `FUN_00150780` | report uploaded filenames to RTOS |
| 15 | `ins_cloud_sdk_db_save` | copy DB to RTOS via frame share |

The upload path has **no binding/entitlement check** (`QueryBindingStatus` is
only exposed to the RTOS, not called by `UploadTaskManager::StartTask`).  The
app-free trigger therefore sends 8, 7, 0 in order via `camera/cloudkick`.
See [`../notes/ENABLE.md`](../notes/ENABLE.md).

## Open questions

1. Which component calls the `linux_cfg` update blob and whether it ever
   replaces the base URL (app vs RTOS). Redirecting both known hosts makes
   this non-blocking.
2. Exact semantics of `signType` values beyond `MD5`/`NONE`.
3. Whether the camera validates firmware images on SD update (not needed for
   the SD-card hook, only relevant for persistent rootfs patches).
