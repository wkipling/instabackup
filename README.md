# Self-hosted Insta360 camera cloud backup

The camera's paid "auto cloud backup" is performed entirely by the camera
firmware (`/usr/bin/instaAIP` + `/usr/lib/libInsCloudSdk.so` on X5 and X4 Air,
`/usr/lib/libinsCloudSdk.so` on X4), not by Studio or the phone app. This
directory contains a drop-in replacement cloud service and two ways to run the
uploader without the paid app flow:

- **X4 Air 1.2.7 and X5 (stock SD hook):** SD-card only; nothing on the
  camera's root filesystem is modified.  This is the easiest path.  See the
  [X4 Air quick start](notes/X4-AIR-QUICKSTART.md).
- **X4 firmware 1.9.21:** `S99bootdone` has the `/tmp/SD0/bootup.sh` block
  commented out.  A one-time `x4_bootstrap.py` installs a tiny preload hook in
  `/usr/lib` + `/etc/ld.so.preload` through the camera's own unauthenticated
  web UI.  After that the same SD-card kit runs on every boot.  This **does**
  modify two files on the camera's writable root filesystem; read the warning
  in [X4 setup](#x4-firmware-1921).

Two transport modes are supported:

- **lan** – the server is on the same network; cloud hostnames resolve to its
  LAN IP.
- **tailscale** – the camera joins your tailnet.  It backs up from any Wi-Fi or
  phone hotspot (hotel, mobile data) with no port forwarding and no public
  exposure.  TLS is still terminated by your server.

The complete recovered protocol is documented in
[`analysis/PROTOCOL.md`](analysis/PROTOCOL.md).

## Container image

The direct receiver is published to GitHub Container Registry:

```sh
docker pull ghcr.io/wkipling/instabackup:latest
```

Or use the included compose file (creates the token file on first run):

```sh
mkdir -p appdata data
openssl rand -hex 24 > appdata/sd-token
docker compose up -d
```

The camera only needs this host's LAN or tailnet IP and port `8844`, plus the
same token value in `sd-upload.token` on the card.  Progress is at
`http://SERVER:8844/` (read-only and unauthenticated); uploads always require
the token.

## Quick start: hotel / phone hotspot

1. Home server: install Tailscale, `sudo tailscale up`, note
   `tailscale ip -4` (e.g. `100.101.102.103`).  Run `inscloud_server.py` on
   port 443 as usual.
2. PC: `cd camera && ./install_to_sd.sh /run/media/$USER/SDCARD - \
   --mode tailscale --target 100.101.102.103 --authkey-file /tmp/tskey`
   Add `--x4-air` for an X4 Air (creates `NO_HIBER`, no bootstrap).  Add
   `--x4` only for an X4 1.9.21.
3. Put the card in the camera (cold boot, not wake-from-hibernation) and
   configure the phone hotspot in the camera's Wi-Fi settings once
   (2.4 GHz/WPA2).  Use a wall charger/power bank while backing up, not a
   computer's data-capable USB port.
4. **X4 1.9.21 only:** connect the PC/phone to the camera's own Wi-Fi AP
   (camera is `192.168.42.1`) and run
   `cd camera && python3 x4_bootstrap.py --camera 192.168.42.1` once.
   Power-cycle the camera.  The X4 Air and X5 need no such step.
5. At the hotel: enable the hotspot, power the camera, leave it on.  Files
   appear in `/srv/insta360-backup/<localMediaId>/`.

No port forwarding, public DNS, or app toggle is involved.  The next sections
explain the details and the LAN-only alternative.

### Direct SD uploader (works without `instaAIP`)

If the camera's paid-cloud trigger never reaches the network, use the direct
path.  `insupload` walks the media tree and pushes new bytes over the same
Tailscale link to `server/sdcard_receiver.py`; a dropped hotspot resumes at
the offset the server already has.

* Server: run the receiver container (`docker compose up -d`, or the image
  above).  It listens on port 8844 and takes `--token-file /app/sd-token`.
* Card: `./install_to_sd.sh ... --upload-token-file <token>`, then keep
  `START_SD_UPLOAD=1` and `SD_UPLOAD_PORT=8844` in `cloud-backup.conf`.
* The uploader scans `/tmp/SD0/DCIM`, skips a clip that changed in the last
  30 s, and logs to `upload.log` on the card.  Server log:
  `docker logs insta360-sink`.
* `tailscaled` runs in userspace-networking mode, so the uploader dials
  `127.0.0.1:8080` with HTTP `CONNECT`; it cannot open `100.x` sockets
  directly.  Keep `SD_UPLOAD_PROXY` at its default in tailscale mode.
* Progress: open `http://<server>:8844/` (or `/api/status`) for a live
  dashboard with the active clip, percent, MB/s and ETA.  The container log
  prints one `PART ... (NN.N%)` line per 32 MiB and a `DONE ...` line per
  file.  The status routes are read-only and unauthenticated; uploads still
  require the token.

## How it works

```
 SD boot hook ──cloudkick 870──> instaAIP cloud_msg queue
 (X5: S99bootdone              │
  X4: ld.so preload)           │ instaAIP + libInsCloudSdk.so
 (or phone app)                │ HTTPS (TLS verified)
                               ▼
                 cloud.insta360.com / service-test.insta360.com
                               │ DNS/CA redirect
                               ▼
                   inscloud_server.py (your machine)
                               │
                               ▼
                       /srv/insta360-backup/<localMediaId>/<file>
```

1. X5 and X4 Air firmware ship with the default endpoint
   `https://service-test.insta360.com/cloud-service` hardcoded in `instaAIP`
   (plus a hardcoded test JWT, the camera model string, firmware `0.5.3`, and
   DNS server `8.8.8.8`). The production host `cloud.insta360.com` is covered
   too in case the runtime configuration replaces the endpoint.
2. On X5 and X4 Air the stock firmware executes `/tmp/SD0/bootup.sh` as root on
   every boot (`/etc/init.d/S99bootdone`); an SD-card `instaAIP` wrapper can
   run it even earlier.  On X4 1.9.21 that block is commented out, so the
   one-time preload hook runs the same script instead.  We use that hook to:
   - resolve the Insta360 hosts to the local server (via `/etc/hosts` and by
     answering DNS on `8.8.8.8`, which the SDK uses through c-ares); a small
     watchdog restarts the resolver if a stock Wi-Fi script kills dnsmasq;
   - append a local CA to the system trust bundle (bind-mounted, runtime only).
3. The camera then talks to `inscloud_server.py`, which speaks the recovered
   protocol and stores files on local disk.
4. On X5 and X4 Air no firmware is modified; rebooting without the SD-card
   files (or removing them) restores the original behaviour.  On X4 the two
   bootstrap files in `/usr/lib` and `/etc` persist until removed or
   reflashed; the SD card still supplies all runtime code (see X4 setup
   below).

In **tailscale** mode, step 2 changes shape:

```
 camera: DNS 8.8.8.8 -> 127.0.0.1
   instaAIP/libcurl -> 127.0.0.1:443 (tailrelay)
                         │  HTTP CONNECT (no TLS termination)
                         ▼
               tailscaled userspace proxy 127.0.0.1:8080
                         │  WireGuard/DERP over the hotel Wi-Fi or hotspot
                         ▼
               home server tailnet IP:443 (TLS ends here)
```

`tailrelay` is a tiny freestanding AArch64 binary (no libc).  It exists because
instaAIP's libcurl never honours `http_proxy`, and its `ClientConfig` proxy
string is empty in stock firmware.  The kernel has no `/dev/net/tun`, so
tailscaled runs with `--tun=userspace-networking` and exposes only a local
CONNECT proxy.  The certificate check stays end-to-end: the camera still sees
`service-test.insta360.com`, and your server presents the certificate for it.

## Requirements

- Camera: X4 Air 1.2.7 / X4 / X5 (X2/X3 should work, the SDK is the same
  family).  X4 Air firmware is published under the product slug
  `insta360-x4air` and is `Insta360X4AirFW_1.2.7.bin`,
  MD5 `53a8d0ebb937a41ef0edc92bdf556f40`.
- A Linux machine running `inscloud_server.py`, with root (port 443).
- For remote mode: the server on a Tailscale tailnet, a Tailscale account, and
  the `tailscale_*_arm64` binaries (already in `vendor/tailscale/`).  Reserve
  ~80 MB on the SD card and expect tailscaled to use tens of MB of RAM.
- TCP 443 open on the server firewall (e.g. Fedora:
  `sudo firewall-cmd --add-service=https --permanent && sudo firewall-cmd --reload`).
- Python 3.10+ (standard library only).
- No app/account/subscription is needed for the trigger below: the SD hook
  posts the same action messages the app's "enable backup" switch causes the
  RTOS to post.  You only need to configure the camera's Wi-Fi once (normal
  app setup) so it has a default route.

