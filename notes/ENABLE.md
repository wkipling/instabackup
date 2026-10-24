# Enabling cloud backup without the app

This is the answer to "the app won't let me enable the paid feature".

## Short version

The app does **not** configure the uploader directly.  It asks the camera's
RTOS to post messages to `instaAIP`'s `cloud_msg` SysV queue.  `instaAIP` then
does all network work on Linux.  We can post the same messages from an SD-card
boot script, with no app, account, binding or subscription.

```
app --BLE/Wi-Fi--> RTOS --SysV msg--> instaAIP --> HTTPS --> our server
                         ^
                         +-- cloudkick (SD card) can send this too
```

## Message format

From `recv_action_handler` @ Ghidra `0x14d31c`, `FUN_0014cf40` and
`FUN_00145ec0`:

| field | value |
|---|---|
| queue key | `0x44455255` (`msgget(key, IPC_CREAT|0666)`, `0x3b6`) |
| `mtype` | `1` |
| message text size | `0x408` |
| action | `mtext[4]` (one byte) |
| `mtext[0..3]` | `1` (as sent by the stock `send_msg` helper) |

Sender (`FUN_0014cf40`) zeroes 0x410 bytes, sets `mtype=1`, `mtext[0..3]=1`
and `mtext[4]=action`, then `msgsnd(q, buf, 0x408, 0)`.

## Actions that matter

| action | function | effect |
|---|---|---|
| 0 | `ins_cloud_sdk_upload_start` | runs `cloud_tcp_tune.sh`, initialises the SDK, calls `setUploadFileInfoList(&DAT_00694d70)` + `upload()` |
| 1 | `ins_cloud_sdk_upload_stop` | stop |
| 2 | `ins_cloud_sdk_upload_pause` | pause |
| 3 | `ins_cloud_sdk_upload_restart` | restart |
| 6 | `ins_cloud_sdk_upload_get_capacity` | capacity |
| 7 | `ins_cloud_sdk_file_group_cfg` | `FUN_00152d8c(0)`: scan `/tmp/SD0/DCIM`, group media, fill `DAT_00694d70`, report totals |
| 8 | `ins_cloud_sdk_query_local_uploaded_file` | `FUN_00152d8c(1)` + `checkFile`; queries `/media/uploaded/list` and reconciles SQLite, so later runs skip uploaded media |
| 9/10 | `FUN_0015038c(1/0)` | clear/update uploaded state |
| 11 | `FUN_001503f0` | clear uploaded list |
| 12 | `FUN_00150780` | send uploaded media list/filenames to RTOS |
| 15 | `ins_cloud_sdk_db_save` | save SQLite DB to RTOS |

`DAT_00694d70` is a `std::vector<InsCloudFileInfoList>` (0x90 bytes per entry).
`ins_cloud_sdk_upload_start` passes `&DAT_00694d70` directly:
`add x1,x20,#0xbc0; add x1,x1,#0x1b0` at `0x14f8f4` (x20=0x694000).

## Why this bypasses the paywall

* The entitlement/binding check is in the **phone app** and RTOS/cloud, not in
  the Linux upload path.  `UploadTaskManager::QueryBindingStatus` has no
  internal callers; only `instaAIP` exposes it to the RTOS.
* The action-0 handler does not query binding status.  It goes straight to
  `ins_cloud_sdk_upload_start`.
* The SDK's hardcoded test JWT is sent in the `Authentication` header; our
  server ignores it.

## X4 Air 1.2.7: where does the script run from?

X4 Air is the easy case.  Its `S99bootdone` has the `/tmp/SD0/bootup.sh` block
**enabled** and also checks `/tmp/SD0/instaAIP` before `/usr/bin/instaAIP`, so
the same card-only trigger used on X5 works unchanged.  No exploit, preload
or rootfs write is involved.

