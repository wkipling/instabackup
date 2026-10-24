# X4 1.9.21 quick start — camera → your server over a phone hotspot

This is the short version for a **physical X4 running 1.9.21**.  It uses the
one-time preload bootstrap because X4's stock `/etc/init.d/S99bootdone` has the
`/tmp/SD0/bootup.sh` hook commented out.

> **X4 Air is different and easier.**  Its 1.2.7 firmware runs
> `/tmp/SD0/bootup.sh` directly, so no bootstrap or firmware modification is
> needed.  Use [`X4-AIR-QUICKSTART.md`](X4-AIR-QUICKSTART.md) instead.

## 0. What has been validated, and what has not

Validated offline against the official X4 1.9.21 image
(`vendor/firmware/Insta360X4FW_1.9.21.bin`, MD5
`e9754e9f47e71352e9522e282e3ce436`):

- rootfs is UBI/UBIFS, kernel cmdline has no `ro`;
- `S99bootdone` has the SD hook commented out;
- `instaAIP` starts an unauthenticated HTTP server on `0.0.0.0:80`;
- `/upload_file` writes the multipart filename under `/tmp/SD0/DCIM/` without
  sanitising `../`;
- `/algorithm/...` can read files under `/tmp/SD0` (used for read-back);
- glibc 2.31 supports `/etc/ld.so.preload` and ignores a missing preload
  object;
- X4 action dispatch (0/7/8) has no binding check;
- the uploader protocol, server, relay and Tailscale flags pass offline tests.

**Not yet run on a physical X4.**  The first hardware run is the checklist
below.  If anything fails, use the camera's SD `bootup.log` and the server's
`requests.log`.

## 1. Home server

```sh
# on the home server
tailscale up
tailscale ip -4                      # e.g. 100.101.102.103

cd research/cloud-backup
sudo python3 server/inscloud_server.py \
    --root /srv/insta360-backup \
    --cert camera/certs/server.crt \
    --key  camera/certs/server.key
```

Keep the default `--public-base https://service-test.insta360.com`.  X4's
native origin is `https://cloud.insta360.com`; both are redirected and covered
by the certificate, and the server accepts both path forms.

## 2. Prepare the SD card

```sh
cd research/cloud-backup/camera
./install_to_sd.sh /run/media/$USER/SDCARD - \
    --mode tailscale \
    --target 100.101.102.103 \
    --authkey-file /tmp/tskey \
    --x4
./verify_sd.sh /run/media/$USER/SDCARD
```

`--x4` creates `DCIM/` (for the bootstrap probe) and `NO_HIBER` (so
`ambernation.sh` does not hibernate during an unattended backup).

## 3. One-time bootstrap (USB unplugged)

1. Insert the card into the X4.
2. Power it on **on battery, with USB unplugged**.  The stock form and
   `/algorithm` read-back are gated while USB is active.
3. Connect the PC/phone to the camera's own Wi-Fi AP.  The camera is
   `192.168.42.1`.
4. Run:

```sh
cd research/cloud-backup/camera
python3 x4_bootstrap.py --camera 192.168.42.1
```

Expected output ends with `bootstrap installed. Power-cycle the camera.`

It installs:

```
/usr/lib/libcloudbootstrap.so
/etc/ld.so.preload   (one line: /usr/lib/libcloudbootstrap.so)
```

The probe/library/preload are read back through `/algorithm`; the script
refuses to install the preload if the library cannot be verified (unless
`--force`).

## 4. Reboot and back up

1. Power-cycle the camera.  It is now safe to plug USB in for charging.
2. Make sure the camera is connected to the hotel phone hotspot (configure it
   once in the app; the camera remembers it).
3. Leave the camera on/charging.  On boot:
   - glibc loads the preload library;
   - its constructor waits for `instaAIP`;
   - `/bin/sh /tmp/SD0/bootup.sh` installs DNS/CA, starts tailscaled +
     tailrelay, and `cloud-upload-watch.sh` sends actions `8,7,0` to
     `instaAIP`'s `cloud_msg` queue;
   - the SDK uploads to your server over Tailscale.
4. Check:
   - `tail -f /tmp/SD0/bootup.log` on the card (eject is not required on the
     camera; read it after shutdown or over telnet);
   - `tail -f /srv/insta360-backup/requests.log` on the server;
   - files appear in `/srv/insta360-backup/<localMediaId>/`.

Useful strings in `bootup.log`:

```
tailscale: tailscaled is up
tailscale: tailnet address: 100.x.y.z
cloud-upload: tailnet path ready
cloud-upload: sending actions 870
```

## 5. Disable / recover

Neutralise the hook (the library stays but is not loaded):

```sh
cd research/cloud-backup/camera
python3 x4_bootstrap.py --camera 192.168.42.1 --disable
```

Power-cycle.  A stock X4 firmware reflash removes both files.  This is a
firmware modification; do not install it on a camera you cannot reflash.

## Troubleshooting

| Symptom | Check |
|---|---|
| `GET /upload_file` forbidden | USB is active or camera is in a factory/STA gate; unplug USB and use the camera AP. |
| Probe read-back fails | USB still active, or evhtp normalised `..`; fix USB/AP first. `--force` is an unsafe escape hatch. |
| No `/tmp/x4preload.log` after reboot | `/etc/ld.so.preload` not written, rootfs not writable, or preload path wrong; rerun bootstrap. |
| `x4preload: exec /bin/sh failed` | `/bin/sh` missing or rootfs damaged; reflash. |
| `bootup.log` absent | Preload did not launch; check `/tmp/x4preload.log`. |
| `tailnet path not ready` | Tailscale auth key/ACL/target; check `diag.log` with `CLOUD_DIAG=1`. |
| No uploads after actions | server logs, cert SAN/date, camera clock, `instaAIP` log. |
