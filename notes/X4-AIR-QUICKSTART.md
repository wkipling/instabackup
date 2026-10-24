# X4 Air quick start — camera → your server over a phone hotspot

This is the short, validated path for a **physical Insta360 X4 Air running
firmware 1.2.7**. It does **not** modify the camera's root filesystem and does
not use the X4 `/upload_file` exploit. Everything lives on the SD card.

The camera's paid "cloud backup" is just a firmware uploader. Enabling it in
the app only makes the RTOS post four SysV messages to `instaAIP`. This kit
posts those same messages itself, so no Insta360 account, binding, or
subscription is checked on the camera. Your server replaces Insta360's cloud.

## 0. What was validated against 1.2.7

Image: `Insta360X4AirFW_1.2.7.bin`, 309,943,867 bytes, MD5
`53a8d0ebb937a41ef0edc92bdf556f40`.  Update the camera to 1.2.7 if it is
older; the firmware page is `https://www.insta360.com/download/insta360-x4air`
(note: `insta360-x4air`, not `insta360-x4-air`).

- `/etc/init.d/S99bootdone` **runs `/tmp/SD0/bootup.sh` unconditionally** (the
  `/tmp/SD0/instaAIP` override is also present). No bootstrap is needed.
- `instaAIP` uses the base URL
  `https://service-test.insta360.com/cloud-service`, so requests are
  `/cloud-service/cloud/service/camera/...`. The bundled server accepts that.
- The `cloud_msg` queue is exactly:
  key `0x44455255`, `msgget(..., 0x3b6)`, `mtype=1`, payload size `0x408`,
  `mtext[0..3]=1`, action at `mtext[4]`.
- Actions: `8` = reconcile uploaded list, `7` = scan `/tmp/SD0/DCIM`,
  `0` = start upload, `15` = save the uploader database. The action switch has
  no binding/entitlement check.
- `ambernation.sh` honours `/tmp/SD0/NO_HIBER`, so the camera can stay awake
  while charging.
- The uploader's DNS server is hard-coded to `8.8.8.8` (confirmed in the
  `instaAIP` initializer). The kit claims that address on `lo` and answers with
  a local dnsmasq.

Reproduce the static checks:

```sh
cd research/cloud-backup
python3 tools/verify_x4air_fw.py
# if /tmp is small, keep the ~200 MB extraction somewhere else:
# python3 tools/verify_x4air_fw.py --work-dir /var/tmp/x4air-fw
```

## 1. Home server (one time)

You need the server on the same Tailscale tailnet as the camera. No port
forwarding and no public DNS.

```sh
# on the home server
sudo tailscale up
tailscale ip -4                 # e.g. 100.101.102.103 -> TAILNET_TARGET
```

Create a reusable auth key in the Tailscale admin console
(Settings → Keys). A tag such as `tag:insta360-camera` and no expiry is
sensible for an unattended camera.

Start the replacement cloud service:

```sh
cd research/cloud-backup
sudo python3 server/inscloud_server.py \
    --root /srv/insta360-backup \
    --cert camera/certs/server.crt \
    --key  camera/certs/server.key
```

Keep the default `--public-base https://service-test.insta360.com`. The
certificate in `camera/certs/` is valid for that host until 2035. Do **not**
point `--public-base` at the tailnet IP: the camera must keep using the
hostname so the TLS certificate and relay path stay valid.

If port 443 is already occupied (for example, by Unraid's nginx), run the
server on another port such as 8843 and set this in the card's
`cloud-backup.conf`:

```sh
SERVER_PORT=8843
```

The camera still calls `127.0.0.1:443`; only `tailrelay`'s upstream connection
to the server uses 8843.  Keep `--public-base` unchanged.

Smoke-test the TLS endpoint locally:

```sh
curl --cacert camera/certs/ca.crt \
    --resolve service-test.insta360.com:443:127.0.0.1 \
    https://service-test.insta360.com/actuator/ping
# -> {"code": 0, "msg": "Insta360 Cloud"}
```

If Syncthing watches `/srv/insta360-backup`, exclude `.parts/`, `state.json`
and `requests.log`; completed media and `.md5` files are safe to sync.

## 2. Prepare the SD card

**Do not use the camera as a USB card reader for this.**  The reliable
procedure is: unplug the camera, power it fully off, remove the microSD card,
put it in a normal card reader, then copy the files.  No camera-side "USB
mode" is involved, and this avoids the USB host disabling the camera's Wi-Fi.
If you have no reader, a USB-C card reader or a phone with a microSD slot works.
The camera's firmware does contain a USB mass-storage mode (`Usb_Storage` in
the X4 Air image), but its exact on-screen label was not verified; it is not
needed for this mod.

Insert the card in your PC, find its mount point, then:

```sh
cd research/cloud-backup/camera

./install_to_sd.sh /run/media/$USER/SDCARD - \
    --mode tailscale \
    --target 100.101.102.103 \
    --authkey-file /tmp/tskey \
    --x4-air

./verify_sd.sh /run/media/$USER/SDCARD
```