## Setup

### 1. Generate certificates (once)

```sh
cd camera
./make_certs.sh certs <SERVER_IP>
```

Copy `camera/certs/ca.crt` to the SD card root as `ca.crt` and
`camera/certs/ca.hash` as `ca.hash` (the hash lets OpenSSL's `CAPATH` lookup
find the CA as well as the `cacert.pem` bundle).

> If you generated certificates with an earlier version of this project,
> delete `camera/certs` and regenerate: the CA now carries critical
> `basicConstraints`/`keyUsage` extensions, which newer TLS stacks require.

### 2. Run the server

```sh
sudo python3 server/inscloud_server.py \
    --root /srv/insta360-backup \
    --cert camera/certs/server.crt \
    --key  camera/certs/server.key \
    --public-base https://service-test.insta360.com
```

Notes:

- Default port is 443. The camera builds API URLs from a base URL without a
  port, so the API must be on 443 (upload URLs may use any port).
- `--public-base` is what the camera is told to upload to. Keep it on one of
  the redirected hostnames so the same certificate is valid.  If you add an
  explicit port, the server must listen on that port as well.
- Files land in `<root>/<localMediaId>/<original file name>`, with a
  `md5sum`-compatible `<file>.md5` sidecar written in the background after
  completion (so the final upload response is never delayed by hashing a
  multi-GB file).
