# Cloud backup reverse engineering — progress log

Owner: cloud-backup agent. Work area: `research/cloud-backup/`.
Do not edit files outside this directory unless agreed with the other agent.
`research/` is gitignored, so the large vendor artifacts are local-only.

## Goal

Understand the Insta360 camera's paid "auto cloud backup" / Insta360+ upload
path and replace the vendor cloud with a self-hosted backup target.

## Constraints / non-goals

- Interoperability only; no redistribution of vendor bytes.
- Do not modify the working Linux port outside `research/cloud-backup/`.
- Camera hardware is not currently attached; use static analysis first.

## Timeline / what has been established

### 1. Desktop Studio does not upload camera files

The shipped QML/English catalogue distinguishes:

- "Cloud files" in Studio are consumed, downloaded and cloud-exported.
- Camera media is *uploaded to the cloud by the camera*.
- `native/data/en-US.json` contains:
  - `footagepanel.cloud.cameratips` = "Use the camera to connect to the
    Insta360 App to upload files."
  - `footagepanel.cloud.uploadtips` = "Currently, only the X2/X3/X4/X5
    models support uploading files to the cloud."
  - `file_not_in_cloud.tips` = "... You can back up the file to the cloud
    using the camera."
  - `task_center.no_equity_advertising` = "You need to subscribe to
    Insta360+ membership and synchronize files to the cloud through the
    camera before you can use cloud export."

Studio's only cloud endpoints are `/cloud/service/media/*`,
`/cloud/service/export/*`, `/cloud/service/share/*` (download/edit/share).
There is no camera-media upload path in Studio.

Conclusion: the paid backup feature lives in the camera firmware plus the
Insta360 mobile app setup/entitlement flow.

### 2. Android app

Downloaded official APK 2.34.7 (2026-10-19):

- `research/cloud-backup/vendor/android/Insta360_v2.34.7_538614.apk`
- size 1,792,453,524 bytes
- sha256 `337527477abdf1a91a0acdedcf0c9fecb0b94ef741e79a28e5a740ece1df20e8`

The APK is protected by 360 Jiagu / Ashield:

- `classes.dex` begins with a small valid stub DEX containing only
  `com.ashield.Stub`; the real DEX is appended/encrypted after it.
- Header at `0x18e4`: `adx0`, declared blob size, metadata length.
- Metadata (396-400 bytes) is XOR-encoded with `0x52` and reads as
  `activityName`, `appName`, `fastLevel`, `jiaguVersion1.5.1.3`,
  `ls6372`, `opt1`, `pkgcom.arashivision.insta360akiko`,
  `versionCode538614`, `versionName2.34.7`.
- Native packer: `libashield.so`; contains `L10DecryptRC4`,
  `UncompressLZMA`, `AShieldData`, `AShieldDataFlat`, `AShieldArtLoader`,
  `AShieldDvmLoader`, `inflate`, `MessageDigest`.
- Static strings nevertheless reveal the relevant app classes:
  - `com.arashivision.insta360.cloud.ui.view.CloudBackUpView`
  - `com.arashivision.insta360.cloud.ui.activity.CloudSyncUploadActivity`
  - `com.arashivision.insta360.cloud.ui.viewmodel.CloudSyncUploadViewModel`
  - `com.arashivision.insta360.cloud.wificonfig.CloudBackupPromote*`
  - `com.arashivision.insta360.cloud.ui.delegate.CloudBindAndWifiConfigDelegate`
  - `com.arashivision.camera.command.GetCloudStorageUploadStatusCmd`
  - `com.arashivision.camera.command.SetCloudStorageUploadStatusCmd`
  - protobuf: `insta360.messages.CloudStorageUploadParams`,
    `GetCloudStorageUploadStatus`, `SetCloudStorageUploadStatus`,
    `NotificationCloudUploadStatus`, `SetCloudStorageUploadStatusResp`.
- The camera API for the app is a protobuf command pair:
  set/get "cloud storage upload status" (likely starts/stops the camera's
  built-in uploader and exposes progress/condition).

An older APK (v1.83.3, 2026-04-21) uses the same packer and the same
`classes.dex` stub layout. Static unpacking is deferred until needed.

### 3. Camera firmware

Downloaded official X5 firmware (latest currently listed; useful as the
reference rootfs) and extracted the UBI/UBIFS root filesystem:

- `research/cloud-backup/vendor/firmware/Insta360X5FW.bin`
- size 336,647,315 bytes
- sha256 `ac3d61c4745a61580e4ad9b56c9d9d2b13ddf8cf3164f7f724ec6339a4a53971`
- UBI starts at `0x104820df`, PEB size `0x40000`,
  `vid_hdr_offset=0x1000`, `data_offset=0x2000`.
- Extracted with `ubi_reader` to:
  `research/cloud-backup/vendor/firmware/extract/843464317/linux/`
- Rootfs is Linux 5.10.61 (Ambarella/ambalink), glibc 2.31,
  curl/OpenSSL/protobuf-lite/rapidjson/sqlite.

Key artifact: `usr/lib/libInsCloudSdk.so` (3,811,216 bytes), the camera's
cloud upload SDK. `usr/bin/instaAIP` is the app/IPC service that loads it
and exposes `ins_cloud_*` control functions. Scripts
`usr/local/share/script/cloud_tcp_tune.sh` and `cloud_tcp_restore.sh` show
upload-specific TCP tuning.

`libInsCloudSdk.so` is **not stripped** (12,203 symbols, full C++ names,
`.rodata` at vaddr 0x311400). Relevant API paths recovered:

```
https://cloud.insta360.com/
/cloud/service/camera/upload/config
/cloud/service/camera/upload/item/entrance?
/cloud/service/camera/upload/item/status/report
/cloud/service/camera/upload/speed/report
/cloud/service/camera/upload/derivatives
/cloud/service/camera/upload/firmwareConfig
/cloud/service/camera/media/uploaded/list?
/cloud/service/camera/media/uploading/list
/cloud/service/camera/userInfo/binding/status
/cloud/service/camera/userInfo/usage/detail
```

HTTP headers:

```
x-insta360-newtoken: <account token>
x-insta360-firmware-ver: <camera firmware>
X-Insta360-Trace-Id: <trace>
Content-MD5
Content-Range
Content-Length
Content-Type
ETag
```

JSON/body field names recovered from `.rodata` (near 0x311900):

```
localMediaId, mediaId, fileName, filePath, uploadMode,
itemId, signType, partNum, offset, length, mediaSize,
url, method, headers { eTag ... }
mediaType, mediaName, width, submediatype, name, size,
completeParts, totalParts, mutipart, uncompletedParts,
sign, path, reqHost, region,
cpuUseRate, memUseRate, reportUploadSpeedConfig
```

SDK symbols (not stripped) include:
`UploadTaskManager::buildUploadInfoHttpRequest`,
`buildUrlInfoHttpRequest`, `buildUploadFileHttpRequest`,
`parseUploadInfoHttpBodyByDOM/SAX`, `parseUrlInfoHttpBody`,
`requestUploadInfo`, `requestUrl`, `uploadFile`,
`InsCloudClient::QueryCapacity`, `ComputeFileContentMD5`.

The SQLite table `tbl_media_upload_record` tracks
`local_id / server_id / status / ctime / utime / reserve`; uploads are
resumable/multipart and the server is queried for already-uploaded parts.

### 4. Firmware / app interaction hypothesis

The mobile app configures the camera's uploader over the camera command
channel (protobuf `SetCloudStorageUploadStatus` / `CloudStorageUploadParams`)
after the user binds the camera to their Insta360 account and enables
auto-backup. The camera then does the actual network upload through
`libInsCloudSdk.so`, authenticating with `x-insta360-newtoken` and using
pre-signed URLs returned by `/cloud/service/camera/upload/...`.

Therefore a self-hosted backup can be achieved by one of:

1. Replace the cloud base URL / intercepted DNS + trusted CA so the camera
   talks to our server, and implement the small subset of the cloud API used
   by `libInsCloudSdk.so`.
2. Patch the SDK / rootfs to point at our server (requires firmware
   signature/update analysis).
3. If the camera is not rootable, reproduce the feature at the PC side:
   detect the mounted camera volume and upload files to the user's server
   (functional replacement, not protocol-compatible).

Option 1 is the cleanest if the camera does not pin certificates; option 2
needs the update-signing pipeline; option 3 is a fallback.

## Next steps

> Historical list; items 1-6 were completed in sections 5-11.  The current
> remaining work is the X4 Air hardware checklist in section 11.

1. Decompile `libInsCloudSdk.so` functions with Ghidra 12.2
   (`research/cloud-backup/tools/ghidra/ghidra_12.2_PUBLIC`), especially:
   - `buildUploadInfoHttpRequest` (0x239038, 5016 bytes)
   - `QueryCapacity` (0x248408, 7828 bytes)
   - `buildUrlInfoHttpRequest`, `buildUploadFileHttpRequest`
   - `parseUploadInfoHttpBody*`, `parseUrlInfoHttpBody`
2. Reconstruct exact request/response JSON schemas and auth semantics.
3. Check TLS verification / certificate pinning in `CurlHttpClient` and
   `InsCloudClient`; identify whether a custom CA can be installed.
4. Check the camera's update/signature mechanism in `instaAIP` / boot
   scripts to see if a modified rootfs is flashable.
5. Build a mock server under `research/cloud-backup/server/` implementing
   the endpoints with local-disk storage.
6. Validate with a camera if available; otherwise provide a replay/fixture
   harness using the recovered JSON shapes.

## Reproduce

```sh
# APK
curl -L -o research/cloud-backup/vendor/android/Insta360_v2.34.7_538614.apk \
  https://wassets.insta360.com/common/9dce1ffae7bd4f1196085d36763caf9b/Insta360_v2.34.7_538614.apk

# firmware
curl -L -o research/cloud-backup/vendor/firmware/Insta360X5FW.bin \
  https://wassets.insta360.com/common/3b84ad45cf9444ef96e6ccda3c6235b7/Insta360X5FW.bin

# X4 Air 1.2.7 (manifest URL; product page slug is insta360-x4air)
mkdir -p research/cloud-backup/vendor/firmware/x4-air
curl -L -o research/cloud-backup/vendor/firmware/x4-air/Insta360X4AirFW_1.2.7.bin \
  https://wassets.insta360.com/common/763cc41c8333401ab3d5d2b22dc3aea4/Insta360X4AirFW.bin

# extract UBI rootfs
~/.local/bin/ubireader_extract_files \
  -o research/cloud-backup/vendor/firmware/extract \
  research/cloud-backup/vendor/firmware/Insta360X5FW.bin
```