`--x4-air` creates `NO_HIBER`, sets `RESTART_AIP=1`, and omits the X4 web
bootstrap. The card needs about 70 MB free for the two Tailscale binaries.
After the camera has joined the tailnet once, you can remove
`TAILSCALE_AUTHKEY` from `cloud-backup.conf`; the node state is kept in
`tailscale/tailscaled.state`.

If the installer says the Tailscale binaries are missing, the copy in this
working tree should be at
`vendor/tailscale/tailscale_1.104.2_arm64/`; otherwise download them:

```sh
cd research/cloud-backup
mkdir -p vendor/tailscale
curl -L -o /tmp/tailscale_arm64.tgz \
    https://pkgs.tailscale.com/stable/tailscale_1.104.2_arm64.tgz
tar -xzf /tmp/tailscale_arm64.tgz -C vendor/tailscale
```

## 3. Connect the camera to Wi-Fi (station mode)

The camera's own hotspot is **AP mode**: the camera is acting as the access
point, and your phone joins it.  That hotspot has no internet.  To upload, the
camera must switch to **station mode** and join your home Wi-Fi or phone
hotspot.  The camera cannot be in both modes at once, so its own hotspot
disappears while it is on your network; the app may disconnect at that point.
That is normal.

### Option A: card-side station mode (recommended)

The camera can be told to join the network directly from the card, with no app
toggle.  The safest way is to let the installer read a local credentials
file, so the password never appears in shell history or process arguments:

```sh
cd research/cloud-backup/camera
umask 077
read "ssid?Hotspot SSID: "
read -s "pass?Hotspot password: "; echo
printf 'WIFI_SSID=%s\nWIFI_PASSWORD=%s\nWIFI_COUNTRY=AU\n' \
    "$ssid" "$pass" > wifi-sta.local
unset pass

./install_to_sd.sh /run/media/$USER/SDCARD - \
    --mode tailscale --target 100.x.y.z --authkey-file /tmp/tskey --x4-air \
    --wifi-conf wifi-sta.local
```

You can also pass `--wifi-ssid` / `--wifi-password` directly, or edit
`wifi-sta.conf` on the card:

```sh
WIFI_SSID=MyPhoneHotspot
WIFI_PASSWORD=secret123
WIFI_COUNTRY=AU            # optional; stock default is CN
WIFI_BSSID=xx:xx:xx:xx:xx:xx  # optional
```

The values are raw data, not shell code.  On each boot `wifi-connect.sh` uses
the camera's stock station-mode scripts to leave AP mode, scan, join the
network, and obtain a DHCP lease.  It first uses `change_mode.sh`; if no
default route appears it retries with `sta_start_fast.sh` + `sta_connect.sh`,
which also handles WPA3/SAE-only hotspots.  The camera's own hotspot
disappears while it is joined to your hotspot, which is expected.

- To change networks, edit `wifi-sta.conf` and reboot.
- If the join fails, `wifi-connect.sh` waits 90 s and then restores the
  camera's original AP profile, so the camera should not become unreachable.
  Power-cycle after fixing `wifi-sta.conf`.
- To go back to the camera's own hotspot, delete `wifi-sta.conf` (or run the
  installer with `--no-wifi`).
- The Wi-Fi password is stored on the SD card; treat the card as a secret.
- Quotes and backslashes in the SSID/password are rejected because the stock
  `wpa_supplicant` config generator does not escape them.

### Option B: camera app

1. Connect the phone to the camera's own Wi-Fi (`X4 Air******`), open the
   Insta360 app, and connect to the camera.  If asked, authorize on the
   camera's touchscreen or with the shutter button.
2. Open the camera settings.  Look for **Wi-Fi Settings**, or under the cloud
   section for **Auto Backup Wi-Fi settings**.  The app labels this network
   list **Wi-Fi for syncing** and describes it as "Choose a Wi-Fi network for
   your camera to enable automatic backup."
3. Select your home Wi-Fi or phone hotspot, enter its password, and confirm.
   The camera scans and joins it.  The camera's own hotspot will drop and the
   app may reconnect over the new network.
4. Make sure the network actually has internet.  The app/camera will not use a
   Wi-Fi network with no internet access.

Use 2.4 GHz and WPA2 if the hotspot offers a choice.  On iPhone enable
"Maximize Compatibility".  After the first association the camera remembers
the network.  Set auto-power-off to a long/disabled value; `NO_HIBER` stops
hibernation but does not override every idle-power setting.

## 4. Back up

1. Power the camera fully off (not just wake it from hibernation), put the
   card in, then power it on.  The SD hook runs during a cold boot.
2. Turn on the phone hotspot.
3. Power the camera from a **wall charger or power bank**, not a computer's
   USB port (a data-capable USB host can put the camera into USB-storage mode
   and may take Wi-Fi down).
4. Leave it alone. On boot:
   - `S99bootdone` starts the stock `instaAIP`;
   - `/tmp/SD0/bootup.sh` claims `8.8.8.8`, installs the local CA, restarts
     `instaAIP`, starts `tailscaled` in userspace mode, and starts `tailrelay`;
   - `cloud-upload-watch.sh` waits for a default route and a real CONNECT to
     your server, then runs `cloudkick 870`.