- Every request is appended to `<root>/requests.log`; state is in
  `<root>/state.json` (resumable multipart uploads).  Use a local filesystem
  for `<root>`: uploads rely on sparse files and atomic renames.
- If Syncthing shares `<root>`, exclude `.parts/`, `state.json` and
  `requests.log` (partial files/live state).  Completed media directories and
  the `.md5` sidecars are safe to sync.

### 2b. Run as a service (optional)

`server/insta360-backup.service` is a hardened example unit.  It runs the
server as an unprivileged `insta360` user with `CAP_NET_BIND_SERVICE` and keeps
writes confined to `/srv/insta360-backup`.  Adjust the paths at the top of the
unit, then:

```sh
sudo cp server/insta360-backup.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now insta360-backup
journalctl -u insta360-backup -f
```

### 2c. Remote mode: put the home server on Tailscale

Only needed for `BACKUP_MODE=tailscale`.

```sh
# on the home server
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4          # e.g. 100.101.102.103 -> TAILNET_TARGET
```

- Keep `inscloud_server.py` listening on `0.0.0.0:443`; it is then reachable on
  the tailnet address. Keep `--public-base` at its default.
- If firewalld blocks the tailnet interface:
  `sudo firewall-cmd --zone=trusted --add-interface=tailscale0`.  Tailscale
  normally does this itself.
- In the Tailscale admin console create a **reusable** auth key
  (Settings -> Keys).  A tag such as `tag:insta360` is a good idea.  If your
  ACLs are not the default allow-all, permit the camera tag to reach the
  server tag on TCP 443.  Consider disabling key expiry for the camera node:
  an unattended camera may be offline longer than the default expiry.
- MagicDNS is not required; `TAILNET_TARGET` is a numeric tailnet IP.

### 3. Prepare the SD card

**Recommended: the onboarding TUI.**  With the card mounted, run:

```sh
cd camera
python3 setup_tui.py
```

It detects the card, lists nearby Wi-Fi networks (`nmcli`), asks for the
server address and token, optionally takes a Tailscale auth key, then runs
`install_to_sd.sh` and `verify_sd.sh`.  The Wi-Fi password is read without
echo and passed in a mode-0600 temporary file; it never appears on a command
line.  A non-interactive form exists for scripts:

```sh
python3 setup_tui.py --non-interactive --sd /run/media/$USER/SDCARD \
    --wifi-conf /tmp/wifi.conf --mode tailscale \
    --target 100.101.102.103 --token-file .sd-upload.token \
    --authkey-file /tmp/tskey
```

The quickest manual path (generates certs if needed, copies everything,
writes the config):

```sh
cd camera
./install_to_sd.sh /run/media/$USER/SDCARD 192.168.1.10
```

For remote mode (the camera backs up from any network):

```sh
./install_to_sd.sh /run/media/$USER/SDCARD - \
    --mode tailscale \
    --target 100.101.102.103 \
    --authkey-file /tmp/tskey \
    --x4-air
./verify_sd.sh /run/media/$USER/SDCARD    # pre-flight check
```

Use `--x4-air` for an X4 Air (recommended; creates `NO_HIBER` and skips the
X4 bootstrap), or `--x4` for an X4 1.9.21.  `--authkey-file` keeps the
Tailscale key out of `ps` output and shell history; `--authkey` is still
accepted for compatibility.  Add `--with-wrapper` only for X5
and X4 Air if the card is mounted executable (it normally
is, because `S99bootdone` executes `bootup.sh` from it; the wrapper is
optional).  On X4 the preload hook runs `bootup.sh` through `/bin/sh`, so a
`noexec` card is fine.  Or copy the files by hand to the **root** of the SD
card:

```
bootup.sh                 (chmod +x)
cloud-upload-watch.sh     (chmod +x; starts the uploader without the app)
cloudkick                 (chmod +x; tiny AArch64 helper, see below)
tailrelay                 (chmod +x; tailscale mode only)
tailscale-up.sh           (chmod +x; tailscale mode only)
tailscale/                (tailscale mode only: tailscaled + tailscale CLI)
diagnose.sh               (optional; writes diag.log when CLOUD_DIAG=1)
instaAIP                  (optional, recommended: copy camera/instaAIP-wrapper,
                           chmod +x; installs DNS/CA before the daemon starts)
ca.crt
ca.hash                   (optional but recommended)
cloud-backup.conf         (from cloud-backup.conf.example)
```

`tailrelay` is a freestanding AArch64 ELF built by
`camera/build_tailrelay.sh` (clang/Zig, raw Linux syscalls, no libc).  It
listens on `127.0.0.1:443` and tunnels each connection through tailscaled's
local HTTP proxy.  `camera/test_tailrelay.py` runs the same protocol code on
the host against the real mock server and a CONNECT proxy.

`cloudkick` is a 508-byte freestanding AArch64 executable built by
`camera/cloudkick_build.py` (no cross compiler needed; it uses raw Linux
syscalls).  It posts action digits to instaAIP's `cloud_msg` SysV queue
(key `0x44455255`, `mtype=1`, action at `mtext[4]`).  `cloud-upload-watch.sh`
copies it to `/tmp` (so a `noexec` SD card is fine) and runs
`cloudkick 870` after Wi-Fi is up:

- action **8** = query the server's uploaded/uploading lists and reconcile the
  local SQLite records (so already-backed-up media is skipped);
- action **7** = `ins_cloud_sdk_file_group_cfg`: scan `/tmp/SD0/DCIM`, group
  media, build the upload list;
- action **0** = `ins_cloud_sdk_upload_start`: tune TCP and start
  `InsCloudClient::upload()`.

This is the same pair the app triggers through the RTOS, but sent directly on
the camera's Linux side.  It is why the app's entitlement/binding check no
longer blocks you.  See [`notes/ENABLE.md`](notes/ENABLE.md) for the recovered
message format and action map.

`instaAIP` is the stronger hook: the stock `S99bootdone` prefers
`/tmp/SD0/instaAIP` over `/usr/bin/instaAIP` and starts it **before**
`bootup.sh`.  The wrapper runs `bootup.sh` first, then `exec`s the real
daemon, so DNS and the CA are already in place when the uploader initialises.
Without it, the redirect is installed a couple of seconds after `instaAIP`
starts (still fine in practice, because the SDK creates its curl handles
lazily).

> **Warning:** only install the `instaAIP` wrapper if the SD card is mounted
> executable.  `S99bootdone` does not fall back to `/usr/bin/instaAIP` when
> `/tmp/SD0/instaAIP` exists but cannot be executed, so a `noexec` mount would
> leave the camera without its daemon.  If in doubt, use `bootup.sh` only.

`cloud-backup.conf` (LAN):

```sh
BACKUP_MODE=lan
SERVER_IP=192.168.1.10   # your server
SERVER_PORT=443
RESTART_AIP=0
START_TELNETD=0
START_CLOUD_UPLOAD=1     # app-free trigger
CLOUD_UPLOAD_INTERVAL=3600
CLOUD_UPLOAD_DELAY=15
```

Remote mode instead uses:

```sh
BACKUP_MODE=tailscale
TAILNET_TARGET=100.101.102.103   # `tailscale ip -4` on the home server
TAILSCALE_AUTHKEY=tskey-auth-... # reusable key; only for the first login
TAILSCALE_HOSTNAME=insta360-cam
TAILSCALE_PROXY_PORT=8080
#TAILSCALE_NETWORK_WAIT=600
#TAILSCALE_GOMEMLIMIT=96MiB
```

The node state is kept in `tailscale/tailscaled.state` on the SD card, so later
boots do not need the auth key.  Treat that file like a password.

If the card uses exFAT/FAT32 the executable bit is usually emulated by the
mount options; verify with `ls -l` after mounting on a PC. If `bootup.sh` is
not executed, see Troubleshooting.

### X4 Air firmware 1.2.7