## 5. Protocol recovery (2026-10-23)

A full Ghidra decompile pass over all 197 `ins::` global functions is done
(`analysis/ins-functions/`, image base 0x100000; helper scripts
`tools/ghidra-scripts/DecompileByAddress.java` and `DumpAsm.java`).
Assembly is annotated with PLT symbols and resolved string pointers by
`tools/annotate_asm.py` into `analysis/asm-annotated/`.

**The complete upload protocol is documented in `analysis/PROTOCOL.md`.**
Highlights:

- Request bodies are rapidjson objects, endpoint responses are
  `{code, msg, data{...}}`.
- `/cloud/service/camera/upload/config` (GET) → `data.concurrency`,
  `reportUploadSpeedConfig.captureIntervalMs`.
- `/cloud/service/camera/upload/derivatives` (POST) → per-media item list;
  response yields `mediaId`, `concurrency`, `uncompletedItems[]` with
  `{name,itemId,mode,signType,uncompletedParts[]}`.
- `/cloud/service/camera/upload/item/entrance?mediaId=..&itemId=..[&partNum=..]&sign=<md5>`
  (GET) → `data.path{url,method,headers[]}` pre-signed upload target.
- Upload = PUT/POST to that pre-signed URL with the server-provided headers,
  `Content-Length`, and file bytes (whole file for `simple`, seek to `offset`
  for `mutipart`).
- `/cloud/service/camera/upload/item/status/report` (POST) reports
  `{mediaId,itemId,etag[,partNum]}`.
- `InsCloudConfig` layout: +0x00 db path, +0x20 firmware ver, +0x40 camera
  model, +0x60 account token, +0x80 **base URL**, +0xA0 user agent.
  The hardcoded `https://cloud.insta360.com/` literal is only the `Referer`.
- TLS: curl peer+host verification ON, configurable CAINFO/CAPATH/DNS/proxy,
  **no certificate pinning**. This makes DNS + custom CA redirection viable if
  the base URL can be pointed at our host.

Next: find who fills `InsCloudConfig` in `instaAIP` (stripped binary, loads
`libInsCloudSdk.so`). This decides between DNS/CA redirect and rootfs patch.

## 6. Redirect solution implemented (2026-10-23)

**No firmware modification is required on X5.** `/etc/init.d/S99bootdone` in
the stock X5 rootfs runs `/tmp/SD0/bootup.sh` as root on every boot (and even
tolerates an `instaAIP` override from the SD card). We use that hook.  X4
1.9.21 has this hook commented out; see section 10 for its one-time preload
bootstrap.

### Camera-side default configuration (instaAIP)

Static initializer `_INIT_1` (0x10B208) builds the global `InsCloudConfig`:

- base URL `https://service-test.insta360.com/cloud-service`
- hardcoded test JWT as the account token
- camera model `Insta360 X5`, firmware `0.5.3`, UA `insta360/x5`
- name servers `["8.8.8.8"]` (libcurl is built with c-ares, so this is used)

A runtime blob (`FUN_0014EE40`, token/base URL/ping URL/order/fw) can override
the endpoint; both known hosts are redirected, so this does not matter.

### Components added

| Path | Purpose |
|---|---|
| `server/inscloud_server.py` | stdlib-only HTTPS mock of the camera cloud API; resumable multipart uploads, local-disk storage, JSON state, request log |
| `server/selftest.py` | end-to-end protocol self test (passes) |
| `camera/bootup.sh` | SD-card boot hook: hosts + DNS override, CA bind-mount, optional instaAIP restart/telnetd |
| `camera/make_certs.sh` | local CA + server cert with SANs for both cloud hostnames |
| `camera/cloud-backup.conf.example` | server IP / options |
| `README.md` | setup, verification, troubleshooting, revert and security notes |

The SDK verifies TLS (peer + hostname) but has **no certificate pinning** and
defaults to `/etc/ssl/certs/cacert.pem`, so a bind-mounted CA bundle is
sufficient. DNS is redirected by adding `8.8.8.8/32` to `lo` and running the
camera's own `dnsmasq` on it, plus `/etc/hosts` entries as a fallback.

Validation done so far: protocol self test (config → derivatives → entrance →
PUT parts → report → stored bytes → resume/complete → uploaded list) and a TLS
handshake with hostname verification using the generated CA/cert.

Remaining: on-device validation with a physical camera (needs hardware), and
ideally a packet capture to confirm which base URL the runtime config uses.

## 7. Parser conformance audit + hardening (2026-10-23, continued)

A second pass compared the mock server against the actual response parsers in
`libInsCloudSdk.so`.  Several assumptions from section 5 were wrong and have
been corrected.

### 7.1 The real request path has TWO `/cloud-service` prefixes

`instaAIP`'s `_INIT_1` (0x10B208) builds the `InsCloudRequestHeader`:

```
0x694e70  firmware "0.5.3"
0x694e90  model    "Insta360 X5"
0x694eb0  token    hardcoded JWT
0x694ed0  base URL "https://service-test.insta360.com/cloud-service"
0x694ef0  UA       "insta360/x5"
```

That header is copied to `InsCloudConfig+0x20` (global `0x65F660`); the SDK
builds URLs as `baseUrl + "/cloud/service/..."`.  Therefore the camera calls:

```
https://service-test.insta360.com/cloud-service/cloud/service/camera/upload/...
```

The earlier server only routed `/cloud/service/...` and would have returned 404
for every request.  `inscloud_server.py` now strips a leading `/cloud-service`
and accepts both forms.

### 7.2 Response-schema corrections (verified in annotated assembly)

| Endpoint | Requirement | Old mock | Fixed |
|---|---|---|---|
| `upload/config` | `data.reportUploadSpeedConfig.captureIntervalMs` (nested) | top-level | nested under `data` |
| `upload/firmwareConfig` | `data` must be an object | string `"{}"` | object `{}` |
| `media/uploaded/list` | `data.list[].uploadFinishTime` must be **int64** | string | integer milliseconds |
| `userInfo/usage/detail` | `data.total`/`data.usage` must be **double** | double | unchanged (verified) |
| `userInfo/binding/status` | parser returns top-level `code` as int status | `data:1` | `code` already 0; documented |
| `media/uploading/list` | `data` is an array of strings | correct | unchanged (verified) |
| item upload response | SDK reads `ETag` header, trims quotes | correct | unchanged (verified) |
| `derivatives` | must return only the items in this request | returned all pending media | restricted to requested names |
| `https://cloud.insta360.com/actuator/ping` | `msg` must be the **string** `"Insta360 Cloud"` | plain text `ok` | JSON `{"code":0,"msg":"Insta360 Cloud"}` |

`uploadFinishTime` units were confirmed independently: `SetUploadRecordFromServer`
divides the value by 1000 before writing `ctime`/`utime` (seconds) to SQLite, so
the wire value is milliseconds.

Key pointers were resolved from the ELF `.data` table (e.g. `0x3b1c50 ->
reportUploadSpeedConfig`, `0x3b1a88 -> ETag`) and the annotated assembly in
`analysis/asm-more/`.

### 7.3 Server hardening (`server/inscloud_server.py`)

- **Streaming parts straight to their final offset** in a sparse file under
  `.parts/<mediaId>/<itemId>/data` (`os.pwrite`), instead of buffering the
  request body and assembling a second copy.  Large videos no longer need
  part-size RAM or 2x disk I/O.
- **Idempotent retries**: `entrance` no longer returns 409 for a stored part;
  a retried PUT is accepted and re-written (or drained for completed items).
- **No global lock during finalisation**: the completed sparse file is renamed
  into place outside the state lock; a whole-file checksum is computed in a
  background thread and written as a `md5sum` sidecar, so large videos never
  hit the camera's HTTP timeout.
- **Crash recovery**: resets stale `finalizing` flags on startup; if the file
  was moved but completion was not persisted, `_finalize` validates the
  existing output and completes it.
- **Cross-media isolation** in `derivatives` (only requested item names).
- **Suffix handling**: appends `items[].suffix` when `items[].name` lacks the
  extension (`PHOTO_...` + `insp` -> `PHOTO_....insp`).
- **`completed_at`** is recorded and used for `uploadFinishTime`.
- The camera's `shootTime` is preserved as the output file's mtime (accepts
  epoch seconds or milliseconds, with a plausibility check).
- TLS handshake resets no longer print tracebacks (`QuietServer`).
- Unknown POST/PUT routes close the connection instead of leaving an unread
  body to corrupt a keep-alive request.

### 7.4 Camera hook hardening (`camera/bootup.sh`)

- The stock Wi-Fi scripts call `killall hostapd dnsmasq` on mode changes
  (`sta_start_fast.sh`, `change_mode.sh`, ...), which would kill our resolver.
  A background watchdog now restarts dnsmasq within ~5 s and re-adds
  `8.8.8.8/32` to `lo` if it disappears.