Static validation against `Insta360X4AirFW_1.2.7.bin` (MD5
`53a8d0ebb937a41ef0edc92bdf556f40`) confirms the same queue contract at
`cloud_rx` (Ghidra `0x14c360`): key `0x44455255`, `msgget(...,0x3b6)`,
`mtype=1`, payload `0x408`, `mtext[0..3]=1`, action at `mtext[4]`.  The action
switch handles the same 0/7/8/15 cases and has no binding check.  Its API base
is `https://service-test.insta360.com/cloud-service`, which the local server
already accepts.  Run `tools/verify_x4air_fw.py` to repeat these checks.

## X4 1.9.21: where does the script run from?

The X4 `S99bootdone` has the `/tmp/SD0/bootup.sh` block commented out, so the
X5 SD-only trigger does not apply.  X4 `instaAIP` does expose an
unauthenticated web UI on TCP/80 (`evhtp_bind_socket("0.0.0.0", 80)`):

| route | effect |
|---|---|
| `GET /upload_file` | returns an upload form (unless factory-mode gate) |
| `POST /upload_file` | multipart body; filename is concatenated to `/tmp/SD0/DCIM/` with no traversal check |
| `POST /upload_fw` | firmware upload path; starts a firmware update |
| `GET /algorithm/...` | serves `/tmp/SD0` + suffix (useful for read-back) |

The file write is performed by `FUN_0010d62c`; it does
`strstr(body+8,"filename")`, copies the quoted filename, then
`sprintf("%s%s", "/tmp/SD0/DCIM/", filename)` and `fopen(...,"wb")`.
`x4_bootstrap.py` exploits that once to install a tiny preload library and
`/etc/ld.so.preload`.  The constructor starts `/tmp/SD0/bootup.sh` after
`instaAIP` exists, replacing the disabled init hook.  See README for the
warning: this modifies the camera's writable root filesystem.

## Trigger

`camera/cloudkick` is a 508-byte freestanding AArch64 ELF (built by
`cloudkick_build.py`) that sends the digits in its argument:

```sh
cloudkick f870     # save DB (15), sync uploaded list (8), scan (7), start (0)
cloudkick 870      # sync uploaded list, scan DCIM, start upload
cloudkick 0        # start with the list built by the last 7/8
cloudkick 7        # rescan only
```

`camera/cloud-upload-watch.sh` waits for `pidof instaAIP` and a default route,
copies the helper to `/tmp` (noexec-card safe), and sends `f` (action 15) then
`870` every `CLOUD_UPLOAD_INTERVAL` seconds when the interface is idle.
`bootup.sh` starts it when `START_CLOUD_UPLOAD=1`.

## Verification on hardware

1. `cat /tmp/SD0/bootup.log` -> `cloud-upload: sending actions 870`.
2. `ipcs -q` -> queue key `0x44455255`.
3. `logread` / `dmesg` -> `[ins_cloud] recv upload action:8`, `:7`, `:0`.
4. `tail -f /srv/insta360-backup/requests.log` -> `upload/config`,
   `derivatives`, `item/entrance`, PUT, `status/report`.
5. X4 only: `/tmp/x4preload.log` should say
   `x4preload: starting /tmp/SD0/bootup.sh`.  If it does not exist, the
   preload did not see the card yet, or `/etc/ld.so.preload` was not installed.

If step 1 never appears, enable `CLOUD_DIAG=1` and read `diag.log` (see
README).  The app toggle and the paid plan are irrelevant to this path.

## Transport: LAN vs hotel/hotspot

Enabling (this document) is independent of how packets reach the server.
`cloud-backup.conf` selects:

- `BACKUP_MODE=lan`: DNS answers `SERVER_IP`; the relay is not used.
- `BACKUP_MODE=tailscale`: DNS answers `127.0.0.1`; `tailrelay` CONNECTs
  through tailscaled to `TAILNET_TARGET:443`.  Works from a phone hotspot at a
  hotel with no port forwarding.  See `README.md` and `PROGRESS.md` section 9.