X4 Air is the easy case.  Its `S99bootdone` runs `/tmp/SD0/bootup.sh`
unconditionally and even supports a `/tmp/SD0/instaAIP` override, so the card
is the only install location.  Use:

```sh
cd camera
./install_to_sd.sh /run/media/$USER/SDCARD - \
    --mode tailscale --target 100.101.102.103 \
    --authkey-file /tmp/tskey --x4-air
./verify_sd.sh /run/media/$USER/SDCARD
```

`--x4-air` creates `NO_HIBER` and sets `RESTART_AIP=1`, but does **not** run
or require `x4_bootstrap.py`.  Full details and the hotspot checklist are in
[`notes/X4-AIR-QUICKSTART.md`](notes/X4-AIR-QUICKSTART.md).

The image's base URL is `https://service-test.insta360.com/cloud-service`
(confirmed in `instaAIP`'s initializer), so the effective API path is
`/cloud-service/cloud/service/camera/...`; the server accepts it.  The
`cloud_msg` contract and action table are identical to the X5 and are checked
by `tools/verify_x4air_fw.py`.

### X4 firmware 1.9.21

X4 1.9.21 does **not** run `/tmp/SD0/bootup.sh`: the stock
`/etc/init.d/S99bootdone` has that block commented out.  The camera does,
however, expose an unauthenticated web UI from `instaAIP` on TCP/80.  Its
`/upload_file` handler takes the multipart filename verbatim and writes
`/tmp/SD0/DCIM/<filename>`, so a `../../../` filename reaches the writable
UBIFS root filesystem.

X4's SDK origin is `https://cloud.insta360.com/` (X5 uses
`service-test.insta360.com`); `bootup.sh` redirects and the certificate covers
both, and the server collapses doubled slashes.  The default
`--public-base https://service-test.insta360.com` works for X4 too, because
upload URLs stay on a redirected, certificate-valid hostname.  If you prefer
same-origin upload URLs on X4, start the server with
`--public-base https://cloud.insta360.com` instead.

`camera/x4_bootstrap.py` uses that one-time to install:

```
/usr/lib/libcloudbootstrap.so   freestanding AArch64 constructor
/etc/ld.so.preload              one line: /usr/lib/libcloudbootstrap.so
```

On every boot the dynamic loader loads the library into new processes.  The
constructor waits for `/tmp/SD0/bootup.sh` and `instaAIP`, then runs the same
SD-card `bootup.sh` through `/bin/sh` (so a `noexec` card is fine).  It is a
no-op if the card is absent, and glibc ignores an unavailable preload object.

1. Prepare the card with `--x4`:

   ```sh
   cd camera
   ./install_to_sd.sh /run/media/$USER/SDCARD - \
       --mode tailscale --target 100.101.102.103 \
       --authkey-file /tmp/tskey --x4
   ./verify_sd.sh /run/media/$USER/SDCARD
   ```

   `--x4` also creates `NO_HIBER` on the card, because X4's
   `ambernation.sh` honours it.  Remove that file if you want the camera to
   hibernate when idle; leave it in for unattended hotel backups.

2. Insert the card, power the camera **on battery with USB unplugged** (the
   stock form and `/algorithm` read-back are gated while USB is active), and
   connect the PC to the camera's own Wi-Fi AP (not a hotspot).  The camera is
   `192.168.42.1` (confirmed from `usr/local/share/script/ap_start_fast.sh` /
   `change_mode.sh`).  Run:

   ```sh
   cd camera
   python3 x4_bootstrap.py --camera 192.168.42.1
   ```

   The script uploads a probe, the library, and `/etc/ld.so.preload`, and
   reads the first two back through the camera's `/algorithm` route.  It
   refuses to install the preload if the library cannot be verified unless
   `--force` is given.

3. Power-cycle the camera.  On the next boot the preload constructor starts
   the SD `bootup.sh`; the rest is identical to the X5 flow.  Check
   `bootup.log` on the card for `tailscale-up started`, `tailnet path ready`
   and `sending actions 870`.

> **Warning:** this is a firmware modification, unlike the X5 path.  It writes
> two files on the camera's writable root filesystem.  The library has no
> libc dependency and glibc ignores a missing preload object, but an unexpected
> boot failure should be treated as recoverable only by reflashing the stock
> X4 firmware.  To neutralise the hook first:
>
> ```sh
> python3 x4_bootstrap.py --camera <camera-ip> --disable
> ```
>
> That replaces `/etc/ld.so.preload` with blank lines; the library file in
> `/usr/lib` remains but is no longer loaded.  A stock firmware flash removes
> both.  Do not install this on a camera you cannot reflash.  Run `--disable`
> before applying an official firmware update.
>
> The same `/upload_file` endpoint is unauthenticated in stock X4 firmware.
> Do not expose the camera's port 80 to an untrusted network; run the bootstrap
> while connected to its own AP and keep the camera behind your own Wi-Fi/hotspot.