- Upstream resolvers are read from `/etc/resolv.conf` (the udhcpc script
  writes the router's DNS there), excluding `127.*` and `8.8.8.8`; if none are
  usable it falls back to `1.1.1.1`/`8.8.4.4`.  `--no-resolv` avoids loops.
- The CA is installed as a **directory overlay** of `/etc/ssl/certs` when
  `ca.hash` is present, so both CAINFO (`cacert.pem`) and CAPATH
  (`<subject_hash>.0`) work.  Otherwise it falls back to per-file bind mounts
  of `cacert.pem` / `ca-bundle.crt`.
- `make_certs.sh` now emits `ca.hash` (openssl subject hash).
- Warns if `SERVER_PORT` is not 443 (the camera API always uses 443).
- Optional `camera/instaAIP-wrapper`: copy it to the SD root as `instaAIP`.
  The stock `S99bootdone` starts that file before `bootup.sh`, so the wrapper
  installs DNS/CA first and then `exec`s `/usr/bin/instaAIP`, eliminating the
  startup race.  (`RESTART_AIP` is ignored when the wrapper calls bootup.sh.)

### 7.5 Deployment

- `server/insta360-backup.service` example systemd unit: unprivileged user,
  `CAP_NET_BIND_SERVICE` for port 443, writes confined to the backup root.

### 7.6 Validation performed

- Expanded `server/selftest.py` (prefix + bare paths, nested config,
  firmwareConfig object, multipart out-of-order + retry, simple mode,
  suffix append, cross-media isolation, int64 uploadFinishTime, uploading
  list, double capacity, persisted completed_at/etag) — **passes**.
- TLS smoke test with hostname verification on the double-prefixed path —
  **passes**.
- Full HTTPS end-to-end test (`server/tls_e2e.py`): hostname verification,
  config/ping/derivatives/entrance/multipart PUT/report/uploaded-list and the
  `.md5` sidecar — **passes**.
- The CA now has critical `basicConstraints=CA:TRUE` and
  `keyUsage=keyCertSign,cRLSign`; without these, OpenSSL 3 / Python 3.14
  rejects the chain (curl was more lenient).
- CAPATH test: same CA fails without `<subject_hash>.0` and succeeds with it —
  confirms the `ca.hash` overlay is useful.
- `sh -n camera/bootup.sh`, `sh -n camera/make_certs.sh` — pass.

Remaining: on-device validation (no camera attached in this session), packet
capture to confirm the prefix/host actually used at runtime, and camera clock
sanity for TLS validity.

## Live state

- 2026-10-23: downloads complete; firmware rootfs extracted; SDK fully
  decompiled; protocol documented; instaAIP config source identified; SD-card
  boot hook found; server + camera hook implemented and self-tested.
- 2026-10-23 (later): parser conformance audit found the double
  `/cloud-service` prefix and four response-schema mismatches; all fixed.
  Ping (`msg == "Insta360 Cloud"`), instant finalisation + background `.md5`
  sidecar, streaming upload path, retry/crash recovery, DNS watchdog and
  CAPATH overlay added; expanded self test and TLS/CAPATH tests pass.
  **Blocked on physical camera for on-device validation.**
- 2026-10-23 (app-free trigger): recovered the RTOS->instaAIP SysV protocol
  (key 0x44455255, action at mtext[4]); actions 8/7/0 build and start the
  upload list without the app.  Added `camera/cloudkick` (444-byte AArch64
  ELF, no cross compiler), `cloud-upload-watch.sh`, `install_to_sd.sh`,
  `test_cloudkick.py` (emulates the ELF and checks actions/message layout),
  and `notes/ENABLE.md`.  Selftest + TLS e2e still pass; `sh -n` and the
  builder reproducibility check pass.  Hardware validation remains.

## 8. App-free enabling recovered (2026-10-23, later)

The blocker was assumed to be the app's binding/subscription check. It is not
needed on the camera side: the app's "enable backup" switch ends up as
SysV messages to `instaAIP`'s `cloud_msg` thread (8/7/0 for an incremental
scan-and-start). Recovered from
`recv_action_handler` (Ghidra 0x14d31c), `FUN_0014cf40`/`FUN_00145ec0`
(`msgsnd`) and `FUN_00152d8c` (media scan):

- queue key `0x44455255`, flags `IPC_CREAT|0666` (`0x3b6`);
- `mtype = 1`, message text length `0x408`, action at `mtext[4]`;
- action **8** = query uploaded/uploading lists and reconcile local SQLite
  records (optional; makes repeat runs incremental);
- action **7** = `ins_cloud_sdk_file_group_cfg` -> `FUN_00152d8c(0)` scans
  `/tmp/SD0/DCIM`, groups media with `ins::getGroupMedia`, and fills the
  `InsCloudFileInfoList` vector at `0x694d70`;
- action **0** = `ins_cloud_sdk_upload_start` -> tunes TCP, initialises the
  SDK and calls `setUploadFileInfoList(&0x694d70)` + `upload()`.

There is **no binding/entitlement check in this path**. The hardcoded test JWT
and base URL are used; our DNS/CA redirect already handles the endpoint and the
mock server ignores auth.

Implemented:

- `camera/cloudkick_build.py` builds a 444-byte freestanding AArch64 ELF
  (`camera/cloudkick`) with no cross compiler; raw syscalls `msgget` 186,
  `msgsnd` 189, `exit` 93.  It sends the digits in `argv[1]`, so `cloudkick 870`
  is uploaded-list sync + scan + start.
- `camera/cloud-upload-watch.sh` waits for `pidof instaAIP` and a default
  route, copies the helper to `/tmp` (works on `noexec` cards), then sends
  `870` every `CLOUD_UPLOAD_INTERVAL` seconds (default 1 h), skipping a cycle
  while the default interface's byte counters show upload traffic; action 15
  (`CLOUD_UPLOAD_SAVE_DB=1`) saves the SQLite records to FL0 when idle.
- `camera/bootup.sh` starts the watcher when `START_CLOUD_UPLOAD=1`.
- `cloud-backup.conf.example`, `README.md` updated; `sh -n` passes.

This bypasses the app entirely (no account, binding, or paid-plan check).
Remaining validation is still hardware: confirm the actions appear as
`[ins_cloud] recv upload action:7/0` in the camera log and that requests reach
`inscloud_server.py`.

## 9. Hotel / hotspot backup over Tailscale (2026-10-24)

New requirement: the camera should back up to the home server while it is on a
phone hotspot at a hotel, with no inbound ports at home.  The original LAN
redirect (DNS -> `SERVER_IP`) cannot do that.

### Options considered

| Option | Why not |
|---|---|
| Port-forward 443 + DDNS | Not always possible (CGNAT), public exposure, cert/SNI work. |
| Tailscale Funnel | Funnel terminates TLS with its own certificate, so the camera's hostname check fails. |
| Cloudflare Tunnel / public VPS | Same TLS-termination problem or extra moving parts. |
| **Camera joins the tailnet** | Works from any outbound network, end-to-end TLS, no public exposure. Chosen. |

### Why a relay is needed

- The camera kernel has no `/dev/net/tun`, so tailscaled must run with
  `--tun=userspace-networking`.
- In that mode apps do not get a route to `100.64.0.0/10`; tailscaled offers a
  local SOCKS5/HTTP proxy instead.
- instaAIP's libcurl does **not** read `http_proxy`, and its `ClientConfig`
  proxy string is empty in stock firmware, so we cannot point the SDK at the
  proxy.
- Therefore DNS answers `127.0.0.1` and `tailrelay` listens on `127.0.0.1:443`.
  It opens an HTTP CONNECT through tailscaled's outbound proxy to
  `TAILNET_TARGET:443` and splices bytes.  TLS is end-to-end; the certificate
  still matches `service-test.insta360.com`.

### Implementation

- `camera/tailrelay.c`: freestanding AArch64/x86_64 C, raw syscalls, no libc.
  Modes: listener (`LISTEN_PORT TARGET_HOST TARGET_PORT [PROXY_HOST]
  [PROXY_PORT] [LISTEN_ADDR]`) and `--probe` for health checks.  One forked
  child per connection; poll-based bidirectional splice; half-close handling;
  `MSG_NOSIGNAL`.
- `camera/build_tailrelay.sh`: cross-links with Zig (installed from the
  `ziglang` pip wheel into `vendor/pyzig`), because the host has no LLD and
  BFD cannot emit AArch64 ELF.  Produces `tailrelay` (AArch64) and
  `tailrelay-host` (x86_64 test build).
- `camera/test_tailrelay.py`: TLS client with real hostname verification ->
  relay -> mock CONNECT proxy -> `inscloud_server.py`; covers a single request,
  four concurrent requests, `--probe`, and a refused CONNECT.  All pass.
- `camera/tailscale-up.sh`: waits for a default route, starts
  `tailscaled --tun=userspace-networking
  --outbound-http-proxy-listen=127.0.0.1:8080 --state=<SD>/tailscale/tailscaled.state
  --socket=/tmp/tailscaled.sock`, logs in with `--auth-key`, `--accept-dns=false`,
  `--accept-routes=false`, and logs the tailnet IPv4.  State persists on SD.
- `camera/bootup.sh`: new `BACKUP_MODE=lan|tailscale`; in tailscale mode DNS
  answers `127.0.0.1`, starts tailscale-up and tailrelay, and runs a watchdog
  that restarts tailscaled/tailrelay.  LAN behaviour is unchanged.
- `camera/cloud-upload-watch.sh`: in tailscale mode waits for `tailrelay
  --probe TAILNET_TARGET 443 ...` before the first scan and before every
  cycle, so a dead tunnel does not start a failing batch.
- `camera/install_to_sd.sh`: `--mode lan|tailscale --target IP` plus either
  `--authkey KEY` or `--authkey-file FILE` (preferred: no secret in argv);
  copies `tailscale/{tailscaled,tailscale}` and writes the config.  In
  tailscale mode it also sets `RESTART_AIP=1`: `instaAIP` starts before
  `bootup.sh`, so restarting it after the DNS/CA setup discards any real-IP DNS
  answer cached during that early startup.
- `camera/diagnose.sh` (`CLOUD_DIAG=1`): writes `diag.log` with routes, resolver,
  mounts, the SysV queue, listeners, Tailscale status, a real relay probe, DCIM
  count and log tails.  This is the first-line hardware debug path.
- `camera/verify_sd.sh` (`verify_sd.sh <mountpoint>`): read-only PC-side
  pre-flight check for files, ARM64 binaries, mode/config values, the
  100.64/10 target and CA extensions/dates.
- `camera/make_certs.sh`: uses OpenSSL's `-not_before`/`-not_after` (when
  available) to backdate the CA and server certificate to 2020-01-01..2035.
  The camera clock can be behind after a long power-off; a "starts now"
  certificate would then fail.  Existing certs were regenerated.
- Server/API change: none.  Keep `--public-base` at
  `https://service-test.insta360.com` so upload URLs also resolve to 127.0.0.1
  and use the relay.

### Validation performed

- `server/selftest.py`: pass.
- `server/tls_e2e.py`: pass (hostname verified, upload, etag + md5).
- `camera/test_cloudkick.py`: pass.
- `camera/test_tailrelay.py`: pass (single, 4 concurrent, probe, refusal).
- `sh -n` on all camera scripts: pass.
- `readelf`: AArch64 `tailrelay` is `EXEC`, static, no `INTERP`, no dynamic
  section; `RX` text, non-exec stack.
- `camera/check_tailrelay_asm.py` (Capstone): entry stub is `mov x0, sp` ->
  `bl start_c`; all 56 `svc #0` sites have the expected `x8/w8` numbers
  (7, 57, 63, 64, 94, 101, 198, 200-203, 206-208, 210, 220, 260).  This caught
  one real bug: `nanosleep` is 101 on AArch64, not 35.
- `camera/test_tailscaled_proxy.py`: runs the x86_64 Tailscale 1.104.2 build on
  this host with the exact `tailscaled`/`tailscale up` flags.  It starts
  without TUN, the outbound proxy binds and answers CONNECT (HTTP 500 while
  unauthenticated), and `--accept-dns=false --accept-routes=false
  --netfilter-mode=off` are accepted.