5. Files appear on the server under
   `/srv/insta360-backup/<localMediaId>/`.

Watch it:

```sh
# on the phone/card later, or over the debug shell
tail -f /tmp/SD0/bootup.log

# on the server
tail -f /srv/insta360-backup/requests.log
find /srv/insta360-backup -type f -not -name 'state.json' -not -name '*.log'
```

Successful log lines on the camera:

```
tailscale: tailscaled is up
tailscale: tailnet address: 100.x.y.z
cloud-upload: tailnet path ready
cloud-upload: sending actions 870
```

Successful requests on the server start with:

```
GET  /cloud-service/cloud/service/camera/upload/config
POST /cloud-service/cloud/service/camera/upload/derivatives
GET  /cloud-service/cloud/service/camera/upload/item/entrance?...
PUT  /upload/...
POST /cloud-service/cloud/service/camera/upload/item/status/report
```

## How the redirect works

```
camera app / SD hook
        │  SysV cloud_msg: 8,7,0
        ▼
   /usr/bin/instaAIP + libInsCloudSdk.so
        │  HTTPS, SNI service-test.insta360.com
        ▼
   127.0.0.1:443  (tailrelay)
        │  HTTP CONNECT, no TLS termination
        ▼
   tailscaled --tun=userspace-networking
        │  WireGuard / DERP over hotel Wi-Fi or hotspot
        ▼
   home server tailnet IP:443  (TLS ends here)
        │
        ▼
   /srv/insta360-backup/<localMediaId>/<file>
```

The camera verifies the server certificate against
`service-test.insta360.com`; the local CA is bind-mounted over the stock CA
bundle at runtime. DNS answers `127.0.0.1` only for the two Insta360 cloud
hostnames. All other traffic continues to use the hotel/hotspot network.

The uploader is hard-coded to ask `8.8.8.8:53`.  The kit answers on that
address with a private dnsmasq, but only after station mode has taken over
from the camera's AP.  The AP's own DHCP/DNS server also owns port 53; the
hook deliberately waits for a default route and then takes the port, instead
of fighting the AP at boot.  A watchdog restarts the redirect after a stock
Wi-Fi script runs `killall dnsmasq`.

## How risky is this?

This is an **SD-card-only, runtime-only** change.  It does not flash or modify
the camera's root filesystem, bootloader, or kernel.  Removing the card (or
deleting `bootup.sh` from it) restores stock behaviour on the next boot.  A
bad script cannot "brick" the camera in the usual sense; the worst realistic
failure is a slow/hanging boot hook or a failed upload until you power off and
remove the card.  The root hook does run as root, so keep the card's contents
trusted and remove the card before lending it to anyone.

Two non-brick caveats: `NO_HIBER` keeps the camera awake (use a charger), and
the Tailscale auth key/node state live on the card, so treat the card as a
secret.

## Disable / roll back

Nothing was written to the camera's internal filesystem. To stop the backup,
power off the camera, remove the SD card, and delete `bootup.sh` from the card
(or set `START_CLOUD_UPLOAD=0` / `BACKUP_MODE=lan` in
`cloud-backup.conf`). Reinserting a stock card returns the camera to normal.

To unlink the camera from Tailscale, remove the node in the admin console and
delete `tailscale/tailscaled.state` from the card.

## Troubleshooting

| Symptom | Check |
|---|---|
| No `bootup.log` | The camera may have resumed from hibernation instead of cold-booting; power it fully off/on. Otherwise the card may be in the other slot (`/tmp/SD1`), or `bootup.sh` is missing/not executable. |
| `bootup.log` exists, no uploads | `pidof instaAIP`, `ip route`, and `cloud-upload: waiting` lines. Try `/tmp/cloudkick 870` over the debug shell. |
| `tailnet path not ready` | Wrong `TAILNET_TARGET`, server not listening on 443, Tailscale ACL, or auth key rejected. See `/tmp/inscloud-tailscaled.log` and `tailscale status`. |
| TLS errors | Camera clock is wrong, or the CA was not mounted. Check `mount | grep ssl/certs` and `date`. Regenerate certs if they were replaced. |
| `cloudkick` sends but server stays empty | Look at `requests.log`; confirm the camera resolved `service-test.insta360.com` to `127.0.0.1` (`nslookup ... 8.8.8.8`). |
| Files already backed up to Insta360 are skipped | The camera's local uploader DB remembers them. For one cycle set `CLOUD_UPLOAD_ACTIONS=70` (scan + start, omit action 8) to force a full scan, then restore `870` so later runs only upload new files. |
| Camera sleeps mid-upload | Ensure `NO_HIBER` is on the card and auto-power-off is disabled. |
| Hotspot connects but no route | Some hotspots block client-to-client or use 5 GHz only. Use 2.4 GHz/WPA2 and disable "client isolation". |
| Debug shell needed | Set `START_TELNETD=1` in `cloud-backup.conf`, reboot, then `telnet <camera-ip> 2323`. Disable it afterwards. |

For a one-shot diagnostic, set `CLOUD_DIAG=1`; after about a minute the card
contains `diag.log` with routes, DNS, mounts, queue state, Tailscale status and
the tunnel probe.