If `GET /upload_file` is `forbidden` in station mode, do the bootstrap while
connected to the camera's own AP.  The POST path does not use the camera's
cloud binding, account, or subscription.

### 3b. Hotel / phone-hotspot workflow

1. Configure the phone hotspot in the camera's Wi-Fi settings once, exactly
   like any other network.  The camera remembers it and reconnects on boot.
   For the card-side station profile, use `setup_tui.py` (or
   `install_to_sd.sh --wifi-conf`); it is independent of the app's own list.
   If the saved SSID is not visible at boot, `wifi-connect.sh` notices the
   stock scan failure after about 15 seconds and restores the camera's own
   hotspot with its original name, password and DHCP range, then skips the
   tunnel/uploader for that boot.  A watchdog reconnect does not use this
   fallback: a brief hotspot outage keeps retrying station mode.
2. Set `BACKUP_MODE=tailscale`, `TAILNET_TARGET`, and (first boot only)
   `TAILSCALE_AUTHKEY` in `cloud-backup.conf`.
3. At the hotel: enable the hotspot, power the camera (ideally on a charger),
   and leave it on.  The boot hook waits for the default route, tailscaled
   connects (direct UDP if possible, otherwise DERP over TCP 443), and the
   watcher only triggers the upload once a real CONNECT to the home server
   succeeds.
4. No port forwarding, public DNS, or inbound ports are involved.  If the
   tailnet is unreachable, the watcher keeps retrying; it does not start a
   batch that would fail immediately.

In tailscale mode the installer sets `RESTART_AIP=1`: `instaAIP` is started by
`S99bootdone` before `bootup.sh`, so it is restarted after the DNS/CA redirect
is in place.  This discards any DNS answer cached during the earlier startup.
If restarting the daemon disturbs camera features on your firmware, set it back
to `0` and use the optional `instaAIP` wrapper instead (bootup.sh runs before
the daemon in that case).

Caveats: the camera must stay powered (disable auto-power-off while charging);
a captive-portal hotel Wi-Fi will not work without a browser, so use the phone
hotspot; the camera clock must be sane because both the local certificate and
Tailscale's control connection validate time; and the camera's Wi-Fi is
2.4 GHz/WPA2-oriented, so enable the hotspot's 2.4 GHz band and WPA2
compatibility (on iPhone: "Maximize Compatibility").  Uploads consume the
phone's data plan, and backing up a large card is best done while the camera
is idle and charging.

If tailscaled is too heavy for the camera (RAM/CPU), the alternative is to run
Tailscale on a small travel router and keep the camera in LAN mode behind it.
The camera-side scripts do not need to change for that.

### 4. Start backup (no app needed)

Insert the card, power the camera, and make sure it is connected to Wi-Fi
(configure it once in the app if it has never been on that network).  In
`tailscale` mode any network works: home Wi-Fi, hotel Wi-Fi without a captive
portal, or a phone hotspot.  With `START_CLOUD_UPLOAD=1` the boot hook waits
for a default route and instaAIP, then runs `cloudkick 870`; no enable step in
the app is required.  In tailscale mode it first waits until
`tailrelay --probe` can open a CONNECT to `TAILNET_TARGET:443`.
The watcher repeats hourly, but skips a cycle while the default interface is
carrying upload traffic (checked via `/proc/net/dev`).  Before each idle scan
it sends action 15 to save the uploader's SQLite records to the camera's FL0
storage, so a reboot only uploads new media (`CLOUD_UPLOAD_SAVE_DB=0` disables
this).

You can also run it by hand on the camera (enable `START_TELNETD=1` first):

```sh
/tmp/cloudkick 870      # sync uploaded state, scan DCIM, start uploading
/tmp/cloudkick 0        # start again with the existing list
/tmp/cloudkick 7        # only rescan
```

If you prefer the normal app path, enabling cloud backup in the app still
works and sends the same actions through the RTOS.  The camera will call
(note the
`/cloud-service` gateway prefix from the base URL plus the SDK's own
`/cloud/service/...` path):

```
GET  /cloud-service/cloud/service/camera/upload/config
POST /cloud-service/cloud/service/camera/upload/derivatives
GET  /cloud-service/cloud/service/camera/upload/item/entrance?...
PUT  <pre-signed upload URL>
POST /cloud-service/cloud/service/camera/upload/item/status/report
```