- Installer dry run into `/tmp/sdtest2` in tailscale mode: correct file set
  (`diagnose.sh`, `tailrelay`, `tailscale/`), `RESTART_AIP=1`, config values.
- Installer dry run into `/tmp/sdtest` in tailscale mode: correct file set and
  config.

### Remaining / risks (hardware)

- Nothing has run on the camera yet.  Highest-risk items: tailscaled Go binary
  starting on the Ambarella kernel 5.10.61 (no TUN; userspace should work),
  RAM headroom, and whether port 443 on `127.0.0.1` is free.
- Verify `tailscaled --tun=userspace-networking` accepts the exact flags; the
  flags were confirmed from strings but not executed on-device.
- The camera clock must be valid for the server certificate and for TLS to
  Tailscale's control plane; the RTOS normally sets it.
- The camera must reconnect to the phone hotspot automatically; configure it
  once in the camera UI/app.
- Upload URLs must keep the redirected hostname.  A custom `--public-base`
  with an IP or port would bypass the relay.

## 10. X4 1.9.21 validation + execution path (2026-10-24)

### Image

- Official X4 firmware v1.9.21:
  `vendor/firmware/Insta360X4FW_1.9.21.bin`
- size 218,940,635 bytes, MD5 `e9754e9f47e71352e9522e282e3ce436`
- downloaded from
  `https://file.insta360.com/static/b369b4c42002f8d969c2892bd6d4f0a2/Insta360X4FW.bin`
- rootfs is UBI/UBIFS starting at file offset `0x98f8220`
  (222 PEBs x `0x40000`, volume `linux`), not squashfs.
- extraction:
  ```sh
  python3 -c "f=open('vendor/firmware/Insta360X4FW_1.9.21.bin','rb');f.seek(0x98f8220);open('/tmp/x4-ubi.img','wb').write(f.read(222*0x40000))"
  ubireader_extract_files -o /tmp/x4-root /tmp/x4-ubi.img
  # rootfs: /tmp/x4-root/1697898654/linux
  ```

Kernel cmdline in the image: `ubi.mtd=5 root=ubi0:linux rootfstype=ubifs
console=ttyS0 nosmp maxcpus=0` (no `ro`).  Rootfs is therefore writable by
default, which the X4 bootstrap relies on.

### Same uploader protocol

- `usr/bin/instaAIP`, SDK `usr/lib/libinsCloudSdk.so` (lowercase i).
- SysV queue key `0x44455255` at file offset `0x546a8`;
  `msgget(key, 0x3b6)` at `0x3aea0`; message size `0x408` at
  `0x3aed0`/`0x3af00`; action dispatch at `0x54734`:
  0 -> `0x567c0`, 6 -> `0x57040`, 7 -> `0x59360`, 8 -> `0x59500`.
- Action 0 reaches `setUploadFileInfoList` + `upload()` with no binding check.
  Action 8 does `init` + `setUploadFileInfoList` + `checkFile` with no binding
  check.
- X4 SDK base string is `https://cloud.insta360.com/` and the SDK paths are
  `/cloud/service/camera/...`.  The server accepts the bare `/cloud/service`
  form.  dnsmasq and the certificate already cover `cloud.insta360.com` and
  `service-test.insta360.com`.

### The X4 SD hook is disabled

`etc/init.d/S99bootdone` starts `instaAIP` unconditionally, but lines 45-58
(the `/tmp/SD0/bootup.sh` and `/mnt/SCAM/cv1_auto_run.sh` blocks) are
commented out.  `tcp_script.sh` (`/tmp/SD0/MISC/tcp_tuner.sh`) has no caller.
No other rootfs binary consumes an SD script.  So X4 needs a different
execution path.

### X4 web UI gives a root-file write

`instaAIP` starts an evhtp HTTP server unconditionally in `FUN_0010c160` ->
`FUN_0010c5c0` -> `FUN_00110d24`; `FUN_0010cd0c` binds `0.0.0.0:80`
(`DAT_004512dc = 0x50`).  Recovered routes and handlers:

| route | handler | notes |
|---|---|---|
| `/upload_file` | GET `0x10e2e0`, POST `0x10d62c` | writes any filename under `/tmp/SD0/DCIM/` |
| `/upload_fw` | GET `0x10e444`, POST `0x10d62c` | firmware upload, then update |
| `/algorithm/...` | `0x10f030` | reads `/tmp/SD0` + URI suffix |
| `/CONF` | `0x10eb40` | reads `/pref/camera_params.json` |
| `/LOG`, `/DCIM` | `0x10fe80`, `0x1105a0` | log/media transfer |

POST path (`FUN_0010d62c`, upload_file branch):
`strstr(body + 8, "filename")` -> quoted filename -> `sprintf("%s%s",
"/tmp/SD0/DCIM/", filename)` -> `fopen(..., "wb")`.  No `..` filtering, no
authentication; mode is gated only when factory mode is 2 and Wi-Fi is STA
(`FUN_001159a0` returns 1), so perform the one-time install on the camera's
own AP.  The success response is HTTP 400 with body `1002`.

`/algorithm/...` concatenates the suffix after `/algorithm` to `/tmp/SD0`,
so `/algorithm/DCIM/<name>` reads back an upload and
`/algorithm/../../usr/lib/...` / `/algorithm/../../etc/...` read rootfs files
(best-effort; evhtp may normalise in some builds).

### Bootstrap implementation

- `camera/x4preload.c` -> `camera/libcloudbootstrap.so`: freestanding AArch64
  shared object, no libc, no NEEDED entries.  Constructor:
  - accesses `/tmp/SD0/bootup.sh`; returns if missing;
  - creates `/tmp/.cloudbootstrap.started` (O_EXCL) once;
  - raw `clone(SIGCHLD)` then `execve("/bin/sh", ["/bin/sh","-c",
    "wait for pidof instaAIP (max 300s); exec /bin/sh /tmp/SD0/bootup.sh"])`.
  - If exec fails, removes the lock.
- `camera/x4_bootstrap.py`: GETs `/upload_file`, POSTs a probe to
  `/tmp/SD0/DCIM/.x4-bootstrap-probe`, verifies it with `/algorithm`, then
  POSTs `../../../usr/lib/libcloudbootstrap.so` and
  `../../../etc/ld.so.preload` (path plus newlines padded to the handler's
  `0x400` minimum).  `--disable` blanks the preload file.
- `camera/build_x4preload.sh` builds the AArch64 library and an x86_64 test
  variant.
- `camera/test_x4preload.py`: host checks no-op without the script, launch via
  `/bin/sh` with a 0644 script, one-shot lock, and lock removal on failure.
  Pass.
- `camera/test_x4_bootstrap.py`: mock evhtp handler with the recovered
  traversal semantics; verifies probe, verbatim library, padded preload,
  read-back, and `--disable`.  Pass.
- `camera/install_to_sd.sh --x4`: same SD kit as X5; prints the extra
  bootstrap step.  `RESTART_AIP=1` is set in tailscale mode, so `bootup.sh`
  restarts `instaAIP` after DNS/CA are in place.

### Risks / open items

- The preload hook is a **firmware modification** (two files on rootfs), unlike
  the X5 SD-only flow.  glibc's loader prints "cannot be preloaded: ignored"
  for a missing/bad preload object, and the constructor is a no-op without the
  SD card, but this has not been tested on hardware.
- Rootfs is assumed writable from the kernel cmdline and UBIFS defaults; must
  be confirmed on a real X4.  If `/upload_file` succeeds but `/algorithm` cannot
  read the library back, the uploader refuses to install the preload unless
  `--force` is used.
- Hardware validation still pending for X4: `/upload_file` reachability from
  the camera AP, traversal write to `/usr/lib`+`/etc`, a reboot with the
  preload, `bootup.log`/`tailscaled`/`tailrelay`, and server requests.
- X4 `/upload_fw` was not used.  Do not confuse it with the bootstrap; it
  triggers the official firmware update path and may flash.

### X4 install sequence (concrete)

1. `camera/install_to_sd.sh <sd> - --mode tailscale --target <tailnet-ip>
   --authkey <key> --x4` (also creates `<sd>/DCIM` for the upload probe and
   `<sd>/NO_HIBER`, which `ambernation.sh` honours).
2. Insert the card, power the camera, join its AP (`192.168.42.1`, confirmed in
   `ap_start_fast.sh` / `change_mode.sh`).
3. `python3 camera/x4_bootstrap.py --camera 192.168.42.1`.
4. Power-cycle.  The preload constructor waits for `instaAIP`, runs
   `/tmp/SD0/bootup.sh` via `/bin/sh`, and the existing tailscale/relay/upload
   chain takes over.

## 11. X4 Air 1.2.7 validation — SD-only path (2026-10-24)

### Image and rootfs

- Official X4 Air firmware 1.2.7: `Insta360X4AirFW_1.2.7.bin`,
  309,943,867 bytes, MD5 `53a8d0ebb937a41ef0edc92bdf556f40`.
- Found via the product slug `insta360-x4air` (the page URL is
  `/download/insta360-x4air`; `insta360-x4-air` does not populate firmware
  data).  Manifest URL:
  `https://wassets.insta360.com/common/763cc41c8333401ab3d5d2b22dc3aea4/Insta360X4AirFW.bin`
- Container has a fixed-size partition table at `0xd8` (32-byte name +
  8-byte size per slot): `BOOTSTRAP`, `BOOTLOADER`, `FW_UPDATER`,
  `ARM_TRUST_FW`, `SYS_SW`, `SECURE`, `SYS_DATA`, `LINUX_Kernel`,
  `LINUX_RFS`, `LINUX_HIBER_IMG`, `CALIB`, `USER_SETTING`, `DRIVE_A`,
  `DRIVE_B`, `XTB`, `USER_DEBUG`, `SYS_SETTING`.
- Rootfs `LINUX_RFS` is UBI/UBIFS at file offset **`0xe1caa87`**, 278 PEBs of
  `0x40000` bytes, volume `linux`.  The separate squashfs at `0x41888d3` is
  not the Linux rootfs.
- Kernel is **5.10.61** (AArch64).
- Extraction:
  ```sh
  python3 -c "f=open('vendor/firmware/x4-air/Insta360X4AirFW_1.2.7.bin','rb');f.seek(0xe1caa87);open('/tmp/x4air-ubi.img','wb').write(f.read(278*0x40000))"
  ubireader_extract_files -o /tmp/x4air-root /tmp/x4air-ubi.img
  # rootfs: /tmp/x4air-root/<volume-id>/linux
  ```

