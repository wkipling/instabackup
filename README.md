# Insta360 self-hosted backup

Back up Insta360 footage to your own server over Wi-Fi. The camera joins your
phone hotspot, connects over Tailscale (or your LAN), and pushes new files to
a small Docker container. Interrupted uploads resume where they stopped.

**Tested end-to-end on Insta360 X4 Air, firmware 1.2.7 only.** Other models are
untested; see [Models](#models).

## Easy setup

### 1. Server (Docker)

On the machine that will store the footage:

```sh
git clone https://github.com/wkipling/instabackup.git
cd instabackup
./scripts/setup-server.sh --data /mnt/user/MyShare/insta360-backup/sdcard
```

This creates a token, pulls `ghcr.io/wkipling/instabackup:latest`, and starts
the receiver on port 8844. It uses Docker only, not podman. No certificates are
needed for this path. The dashboard opens at `http://SERVER:8844/`.

If you prefer Compose:

```sh
mkdir -p appdata data
openssl rand -hex 24 > appdata/sd-token
docker compose up -d
```

### 2. SD card

Put the card in your PC and run:

```sh
./instabackup-tui
```

It finds the mounted card automatically. To skip the card prompt, pass the
mountpoint (for example `./instabackup-tui --sd /run/media/you/disk`). Pick
the Wi-Fi network or phone hotspot, the server's Tailscale IP
(`tailscale ip -4` on the server), and the token file printed by step 1. The
TUI writes the card, verifies it, and prints a checklist. Python 3.10+ is the
only requirement; the Wi-Fi list uses `nmcli` when available, and manual SSID
entry works without it.

### 3. Use it

Insert the card, power on the camera, and enable the hotspot. The camera
leaves its own AP and uploads. If the hotspot is unavailable, it restores its
normal AP after about 15 seconds and tries again on the next boot.

Files appear under the `--data` directory. Progress is at
`http://SERVER:8844/`.

## What the dashboard shows

- Incoming uploads with percent, speed and ETA.
- Partial files waiting to resume.
- Every completed file with size and modified time, with a path filter.

The page updates itself over `fetch()`; it does not reload. `/api/status` and
`/api/files` return the same data as JSON. These read-only routes are
unauthenticated; uploads require the token.

## Models

| Model | Status | Notes |
| --- | --- | --- |
| X4 Air 1.2.7 | **Tested** | Stock SD hook. No firmware changes. |
| X5 | Untested | Expected to use the same stock SD hook. |
| X4 1.9.21 | Untested | SD hook is disabled; needs the one-time `x4_bootstrap.py` preload. |
| other models | Unsupported | The boot hook and cloud queue are not verified. |

Only X4 Air 1.2.7 has been validated end-to-end. If you test another model,
keep a copy of the card and report what worked. Model notes:
[notes/X4-AIR-QUICKSTART.md](notes/X4-AIR-QUICKSTART.md) and
[notes/X4-QUICKSTART.md](notes/X4-QUICKSTART.md).

## How it works

1. The stock boot hook runs `bootup.sh` from the card.
2. `wifi-connect.sh` switches from the camera's AP to your Wi-Fi/hotspot.
3. `insupload` walks `/DCIM`, asks the receiver for the byte offset it already
   has, and appends only the missing bytes.
4. `tailscaled` runs in userspace mode; the uploader dials the tailnet through
   its local HTTP CONNECT proxy. No inbound ports or port forwarding.
5. The receiver writes `.part` files and resumes them. A complete file is
   renamed into place.
6. If the saved SSID is missing, `wifi-connect.sh` restores the camera's own
   AP and verifies it is beaconing. The watchdog only reconnects station mode,
   so a brief hotspot outage does not strand the camera in AP mode.

Everything lives on the SD card. Removing the card, or deleting
`wifi-sta.conf`, returns the camera to stock behaviour. Nothing on the camera's
root filesystem is changed on X4 Air.

## Manual / advanced

### Build from source

```sh
make build      # AArch64 helpers; needs Zig 0.17
make certs      # only for the paid-cloud TLS path
make test       # offline suite
make container
```

### Manual card install

```sh
cd camera
./install_to_sd.sh /run/media/$USER/SDCARD - \
    --mode tailscale --target 100.x.y.z --x4-air \
    --wifi-conf /tmp/wifi.conf \
    --upload-token-file .sd-upload.token
./verify_sd.sh /run/media/$USER/SDCARD
```

`--wifi-conf` and `--authkey-file` keep secrets out of argv. Add
`--authkey-file /tmp/tskey` on the first Tailscale login. For LAN only, use
`--mode lan --server-ip 192.168.1.10`.

### Paid-cloud replacement (advanced)

The direct uploader above does not use Insta360's cloud at all. The original
reverse-engineered path is still available: a local `inscloud_server.py` on
port 443 plus a DNS/CA redirect. It is not needed for the easy setup.

The server can generate its own certificates:

```sh
python3 server/inscloud_server.py --root ./backup --port 443 \
    --generate-certs ./certs
```

or generate them and exit:

```sh
python3 server/inscloud_server.py --generate-certs ./certs \
    --generate-certs-only
```

Copy `certs/ca.crt` and `certs/ca.hash` to the card. Full details:
[notes/ADVANCED.md](notes/ADVANCED.md) and
[analysis/PROTOCOL.md](analysis/PROTOCOL.md).

## Troubleshooting

- **Camera did not join:** check `bootup.log` on the card. A missing SSID
  restores the AP after about 15 seconds by design.
- **Nothing on the dashboard:** `docker logs insta360-sink` and
  `curl http://SERVER:8844/api/status`.
- **Upload stalls:** the receiver resumes from the stored offset on the next
  pass. Keep the camera powered.
- **Token mismatch:** `camera/.sd-upload.token` and `appdata/sd-token` must be
  identical (the receiver strips the trailing newline).
- More: [PROGRESS.md](PROGRESS.md).

## Security

- Uploads require `X-Auth-Token`; the dashboard is read-only.
- The direct path is HTTP inside the tailnet, encrypted by WireGuard. Do not
  publish port 8844 to the internet.
- Never commit `camera/certs/`, `camera/.sd-upload.token`, or
  `wifi-sta.conf`.

## Repository layout

| Path | Purpose |
| --- | --- |
| `camera/` | boot hook, Wi-Fi, uploader, installer, TUI, tests |
| `server/` | direct receiver, dashboard, fake cloud server, container |
| `scripts/` | one-command server setup |
| `tools/` | firmware/transport analysis helpers |
| `notes/` | model quick starts and long-form advanced docs |
| `PROGRESS.md` | reverse-engineering and deployment log |