`inscloud_server.py` also accepts the same paths without the `/cloud-service`
prefix.

Watch `<root>/requests.log` on the server.

## Building from source

The camera helpers are static AArch64 binaries.  Zig is the easiest way to
cross-build them from an x86_64 host:

```sh
make build            # insupload, tailrelay, cloudkick, libcloudbootstrap.so
make certs            # local CA + server certificate for the paid-cloud path
make test             # build + the full offline regression suite
make container        # docker build -f server/Dockerfile -t instabackup .
```

`make build` uses `zig` from `PATH`; a vendored toolchain also works:
`make ZIG=vendor/pyzig/ziglang/zig build`.

Repository layout:

| Path | Purpose |
| --- | --- |
| `camera/` | boot hook, Wi-Fi station mode, uploader, installer, TUI, tests |
| `server/` | direct receiver, fake paid-cloud server, container files |
| `tools/` | firmware/transport analysis helpers |
| `notes/` | per-model quick starts |
| `analysis/PROTOCOL.md` | recovered cloud protocol |
| `PROGRESS.md` | reverse-engineering and deployment log |

## Verification

Firmware-level check (no camera needed):

```sh
python3 tools/verify_x4air_fw.py
```

It verifies the X4 Air 1.2.7 image's MD5, UBIFS location, enabled SD hook,
`instaAIP`/SDK endpoints, and the exact `cloud_msg` queue contract.

On the camera (with `START_TELNETD=1` in `cloud-backup.conf`):

```sh
cat /tmp/SD0/bootup.log
ip addr show lo | grep 8.8.8.8
cat /tmp/inscloud-dns.pid
cat /tmp/inscloud-dns-watchdog.pid
mount | grep -E 'cacert|ca-bundle|/etc/ssl/certs|/etc/hosts'
nslookup service-test.insta360.com 8.8.8.8   # must answer 127.0.0.1 (tailscale)
                                             # or SERVER_IP (lan)
pidof instaAIP
ipcs -q | grep -i 44455255                 # cloud_msg queue
/tmp/cloudkick 870                         # manual trigger (optional)

# tailscale mode only
pidof tailscaled; cat /tmp/inscloud-tailrelay.pid
/tmp/SD0/tailscale/tailscale --socket=/tmp/tailscaled.sock status
/tmp/SD0/tailscale/tailscale --socket=/tmp/tailscaled.sock ip -4
/tmp/SD0/tailrelay --probe 100.101.102.103 443 127.0.0.1 8080   # probe ok
```

From the server:

```sh
tail -f /srv/insta360-backup/requests.log
find /srv/insta360-backup -type f -not -name 'state.json' -not -name '*.log'
```

## Troubleshooting

**Start here when nothing happens:** set `CLOUD_DIAG=1` in
`cloud-backup.conf`, reboot, wait a minute, and read `diag.log` on the SD
card.  It contains the routes, resolver setup, mounts, `instaAIP`/queue state,
listeners, Tailscale status, a real tunnel probe, DCIM count and the ends of
all logs.  `diagnose.sh` only reads state and writes that one file.