### The X4 Air SD hook is enabled

`etc/init.d/S99bootdone` (4502 bytes) starts `instaAIP`, then unconditionally
runs:

```sh
if [ -x /tmp/SD0/bootup.sh ]; then
    LOG "inject SD0 boot.sh"
    /tmp/SD0/bootup.sh
else
    if [ -x /tmp/SD1/bootup.sh ]; then ... fi
fi
```

It also prefers `/tmp/SD0/instaAIP` over `/usr/bin/instaAIP` (unused by this
kit).  `ambernation.sh` honours `/tmp/SD0/NO_HIBER`.  There is no `/etc/ld.so.preload`
and no firmware modification needed.

### Same uploader, X4 Air specifics

- Files: `usr/bin/instaAIP` (5,763,208 B) and
  `usr/lib/libInsCloudSdk.so` (3,794,832 B).
- `instaAIP` initializer (`Ghidra 0x10b488`) sets:
  - base `https://service-test.insta360.com/cloud-service`
  - ping `https://cloud.insta360.com/actuator/ping`
  - model `Insta360 X4 Air`, user agent `insta360/x4 air`
  - SDK/version `0.5.3`
  - DNS server `8.8.8.8`
  - a hardcoded test JWT (ignored by the local server)
- Effective API path: `/cloud-service/cloud/service/camera/...`; the server
  accepts it.  All required endpoints are present in the SDK, including
  `userInfo/binding/status`.
- `cloud_rx` at Ghidra `0x14c360` disassembles to
  `mov w0,#0x5255; movk w0,#0x4445,lsl#16; bl 0x44ee0` -> queue key
  `0x44455255`; `msgget` mode `0x3b6`; `msgsnd/msgrcv` size `0x408`;
  `mtype=1`, `mtext[0..3]=1`, action at `mtext[4]`.
- Action switch: 0 start, 1 stop, 2 pause, 3 restart, 5/6 capacity, 7 scan,
  8 query uploaded list, 9/10 state, 11 clear, 12 send list, 15 save DB.
  Action 0 goes directly to `ins_cloud_sdk_upload_start` -> `upload()`; action
  7 and 8 do not check binding.  There is no entitlement gate in this path.
- The stock `/usr/local/share/script/cloud_tcp_tune.sh` and
  `cloud_tcp_restore.sh` exist, so action 0's TCP tuning is a no-op success.

### Implementation / deployment

- `camera/install_to_sd.sh --x4-air` copies the existing kit, creates
  `NO_HIBER`, sets `RESTART_AIP=1` (because `instaAIP` starts before
  `bootup.sh`), and prints X4 Air instructions.  It does not run
  `x4_bootstrap.py`.
- Tailscale 1.104.2 ARM64 binaries fetched to
  `vendor/tailscale/tailscale_1.104.2_arm64/` and copied to the card by the
  installer.  `test_tailscaled_proxy.py` accepts the
  `--tun=userspace-networking --outbound-http-proxy-listen` flags.
- `tools/verify_x4air_fw.py` performs the MD5/UBI checks, SD-hook checks,
  endpoint strings and queue disassembly.  It passes against 1.2.7.
- New guide: `notes/X4-AIR-QUICKSTART.md`.

### Validation performed

- `tools/verify_x4air_fw.py` -> `RESULT: X4 Air 1.2.7 is compatible`
  (MD5, UBI, SD hook, queue key/mode/size/action offset, dispatch targets
  for actions 0/7/8/15, SDK endpoints).
- `server/selftest.py` -> `ALL TESTS PASSED`.
- `server/tls_e2e.py` -> `TLS end-to-end OK`.
- `camera/test_cloudkick.py` -> `cloudkick emulation OK ... size=0x408`.
- `camera/test_tailrelay.py`, `test_tailscaled_proxy.py`, `test_x4preload.py`,
  `test_x4_bootstrap.py`, `check_tailrelay_asm.py` -> pass.
- `camera/install_to_sd.sh --x4-air` LAN and Tailscale installs into temp
  directories -> `verify_sd.sh` reports `RESULT: ready (0 warnings)`.
- `camera/test_install_x4air.py` -> `PASS: X4 Air SD install`.
- `sh -n` on all shell scripts -> pass.

### Hardware checklist (X4 Air)

1. Card prepared with `--x4-air`, hotspot saved in the app, card inserted.
2. Power on; read `/tmp/SD0/bootup.log` for `sending actions 870` and
   `tailnet path ready`.
3. `ipcs -q` shows key `0x44455255`; `logread` shows
   `[ins_cloud] recv upload action:8/7/0`.
4. Server `requests.log` shows `upload/config`, `derivatives`,
   `item/entrance`, PUTs, `item/status/report`; files appear under
   `<root>/<localMediaId>/`.
5. If not, set `CLOUD_DIAG=1`, reboot, read `diag.log`.

### Re-validation and USB-transfer note (2026-10-24)

- Re-ran `tools/verify_x4air_fw.py` against the same 1.2.7 image: all checks
  pass (MD5/UBI, `S99bootdone` SD hook, endpoints, queue key/mode/size/action
  offset, action 0/7/8/15 dispatch).
- Re-ran `camera/test_install_x4air.py`: `PASS: X4 Air SD install`; `sh -n`
  passes for `bootup.sh`, `install_to_sd.sh`, `verify_sd.sh`,
  `cloud-upload-watch.sh`, `tailscale-up.sh`, `diagnose.sh`.
- USB question: do **not** depend on camera USB mode.  The deterministic path
  is power off -> remove microSD -> PC card reader.  A firmware string for
  `Usb_Storage` exists in the X4 Air image, but the on-screen label is not
  verified.  Camera USB mass-storage can also drop Wi-Fi, so it must not be
  used during backup tests.
- The quick-start now documents this, plus the SD-only rollback and risk
  model (no rootfs/bootloader/kernel writes; remove the card to revert).

### Open risks

- Hardware validation is still pending.  Static firmware evidence is strong,
  but `/tmp/SD0` execution, tailscaled memory/CPU, and actual upload behaviour
  must be confirmed on the physical X4 Air.
- The camera must have the hotspot saved.  The paid app toggle is not needed,
  but the normal app is still the easiest way to save the network.
- Use a wall charger or power bank, not a data-capable computer USB port; the
  latter can put the camera into USB-storage mode and take Wi-Fi down.
- Tailscale auth key/state are on the SD card; treat the card as a secret.

## 12. Live deployment — home-server Unraid (2026-10-24)

Target selected by the user: `home-server`, Tailscale IPv4
`100.101.102.103`.

### Server

- Unraid's bundled Python is 3.9 and cannot import `_ssl`
  (`libssl.so.1.1` missing), so the server runs in Docker instead of directly
  on the host.
- Container: `insta360-cloud`, image `python:3.12-slim`, user `99:100`,
  `--restart unless-stopped`.
- Port: **8843**.  Unraid nginx already owns 443 (and 8443 was used for the
  first test), so the relay's upstream port is 8843.  The camera still calls
  its own `127.0.0.1:443`; `tailrelay` connects to
  `100.101.102.103:8843`.  `SERVER_PORT=8843` in the card config.
- Persistent files:
  - server + certs: `/mnt/user/appdata/insta360-backup`
  - media/state: `/mnt/user/MyShare/insta360-backup` (a normal Unraid share
    directory; Syncthing is not part of this setup)
- Container command:
  ```sh
  docker run -d --name insta360-cloud --restart unless-stopped \
    --user 99:100 -e PYTHONUNBUFFERED=1 -p 8843:8843 \
    -v /mnt/user/appdata/insta360-backup:/app:ro \
    -v /mnt/user/MyShare/insta360-backup:/data \
    python:3.12-slim python3 /app/inscloud_server.py \
      --bind 0.0.0.0 --port 8843 --root /data \
      --cert /app/certs/server.crt --key /app/certs/server.key
  ```
- Verified from the Fedora host over the tailnet:
  ```sh
  curl --cacert camera/certs/ca.crt \
    --resolve service-test.insta360.com:8843:100.101.102.103 \
    https://service-test.insta360.com:8843/actuator/ping
  # HTTP/1.1 200 OK
  # {"code":0,"msg":"Insta360 Cloud"}
  ```

### SD card

Mounted at `/run/media/wkipling/disk` (exFAT, 239 GB, existing DCIM/MISC
preserved).  Installed with:

```sh
cd research/cloud-backup/camera
./install_to_sd.sh /run/media/wkipling/disk - \
  --mode tailscale --target 100.101.102.103 --x4-air
```

Then edited `cloud-backup.conf`:

```sh
SERVER_PORT=8843
CLOUD_DIAG=1
```

Also present: `NO_HIBER`, `tailscale/{tailscaled,tailscale}`, `RESTART_AIP=1`,
`START_CLOUD_UPLOAD=1`, `TAILNET_TARGET=100.101.102.103`.

**Auth key installed:** a reusable, non-ephemeral Tailscale auth key was
inserted into `/run/media/wkipling/disk/cloud-backup.conf` (value not logged).
`verify_sd.sh` now reports:

```
RESULT: ready (1 warning(s))
```

The warning is expected: `SERVER_PORT=8843` is the relay's upstream port; the
camera still calls `127.0.0.1:443` locally.  Card was synced and ejected.

Next: cold-boot the X4 Air with the card, ensure the phone hotspot is saved,
and watch `bootup.log` / the Unraid container logs.

## 13. Card-side Wi-Fi station mode (2026-10-24)

The app's Wi-Fi screen is not required.  The X4 Air rootfs contains the stock
station-mode scripts:

- `/usr/local/share/script/change_mode.sh` reads `/pref/wifi.conf`, kills
  hostapd/dnsmasq/wpa_supplicant, scans, joins the saved SSID, and runs
  `udhcpc` on `wlan0`.
- `/usr/local/share/script/sta_start_fast.sh` + `sta_connect.sh` are a
  fallback path.
- `/pref` is a tmpfs bind mount (`S51pref`), so this is still runtime-only and
  does not write the camera's persistent rootfs.

Implementation:

- New `camera/wifi-connect.sh`: reads `wifi-sta.conf` as raw data (never
  sourced), rejects newlines/quotes/backslashes, writes `/tmp/wifi.conf`,
  invokes `change_mode.sh`, and waits for a default route.  Stock script
  output goes to `/tmp/inscloud-wifi.log`, not the SD card, because it prints
  the PSK.
- `camera/bootup.sh`: if `wifi-sta.conf` has a non-empty `WIFI_SSID`, runs
  `wifi-connect.sh` before dnsmasq/tailscale setup.  The camera's AP is gone
  during station mode, so the app cannot connect at the same time.
- `camera/install_to_sd.sh`: `--wifi-ssid`, `--wifi-password`,
  `--wifi-country`, `--no-wifi`; writes `wifi-sta.conf` (raw lines, not shell
  code).
- `camera/verify_sd.sh`: validates `wifi-connect.sh`, SSID/password presence,
  and warns about characters the stock parser cannot handle.
- `camera/diagnose.sh`: logs redacted Wi-Fi config, `iw link`, `wpa_cli
  status`, and a redacted tail of the stock station-mode log.
- `notes/X4-AIR-QUICKSTART.md`: documents card-side station mode plus the app
  fallback labels (`Wi-Fi Settings` / `Auto Backup Wi-Fi settings`).

Validation performed (no hardware):

- `test_install_x4air.py` now installs with `--wifi-ssid`/`--wifi-password`,
  checks `wifi-sta.conf`, and verifies the card -> `PASS`.
- `wifi-connect.sh` smoke test with a mock `change_mode.sh`: writes the
  expected `WIFI_MODE=sta` config, passes the country argument, exits 0, and
  does not put the password in `bootup.log`.
- Quote/backslash rejection test -> exit 1.
- `sh -n` for all modified shell scripts -> pass.

**Hardware boot observed (2026-10-24, X4 Air 1.2.7, no station config):**

- The card hook runs: `S99bootdone` executed `/tmp/SD0/bootup.sh`, claimed
  `8.8.8.8/32` on `lo`, mounted the CA overlay, started `tailrelay`, restarted
  `instaAIP`, and started the cloud watcher.
- The camera was still in AP mode (`wlan1 = 192.168.42.1`), as expected with
  no `wifi-sta.conf`.  There was no default route, so `tailscaled` correctly
  did not start and the uploader did not run.
- This exposed a real bug: the stock AP startup begins its own dnsmasq after
  `bootup.sh` starts, and it also owns UDP 53.  The old code tried to bind
  `8.8.8.8:53` immediately and entered an endless
  `Address already in use` watchdog loop.  The redirect was not reliable.

**Fix (same date):**

- `bootup.sh` no longer starts the private dnsmasq in AP mode.  It waits for a
  default route (station mode), then binds `8.8.8.8:53`.  The AP's DHCP/DNS
  server is left untouched until station mode replaces it.
- The private dnsmasq now runs with `--keep-in-foreground`; `$!` is the real
  PID, avoiding the old daemon-PID mismatch that spawned duplicate instances.
- A canary name (`inscloud-probe.insta360.com`, never in `/etc/hosts`) is
  resolved through `8.8.8.8` to verify the whole path, not just process
  liveness.  AAAA is made NODATA with `--local` so c-ares does not get a
  REFUSED response.
- If a lingering stock dnsmasq holds port 53 after station mode is up, the
  watchdog takes the port over once, then retries.
- `wifi-connect.sh` now falls back to `sta_start_fast.sh` + `sta_connect.sh`
  when `change_mode.sh` does not produce a default route.  The fallback path
  supports WPA3/SAE-only hotspots, which `change_mode.sh` does not.
- `wifi-connect.sh` removes the old AP address `192.168.42.1/24` (and its
  connected route) from `wlan0`/`wlan1` after switching.  This matters because
  the home LAN here is also `192.168.42.0/24`: leaving the AP address behind
  would send all home-router/server traffic out the dead AP interface.
- If the join still fails after 90 s, `wifi-connect.sh` restores the original
  AP profile saved from `/pref/wifi.conf` and re-runs the stock AP path.  The
  camera should remain reachable instead of becoming a station-mode brick.
- Diagnostics now record UDP listeners, the DNS canary, and the private
  dnsmasq log.

**First hardware validation prepared (2026-10-24):**

- The card was installed with the saved home Wi-Fi profile `HomeWiFi`
  (`wpa-psk`, 2.4/5 GHz) for an end-to-end test.  The password was read from
  NetworkManager and written directly to `wifi-sta.conf`; it was never
  printed.  `verify_sd.sh` reports `ready (1 warning)`, the warning being the
  expected `SERVER_PORT=8843` note.
- Offline tests re-run after the changes: `test_install_x4air.py`,
  `test_wifi_connect.py`, `test_cloudkick.py`, `test_tailrelay.py`, firmware
  verifier, `sh -n` and `py_compile` all pass.

**Second hardware boot (2026-10-24 10:54):** the card hook ran and attempted
station mode, but the join failed.  The logs show:

- `change_mode.sh` returned 0 but never got a route.  It took the SSID scan
  path and found nothing.
- `sta_connect.sh` then returned 11 (`can't find the SSID`), and its log was
  full of `Failed to connect to non-global ctrl_ifname: wlan0`.
- The AP interface `wlan1` still had `192.168.42.1/24` and was `UP` at the end.

Root cause: on this Broadcom driver `mchan_cap=0`, so `wlan0` cannot scan
while the AP on `wlan1` is holding the radio.  The stock `change_mode.sh`
kills hostapd but never brings `wlan1` down; only `ap_stop.sh` does
`ifconfig wlan1 down`.  The scan therefore returned an empty list.  The
fail-safe AP restore also did not run because `/pref/wifi.conf` was not
present yet when `wifi-connect.sh` saved it (S99 raced the AP startup).

Fixes:

- `wifi-connect.sh` waits up to 25 s for the stock AP to appear, then saves
  both `/pref/wifi.conf` and `/tmp/hostapd.conf`.
- It brings `wlan1` down before any scan, matching `ap_stop.sh`.
- It uses `sta_start_fast.sh` + `sta_connect.sh` first (bounded scans, WPA2
  and WPA3/SAE); `change_mode.sh` is the fallback and now runs with a 75 s
  hard timeout because its `wpa_cli -A` scan can block forever.
- If both fail, it restores the AP from `/pref/wifi.conf`, or from the saved
  `hostapd.conf` if the high-level profile was never written.
- Diagnostics now save the complete redacted station-mode log to
  `wifi-diag.log`, list visible SSIDs, and show hostapd/wpa_supplicant state.

**Third hardware boot (2026-10-24 11:05):** station association worked.  The
camera joined `HomeWiFi`, obtained `192.168.1.81`, and was visible in the
host ARP table.  But it had no default route, so Tailscale never started and
the AP fail-safe restored the hotspot after 90 s.

Cause: `udhcpc` ran while `wlan1` still held `192.168.42.1/24`.  Downing the
interface did not remove the address on this build.  The joined home LAN is
also `192.168.42.0/24`, so the AP's local address/connected route kept the
router unreachable and no default route was installed.  The cleanup ran
*after* DHCP, too late.

Fix: `drop_ap_address` is now called immediately after bringing `wlan1` down
and **before** `sta_start_fast.sh` / `sta_connect.sh` (and therefore before
`udhcpc`).  It also removes any default route on the AP interface.  A second
cleanup pass remains after association.

**Next:** reinstall and boot.  Expected sequence:

```
wifi: brought wlan1 down before scanning
wifi: removed AP address 192.168.42.1/24 from wlan1
wifi: using sta_start_fast.sh + sta_connect.sh
wifi: sta_connect.sh rc=0
wifi: station mode ready (192.168.42.x/24); default route present
```

Then `tailscaled` should start, the `insta360-cam` node should appear in the
tailnet, and the server should receive `upload/config`.  After that works,
replace `wifi-sta.conf` with the phone hotspot profile for hotel use.
Recovery is unchanged: power off, remove the card, delete `wifi-sta.conf`,
boot again (camera returns to AP mode).

## 14. Direct SD-card uploader — bypassing instaAIP (2026-10-24)

### Why this path

`instaAIP` accepts the action-870 SysV messages (`cloudkick rc=0`) but never
opens a TCP connection to the relay.  Whatever gate it is stuck behind
(media DB, binding/token, RTOS state) is opaque and was costing hardware
round-trips.  The paid-cloud path is therefore **not required** for the
user's actual goal: get the files onto their own server.

The camera already has a working, encrypted path to the server: it joins
Wi-Fi in station mode, `tailscaled` is Running, and the node has a direct
WireGuard connection to the tailnet.  So we ship our own uploader and push
the media over that link.  No port forwarding, no Insta360 account, no TLS
certificate on the camera.

### Topology

```
camera /tmp/SD0/DCIM
   |  insupload (aarch64 static)
   |  HTTP/1.1 over 127.0.0.1:8080  (tailscaled HTTP CONNECT proxy)
   v
tailscaled userspace netstack  ==WireGuard==>  100.101.102.103
                                                   |
                                      insta360-sink :8844
                                                   |
                                  /mnt/user/MyShare/insta360-backup/sdcard
```

**Important:** `tailscaled` runs with `--tun=userspace-networking` (the
camera kernel has no `/dev/net/tun`).  Normal sockets on the camera cannot
route to `100.x` addresses.  `insupload` therefore speaks `CONNECT
host:port` to `127.0.0.1:8080` exactly like `tailrelay` does.  A direct
`connect()` would silently fail — this was the first design bug caught
before touching hardware.

### Protocol (plain HTTP; WireGuard provides the encryption)

| request | meaning |
| --- | --- |
| `GET /api/ping` | reachability/auth check |
| `GET /api/stat?path=REL` | `{"size":N,"final":bool}` — how much the server holds |
| `PUT /api/upload?path=REL` + `X-Offset`, `X-Total-Size` | append the next chunk; partial data is `REL.part` |
| `POST /api/reset?path=REL[&all=1]` | discard a partial (`all=1` also discards a complete-but-wrong file) |

Resume is server-driven: after a dropped hotspot the uploader asks for the
size and continues at that offset.  A complete file is skipped when the
sizes match; a same-name file with a different size is reset and re-sent.
1 MiB chunks, 45 s socket timeouts, 5 attempts per file per pass.

### Card-side behaviour

`bootup.sh` section 3b starts `insupload` (copied to `/tmp` to survive a
noexec mount) after Wi-Fi/Tailscale setup:

```
insupload --url http://100.101.102.103:8844 --root /tmp/SD0/DCIM \
          --proxy 127.0.0.1:8080 --token-file /tmp/SD0/sd-upload.token \
          --interval 300 --wait 600
```