| Symptom | Check |
|---|---|
| No `/tmp/SD0/bootup.log` | SD is mounted at `/tmp/SD1` instead (try the second slot); or the card is mounted `noexec`; or the file is not executable. |
| `START_CLOUD_UPLOAD=1` but no uploads | Look for `cloud-upload: waiting` / `sending actions 870` in `bootup.log`. `pidof instaAIP` must succeed and `ip route` must show a `default` route. Try `/tmp/cloudkick 870` manually. |
| No `default` route / watcher never sends | The camera is in AP mode or USB mode, not station mode. Connect it once to your home Wi-Fi in the app (camera Wi-Fi settings), then power-cycle. Charging from a USB **data** port can keep it in USB mode; use a charger. |
| `cloudkick` prints nothing and no uploads | `cloudkick` is silent by design; check its exit code (`echo $?`) and whether `ipcs -q` shows queue key `0x44455255`. If the queue exists but nothing happens, capture the camera's syslog (`logread`/`dmesg`) for `[ins_cloud] recv upload action`. |
| Uploads start but fail immediately | The trigger worked; debug the HTTP side with `requests.log`, TLS mounts, and camera clock as in the rows above. |
| `bootup.log` exists but no requests | DNS: `ip addr show lo` must list `8.8.8.8`; `cat /tmp/inscloud-dns.pid` and `ps | grep dnsmasq`; test with `nslookup service-test.insta360.com 8.8.8.8` on the camera. The watchdog should restore dnsmasq within ~5 s if a Wi-Fi mode switch killed it. |
| TLS errors in camera logs | `mount` must show the CA bind mount (either the `/etc/ssl/certs` directory overlay or the per-file `cacert.pem`/`ca-bundle.crt` mounts); camera clock must be sane (cert validity); CA file must be the same CA that signed `server.crt`. |
| Camera still talks to Insta360 | The runtime config may override the endpoint; both `service-test.insta360.com` and `cloud.insta360.com` are redirected. Capture with `tcpdump -ni any port 443` to see the SNI/host. |
| Uploads stall at "prepare" | Check `requests.log` for `derivatives`; the response must contain `uncompletedItems`. |
| `RESTART_AIP=1` breaks camera features | Set it back to `0`; normally unnecessary. |
| Server won't start: port 443 in use | The camera always uses HTTPS 443, so free the port or terminate TLS in a reverse proxy that forwards the two hostnames to this server. |
| Camera services missing after adding the `instaAIP` wrapper | The SD card is mounted `noexec`. Remove the `instaAIP` file from the card and use `bootup.sh` only. |
| tailscale mode: `tailnet path not ready` repeats | On the camera: `tailscale --socket=/tmp/tailscaled.sock status`; the home server must be listed and online. Check `TAILNET_TARGET` against `tailscale ip -4` on the server, the ACL (camera tag -> server tag, TCP 443), and the server firewall (`tailscale0` trusted). `/tmp/inscloud-tailscaled.log` has tailscaled's view. |
| `tailscale up` prints a login URL | No valid `TAILSCALE_AUTHKEY` and no saved state. Add a reusable key to `cloud-backup.conf` (or run `tailscale up` once via telnet). The key is only needed for the first login. |
| `tailscaled exited during startup` | Look at `/tmp/inscloud-tailscaled.log`. Common causes: no free space on the SD card, the binary was not copied (`tailscale/` missing), or the camera has too little RAM (lower `TAILSCALE_GOMEMLIMIT`, but Go may still be OOM-killed). |
| `tailrelay: bind failed errno=98` | Something else on the camera already listens on 443. The SDK always uses 443, so this must be freed; check `netstat -lntp` via telnet. |
| tailscale mode: uploads start but no requests arrive | The probe only checks TCP. Check the server's `requests.log` and whether `--public-base` was changed to the tailnet IP (it must stay `https://service-test.insta360.com`). |
| tailscale mode: no inbound connection at all | The camera may be on a captive-portal hotel Wi-Fi. Use the phone hotspot; tailscaled needs outbound UDP 41641 or TCP 443 to reach DERP. |

## Reverting

Power off, remove `bootup.sh` / `instaAIP` / `ca.crt` / `ca.hash` /
`cloud-backup.conf` / `tailrelay` / `tailscale-up.sh` / `tailscale/` from the
SD card, reboot. Nothing on the camera's root filesystem is modified; all
redirects and CA changes are bind mounts in RAM.  If Tailscale was used, delete
the camera node in the admin console (and revoke its auth key) so it does not
linger in the tailnet.

## Security notes

- The server accepts any `Authentication` token: the camera-side uploader is
  not authenticated by us. In LAN mode keep it behind a firewall. In tailscale
  mode every device in your tailnet can reach port 443; use an ACL that only
  allows the camera tag to reach the server tag on TCP 443, and do not enable
  Tailscale Funnel for this service.
- `TAILSCALE_AUTHKEY` and `tailscale/tailscaled.state` are node credentials.
  Keep the SD card and the config file private; remove the key from the config
  after the first login if you prefer (state alone is enough).
- `tailrelay` listens only on `127.0.0.1:443` and has no authentication. Any
  local process on the camera could use it to reach the server; that is the
  same trust boundary as the camera's other local daemons.
- The local CA private key (`camera/certs/ca.key`) can sign certificates that
  the camera trusts while the SD card is inserted. Keep it offline.
- `START_TELNETD=1` opens a passwordless root shell on port 2323. Use only for
  debugging on a trusted network.
- The direct uploader uses plain HTTP: the transport is already WireGuard via
  Tailscale, so TLS would only add CPU cost.  Port 8844 is published on the
  LAN by the container, so keep `sd-upload.token` set and never expose 8844
  to the internet.  The receiver rejects path traversal and writes only below
  `--root`.
- The camera's traffic to other Insta360 services (updates, telemetry) is not
  intercepted; only the two cloud upload hostnames are redirected.