* scans `/tmp/SD0/DCIM`, media extensions only;
* skips files modified in the last 30 s (a clip still being written);
* keeps running across passes, so clips recorded later are picked up
  without a reboot;
* logs to `/tmp/SD0/upload.log` (rotated at 1 MiB);
* `SD_UPLOAD_DELETE=0` by default — the card remains the source of truth.

### Server-side behaviour

`server/sdcard_receiver.py` runs in a second container on home-server:

```
docker run -d --name insta360-sink --restart unless-stopped \
  --user 99:100 -e PYTHONUNBUFFERED=1 -p 8844:8844 \
  -v /mnt/user/appdata/insta360-backup:/app:ro \
  -v /mnt/user/MyShare/insta360-backup/sdcard:/data \
  python:3.12-slim python3 /app/sdcard_receiver.py \
    --bind 0.0.0.0 --port 8844 --root /data \
    --token-file /app/sd-token
```

Token: 48 hex chars in `/mnt/user/appdata/insta360-backup/sd-token`
(mode 600, uid 99).  Copied to the card as `sd-upload.token` by the
installer (`--upload-token-file`), so it never appears in chat or argv.
Verified from the host over the tailnet:
`{"ok": true, "service": "insta360-sdcard-sink"}`.

### Offline test results

`camera/test_sdcard_push.py` (5 tests, all pass): fresh upload of a 3 MiB
file, media-extension filtering, resume from a 1 MiB `.part`, repair of a
stale complete copy, wrong-token rejection, and the **CONNECT proxy** path
with a stand-in tailscaled proxy.  `test_install_x4air.py` also checks that
`insupload`, `START_SD_UPLOAD=1`, `SD_UPLOAD_PORT=8844` and the token file
land on the card.

### Also fixed

`tailscale-up.sh` now reads `status --json` and refuses to run `tailscale up`
when `BackendState` is already `Running`.  The watchdog previously re-ran it
on a transient status failure and the CLI segfaulted (`rc=139`).

### Card state at handoff

Installed and verified `ready (2 warnings)`: the two warnings are expected
and intentional — `SERVER_PORT=8843` is the relay target port (the camera
API port stays 443), and `START_CLOUD_UPLOAD=0` because the direct uploader
replaces it.  `START_TELNETD=1` is temporarily on for the first live run.
`wifi-sta.conf` is the home Wi-Fi (`HomeWiFi`); it must be replaced with
the phone hotspot profile before the hotel trip.

### Next

1. Boot with the card; check `/tmp/SD0/upload.log` for
   `receiver reachable` and per-file progress, and `docker logs
   insta360-sink` for `PART/DONE` lines.
2. If the proxy rejects port 8844, fall back to a second `tailrelay`
   instance (`tailrelay 8844 <target> 8844 127.0.0.1 8080 127.0.0.1`)
   and point the uploader at `http://127.0.0.1:8844`.
3. Replace `wifi-sta.conf` with the hotspot; test one hotel-style boot.
4. Optional: auto-power-on when charging, if the camera offers it.

## 15. First real upload + Wi-Fi auto-off (2026-10-24)

### Result

The direct uploader works.  Over the home LAN fallback it uploaded
**389,021,696 bytes** of `Camera01/VID_20260810_163517_00_003.insv`
(796,840,319 bytes total) before the camera powered its Wi-Fi off.  The
receiver holds the partial file and the uploader resumed from offset
22,020,096 at the start of the run, so both resume paths are proven on
hardware.

`cardcheck.log` from the camera (written before Wi-Fi starts) now reports:

```
tmpfs 78.5M used 24.7M ... /tmp
OK   insupload
OK   tailscale/tailscaled
OK   tailscale/tailscale
c: on /tmp/SD0 type ambafs (rw,relatime)
```

The earlier hash mismatch was transient: the same files verify correctly on
the camera when read again.  The camera's filesystem is a custom `ambafs`
driver, not the host's exfat driver.

### Two bugs found and fixed

1. **`/tmp` was filled by the integrity copies.**  Copying `tailscaled` and
   the CLI (61 MB) into the 78.5 MB tmpfs left 0 bytes free, which also
   prevented `telnetd` from starting.  `tailscale-up.sh` now verifies the
   binaries **in place** and only copies to `/tmp` when the read is bad.
   `bootup.sh` does the same for `insupload`.
2. **The camera turns Wi-Fi off after ~2 minutes of inactivity**, even with
   `NO_HIBER`.  This is what ended the upload.  Two mitigations:
   * use a charger/power bank (the idle policy is battery-oriented), and set
     any "Auto Power Off"/Wi-Fi timeout to Never in the camera UI;
   * `wifi-watchdog.sh` now checks `wlan0` every 20 s and, after 3 failures,
     re-runs `wifi-connect.sh` in `WIFI_RECONNECT=1` mode (no AP wait, no AP
     fallback).  `sta_start.sh` restarts `wpa_supplicant` without reloading
     the driver, so this recovers a GUI-induced Wi-Fi shutdown.

### Tailscale, separately

Tailscale 1.92.3 starts and stays up:

```
100.101.102.104  insta360-cam  user@  linux
pong from home-server (100.101.102.103) via 192.168.1.10:38883 in 11ms
```

`/tmp` exhaustion and the Wi-Fi auto-off were the reasons it looked broken;
the daemon itself is healthy on this build.

### Server

`insta360-sink` on home-server, port 8844, writes to
`/mnt/user/MyShare/insta360-backup/sdcard/<Camera01>/...`.  Partial files are
`*.part`; a size-matched final file is skipped on later passes.

### Next

1. Boot on a charger with the watchdog card; confirm the camera rejoins
   after a Wi-Fi shutdown and that the 389 MB partial completes.
2. Replace `wifi-sta.conf` with the phone hotspot and test one hotel-style
   boot.  The uploader will use the tailnet endpoint when the LAN one is not
   reachable; both are tried every 10 s.
3. If the camera still sleeps Wi-Fi on external power, look for the
   `instaAIP` close timer (`InsApp_Wifi_Close` / `WIFI_CLOSE` strings) and
   consider a periodic synthetic input event.

## 16. Root cause of the stalls: Wi-Fi power save (2026-10-24)

### What was actually wrong

The camera was **not** turning Wi-Fi off by itself.  Three separate problems
combined to look like it:

1. **Broadcom power save was ON.**  `iw dev wlan0 get power_save` returned
   `Power save: on`.  Uploads stalled with 100+ KB sitting in the TCP send
   queue, host pings flapped, and connections reset mid-chunk.
   `iw dev wlan0 set power_save off` fixed it immediately: throughput became
   a steady ~4 MB/s and the link stopped flapping.  This is now done by
   `wifi-connect.sh` after a successful join and by the watchdog after every
   reconnect.
2. **The watchdog was cycling the interface on false positives.**  Its check
   used `ip link ... | grep 'state UP'`, but this Broadcom driver reports
   `state UNKNOWN` even when associated.  Every 60 s it ran
   `ifconfig wlan0 down/up`, killing the upload.  The diagnostic line proved
   the link was fine (`inet 192.168.1.81`, default route,
   `wpa_state=COMPLETED`) while the check failed.
   The check is now `iw dev wlan0 link | grep 'Connected to'` plus IP and
   route, and it no longer cycles the interface as an escalation.
3. **The receiver could deadlock on a half-open upload.**  A client that
   vanished mid-chunk left `do_PUT` blocked in `rfile.read()` while holding
   the per-path lock, so every retry queued behind it forever.
   `sdcard_receiver.py` now sets a 60 s socket timeout in `setup()`.

Also changed: `insupload` aborts a pass as soon as the receiver is lost and
rescans every 60 s (`SD_UPLOAD_INTERVAL=60`), instead of spending the retry
budget on every remaining file.

### Verified live (12:41-12:47)

* first clip `VID_20260810_163517_00_003.insv` complete: **796,840,319 bytes**
* second clip and two `.insp` photos complete
* over **2 GB** total on the server, continuing at ~4 MB/s
* uploads are flowing through the **tailnet endpoint** (`--proxy
  127.0.0.1:8080`), not just the LAN fallback, so the hotel path is proven
* watchdog running with no false `link check failed` lines
* `Power save: off`

### Card updated over the network

Instead of another card pull, `bootup.sh`, `wifi-connect.sh` and
`wifi-watchdog.sh` were served from home-server on port 9001 and fetched by the
camera with `wget`; SHA-256 matched the host.  The watchdog was restarted
with `setsid`.  The card now boots with power-save-off and the corrected
watchdog.

### Remaining

* Let the ~27 GB finish; sizes/one hash check afterwards.
* Replace `wifi-sta.conf` (currently `HomeWiFi`) with the phone hotspot
  before the trip.

## 17. Stock-AP fallback, onboarding TUI, live dashboard (2026-10-24)

Three follow-ups the user asked for after uploads were confirmed working.

### 17.1 Fall back to normal camera behaviour when the Wi-Fi is absent

Requirement: "go back to normal if it doesn't see my wifi in say 15 sec, with
all the default/usual wifi settings".

The stock `sta_connect.sh` already scans three times and exits **11** when
the saved SSID is not in range; that is the ~15 s detection budget.  We use
it instead of adding a second scan (the earlier `sta_scan.sh` probe was
removed; it doubled boot time and used a substring match).

`wifi-connect.sh`:

* on boot only (`WIFI_RECONNECT != 1`) and only when rc is 11, calls
  `restore_ap` and exits **3**;
* `restore_ap` copies the saved stock `/pref/wifi.conf` back with
  `WIFI_MODE=ap`, runs the stock `change_mode.sh` (now wrapped in a 30 s
  deadline), then **verifies** that `192.168.42.1` actually appeared on
  wlan0/wlan1 before declaring success.  If not, it falls back to the saved
  `hostapd.conf` + dnsmasq path;
* on success writes `/tmp/inscloud-ap-fallback` (overridable with
  `WIFI_AP_FALLBACK_FLAG`).

This restores the original hotspot SSID, password, address and DHCP range,
because they come from the saved stock profile, not from our minimal station
config.

`bootup.sh` now records the exit code: `0` = station, `3` = `ap_fallback`,
anything else = `degraded`.  In `ap_fallback` it does **not** start
tailscale, the relay, or the SD uploader, and it does not start the watchdog:
the camera behaves exactly like stock until the next boot.

`wifi-watchdog.sh` exits immediately when the flag exists, and also idles
(per iteration) while `hostapd` is running, so it never fights the user's
own AP mode.  Watchdog reconnects never use the AP fallback: a brief hotel
hotspot outage keeps retrying station mode instead of stranding the camera
in AP mode for the rest of the boot.

Tests: `test_wifi_connect.py` gained `missing-ssid-boot` (rc 3, stock AP
restored, flag written) and `missing-ssid-reconnect` (no AP restore), and
the generic-failure case now uses rc 1 so the `change_mode.sh` fallback is
still covered.

### 17.2 Onboarding TUI (`camera/setup_tui.py`)

Stdlib-only curses app for the computer with the card mounted:

* auto-detects the card from `/proc/self/mounts` (prefers a mount containing
  `DCIM` or `cloud-backup.conf`) and lets you pick another;
* lists Wi-Fi networks from `nmcli -t -f SSID,SIGNAL,SECURITY`, with rescan,
  manual SSID, and "keep the camera's own hotspot"; the password is read
  without echo and written to a mode-0600 temp file passed as
  `--wifi-conf` (never argv, never the summary, deleted in `finally`);
* asks for the server (tailnet IP auto-detected with `tailscale ip -4`, or a
  LAN IP), port, upload token (reuse/generate/choose/skip), and a Tailscale
  auth key when the card has no `tailscale/tailscaled.state`;
* probes `GET /api/status` and shows whether the receiver is reachable;
* shows a summary, then runs `install_to_sd.sh`, rewrites the runtime keys
  in `cloud-backup.conf` (an existing conf is otherwise kept by the
  installer), runs `verify_sd.sh`, and prints a checklist.

The interactive flow never passes secrets on a command line.  A
non-interactive form exists for scripts/tests, also file-based for secrets:

```
python3 camera/setup_tui.py --non-interactive --sd /run/media/$USER/disk \
    --wifi-conf /tmp/wifi.conf --mode tailscale --target 100.x.y.z \
    --token-file .sd-upload.token --authkey-file /tmp/tskey
```

Pure helpers are unit-tested in `camera/test_setup_tui.py` (23 tests):
argv construction, conf editing (including commented keys and duplicate
removal), runtime values, nmcli parsing with escaped colons, card detection,
Wi-Fi validation, and secret non-leakage.

An end-to-end run against a throwaway card (`/tmp/fakecard`) completed:
install -> conf rewrite -> `verify_sd.sh` PASS.  Curses start/exit was smoke-
tested under a real pty.

### 17.3 Live progress dashboard

`sdcard_receiver.py` now tracks uploads in a `Status` object and serves:

* `GET /` - dark dashboard, refreshes every 2 s: active file, percent, bar,
  MB/s, ETA, partial files and bytes stored;
* `GET /api/status` - the same data as JSON;
* `PART ... (NN.N%) X.XX MB/s` once per 32 MiB and
  `DONE ... (X.XX MB/s avg)` in the container log.

Both routes are deliberately unauthenticated (they expose only media names
and byte counts); uploads still require the token.  Verified locally with a
resumable upload, then deployed to `insta360-sink` on home-server and checked
at `http://192.168.1.10:8844/` and over the tailnet at
`http://100.101.102.103:8844/`.

### Remaining

1. Refresh the real card's scripts with this version (keep its existing
   `wifi-sta.conf` for `HomeWiFi` until the trip).
2. Before the trip: use the TUI to write the phone hotspot
   (`WIFI_SSID`/password) and set `START_TELNETD=0`; do one hotel-style boot
   and confirm the ~15 s AP fallback by booting away from both networks.
3. Let the remaining ~27 GB finish; spot-check one SHA-256.

## 18. Public repository, container image and CI (2026-10-24)

The working tree moved from the research checkout to its own repository at
`https://github.com/wkipling/instabackup` (public).  Local-only material
(`vendor/`, `analysis/` except `PROTOCOL.md`, `tools/jadx`, `tools/jdk`) stays
on the build machine and is ignored by git, as are generated binaries,
certificates, tokens, Wi-Fi profiles and logs.

### Container

`server/Dockerfile` packages the direct receiver on `python:3.12-slim` for
`linux/amd64` and `linux/arm64`.  The entrypoint reads:

* `SDCARD_ROOT` (default `/data`)
* `SDCARD_PORT` (default `8844`)
* `SDCARD_BIND` (default `0.0.0.0`)
* `SDCARD_TOKEN_FILE` (optional; uploaded requests require the token)
* `SDCARD_MIN_FREE_MB`

`docker-compose.yml` deploys it with the Unraid UID/GID (`99:100`) and a
read-only `./appdata:/app` mount for `sd-token`.  The status endpoints are
unauthenticated and read-only; uploads are not.

### GitHub Actions

* `.github/workflows/ci.yml` - shell/Python syntax, builds every AArch64
  helper with Zig 0.17, generates throwaway certificates, and runs the whole
  offline suite (TUI, Wi-Fi fallback, uploader/receiver, installer, cloudkick,
  tailrelay, preload, bootstrap).
* `.github/workflows/container.yml` - builds and pushes
  `ghcr.io/wkipling/instabackup` on `main` and tags (multi-arch).
* `.github/workflows/release.yml` - on a `v*` tag, builds the card helpers and
  publishes `instabackup-camera-aarch64.tar.gz`, the individual binaries and
  `SHA256SUMS`.

### Secrets

`camera/.sd-upload.token`, `camera/wifi-sta.conf` and `camera/certs/` are
gitignored.  The TUI supplies the Wi-Fi password, upload token and Tailscale
auth key as mode-0600 temporary files (`--wifi-conf`, `--upload-token-file`,
`--authkey-file`), so none appears in argv or shell history.

### Live container switched to the published image (2026-10-24)

`insta360-sink` on the Unraid box (`rack720xd-unraid`) now runs
`ghcr.io/wkipling/instabackup:latest` instead of the ad-hoc
`python:3.12-slim` container.  It was first tested on a spare port with the
same `/app` (read-only token) and `/data` mounts, then recreated on 8844:

* image: `ghcr.io/wkipling/instabackup:latest`
* user `99:100`, `--restart unless-stopped`
* `-p 8844:8844`
* `-v /mnt/user/appdata/insta360-backup:/app:ro`
* `-v /mnt/user/MyShare/insta360-backup/sdcard:/data`
* `SDCARD_ROOT=/data`, `SDCARD_PORT=8844`, `SDCARD_TOKEN_FILE=/app/sd-token`

Health: `healthy`; `/api/status` still reports the 2.09 GB already stored and
the 90 MB partial.  No podman is involved on Unraid; this uses its normal
Docker daemon.

## 19. Dashboard redesign and simpler onboarding (2026-10-24)

### Live dashboard

`GET /` is now a single-page live view; the `<meta http-equiv=refresh>` tag is
gone.  JavaScript polls `/api/status` every 2 s and updates only the changed
nodes, pausing while the tab is hidden.  It shows:

* **Incoming** - active uploads with a progress bar, bytes, MB/s and ETA;
* **Waiting to resume** - partial files that are not currently being written;
* **Files** - every completed file with size and mtime, plus a path filter;
* summary figures for stored files, bytes, free space and last activity.

`GET /api/files` was added (read-only, unauthenticated like `/api/status`) and
returns path/size/mtime for every completed file.  `Status.completed` now
stores `(bytes, mtime)` and `Status.file_list()` serves the ledger.  The active
map drops entries after `SDCARD_ACTIVE_GRACE` seconds (default 15) so a dropped
connection becomes "waiting to resume" instead of a stale progress bar.

Fixed while testing: `.part` files found at startup were keyed with the
`.part` suffix, while uploads address the target path.  After a restart, a
completed file could leave a ghost partial.  `_scan` now strips `.part` and
drops partials that already have a final file.

### Simpler docs and onboarding

* README rewritten around the direct X4 Air path; long-form material moved to
  `notes/ADVANCED.md`.  It states explicitly that only X4 Air 1.2.7 is tested,
  with X4 1.9.21 and X5 listed as untested.
* `scripts/setup-server.sh` - Docker-only one-command receiver setup (token,
  pull, run; `--replace`, `--dry-run`), no Compose requirement.
* `inscloud_server.py --generate-certs DIR` and `--generate-certs-only` now
  create the paid-cloud CA/server certs via `camera/make_certs.sh`, so the
  advanced path no longer needs a separate cert step.  The direct path needs
  no certificates at all.
* CI checks the self-generated certificates and the `/api/files` + dashboard
  response.

Found during live deployment: the `/app` bind mount (used for `sd-token`)
was shadowing the receiver code baked into the image, so the container kept
running a stale scp'd copy.  The Dockerfile now installs the receiver under
`/opt/instabackup/` and the entrypoint execs that path, leaving `/app` free
for the token mount.

## 16. Tailscale crash-loop diagnosis (2026-10-25)

The card's 106-file queue had drained, but every pass logged
`primary unreachable; using fallback 192.168.42.120:8844`: the camera had
never uploaded through the tailnet. `bootup.log` showed `tailscaled` dying
and the watchdog refusing to restart it ("2691 starts in 5 min") for hours.

Two independent bugs:

* `tailscale-up.sh` ran `tailscale up` even when `tailscaled.state` held a
  complete login (`LoggedOut: false`, `WantRunning: true`). `tailscaled`
  reconnects from that state by itself. Running the 25 MB CLI needlessly
  exposed the known exFAT bad-read/segfault behaviour (`rc=139`).
* The crash-loop guard rewrote its timestamp on every call, so its five
  minute window never expired. After six fast failures it blocked every
  later attempt until the next boot.

Fixes:

* Saved state now skips the CLI entirely; only a first login runs
  `tailscale up` with the auth key.
* `tailscaled` is staged in `/tmp` and hash-verified before execution;
  falling back to the card is logged. The CLI is only staged for first
  login, then removed to leave room.
* The guard now allows six attempts per five minutes, then waits five
  minutes and resets, instead of disabling Tailscale forever.
* Failed starts append the tails of the daemon and `up` logs to
  `tailscale.log` on the card (rotated at 200 KB), so the next failure has
  evidence. `/tmp/inscloud-tailscaled.log` is capped at 256 KB.
* TUI: when a saved login exists it now offers "Replace it with a new auth
  key"; replacing removes `tailscaled.state` and the `.logged-in` marker so
  the camera logs in cleanly.
* `camera/test_tailscale_up.py` pins both paths with fake binaries: saved
  state must not execute the CLI; a first login must use the key.

The direct uploader and dashboard are unchanged. `insupload` now uses 8 MiB
chunks (was 1 MiB) to cut per-request connection setup through tailscaled.
