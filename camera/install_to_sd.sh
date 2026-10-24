#!/bin/sh
# Copy the self-hosted cloud-backup kit to a mounted camera SD card.
#
# Usage:
#   ./install_to_sd.sh <sd-mountpoint> <server-ip|-> [options]
#
# Options:
#   --mode lan|tailscale     lan (default) or tailscale remote mode
#   --target <tailnet-ip>    home server's `tailscale ip -4` (tailscale mode)
#   --authkey <key>          reusable Tailscale auth key (first login)
#   --authkey-file <file>    read that key from a file (keeps it out of argv)
#   --hostname <name>        node name in the admin console (default insta360-cam)
#   --with-wrapper           also install the instaAIP wrapper (card must be
#                            mounted exec; usually unnecessary)
#   --x4-air                 X4 Air: stock SD hook is enabled; create NO_HIBER
#                            and do not use the X4 web bootstrap
#   --x4                     X4 1.9.21: no stock SD hook; after copying, run
#                            x4_bootstrap.py once (see README)
#   --wifi-ssid <ssid>       switch to station mode and join this Wi-Fi/hotspot
#   --wifi-password <pass>   password for --wifi-ssid (empty for an open AP)
#   --wifi-conf <file>       read WIFI_SSID/WIFI_PASSWORD/WIFI_COUNTRY from a
#                            file instead of putting the password in argv
#   --wifi-country <cc>      optional country code, e.g. AU or US
#   --no-wifi                remove an existing station-mode Wi-Fi config
#   --upload-token <token>   shared secret for the direct SD->server uploader
#                            (must match the receiver's --token)
#   --upload-token-file <f>  read that secret from a file instead of argv
#   --tailscale-dir <dir>    use these tailscaled/tailscale binaries
#
# Examples:
#   ./install_to_sd.sh /run/media/$USER/SDCARD 192.168.1.10
#   ./install_to_sd.sh /run/media/$USER/SDCARD - --mode tailscale \
#       --target 100.101.102.103 --authkey tskey-auth-... --x4-air
#   ./install_to_sd.sh /run/media/$USER/SDCARD - --mode tailscale \
#       --target 100.101.102.103 --authkey tskey-auth-... --x4-air \
#       --wifi-ssid 'MyPhoneHotspot' --wifi-password 'secret123'
#
# The card must be mounted read-write.  This does not touch the camera's
# internal filesystem.  Re-run after changing certs or scripts.
set -eu

usage() {
    sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

SD=${1:-}
IP=${2:-}
[ -n "$SD" ] && [ -n "$IP" ] || usage
shift 2

MODE=lan
TARGET=
AUTHKEY=
AUTHKEY_FILE=
TS_HOSTNAME=insta360-cam
WRAPPER=0
X4=0
X4AIR=0
WIFI_SSID=
WIFI_PASSWORD=
WIFI_COUNTRY=
WIFI_CONF=
WIFI_BSSID=xx:xx:xx:xx:xx:xx
NO_WIFI=0
UPLOAD_TOKEN=
UPLOAD_TOKEN_FILE=
TS_DIR_OVERRIDE=
while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE=${2:-}; shift 2 ;;
        --target) TARGET=${2:-}; shift 2 ;;
        --authkey) AUTHKEY=${2:-}; shift 2 ;;
        --authkey-file) AUTHKEY_FILE=${2:-}; shift 2 ;;
        --hostname) TS_HOSTNAME=${2:-}; shift 2 ;;
        --wifi-ssid) WIFI_SSID=${2:-}; shift 2 ;;
        --wifi-password) WIFI_PASSWORD=${2:-}; shift 2 ;;
        --wifi-conf) WIFI_CONF=${2:-}; shift 2 ;;
        --wifi-country) WIFI_COUNTRY=${2:-}; shift 2 ;;
        --upload-token) UPLOAD_TOKEN=${2:-}; shift 2 ;;
        --upload-token-file) UPLOAD_TOKEN_FILE=${2:-}; shift 2 ;;
        --tailscale-dir) TS_DIR_OVERRIDE=${2:-}; shift 2 ;;
        --no-wifi) NO_WIFI=1; shift ;;
        --with-wrapper) WRAPPER=1; shift ;;
        --x4-air) X4AIR=1; shift ;;
        --x4) X4=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done
[ "$X4" = "1" ] && [ "$X4AIR" = "1" ] && {
    echo "--x4 and --x4-air are mutually exclusive" >&2
    exit 2
}

[ "$MODE" = "lan" ] || [ "$MODE" = "tailscale" ] || {
    echo "invalid --mode: $MODE" >&2
    exit 2
}
if [ -n "$AUTHKEY_FILE" ]; then
    [ -f "$AUTHKEY_FILE" ] || {
        echo "no such auth key file: $AUTHKEY_FILE" >&2
        exit 2
    }
    [ -z "$AUTHKEY" ] || {
        echo "use --authkey or --authkey-file, not both" >&2
        exit 2
    }
    AUTHKEY=$(sed -n '1p' "$AUTHKEY_FILE" | tr -d '\r')
fi
if [ -n "$WIFI_CONF" ]; then
    if [ -n "$WIFI_SSID" ] || [ "$NO_WIFI" = "1" ]; then
        echo "--wifi-conf cannot be combined with --wifi-ssid/--no-wifi" >&2
        exit 2
    fi
    [ -f "$WIFI_CONF" ] || { echo "no such Wi-Fi config: $WIFI_CONF" >&2; exit 1; }
    WIFI_SSID=$(sed -n 's/^WIFI_SSID=//p' "$WIFI_CONF" | tr -d '\r' | tail -n 1)
    WIFI_PASSWORD=$(sed -n 's/^WIFI_PASSWORD=//p' "$WIFI_CONF" | tr -d '\r' | tail -n 1)
    conf_country=$(sed -n 's/^WIFI_COUNTRY=//p' "$WIFI_CONF" | tr -d '\r' | tail -n 1)
    conf_bssid=$(sed -n 's/^WIFI_BSSID=//p' "$WIFI_CONF" | tr -d '\r' | tail -n 1)
    [ -n "$WIFI_COUNTRY" ] || WIFI_COUNTRY=$conf_country
    [ -n "$conf_bssid" ] && WIFI_BSSID=$conf_bssid
    [ -n "$WIFI_SSID" ] || { echo "$WIFI_CONF has no WIFI_SSID" >&2; exit 2; }
fi
if [ -n "$WIFI_PASSWORD" ] && [ -z "$WIFI_SSID" ]; then
    echo "--wifi-password requires --wifi-ssid" >&2
    exit 2
fi
case "$WIFI_SSID$WIFI_PASSWORD" in
    *'
'*) echo "Wi-Fi SSID/password must not contain a newline" >&2; exit 2 ;;
esac
case "$WIFI_SSID$WIFI_PASSWORD" in
    *'"'*|*\\*)
        echo "quotes and backslashes are not supported in Wi-Fi credentials" >&2
        exit 2
        ;;
esac
if [ "$MODE" = "tailscale" ] && [ -z "$TARGET" ]; then
    echo "tailscale mode requires --target <home-server-tailnet-ip>" >&2
    exit 2
fi
if [ -n "$UPLOAD_TOKEN_FILE" ]; then
    [ -f "$UPLOAD_TOKEN_FILE" ] || {
        echo "no such token file: $UPLOAD_TOKEN_FILE" >&2
        exit 2
    }
    UPLOAD_TOKEN=$(sed -n '1p' "$UPLOAD_TOKEN_FILE" | tr -d '\r')
fi
if [ "$MODE" = "lan" ] && [ "$IP" = "-" ]; then
    echo "lan mode requires a server IP" >&2
    exit 2
fi
if [ "$IP" = "-" ]; then
    IP=
fi

[ -d "$SD" ] || { echo "not a directory: $SD" >&2; exit 1; }
[ -w "$SD" ] || { echo "not writable: $SD" >&2; exit 1; }

# The uploader scans /tmp/SD0/DCIM.  Create it on a freshly formatted card;
# the camera normally creates it too.  X4 also needs it for the web bootstrap.
if [ "$X4" = "1" ] || [ "$X4AIR" = "1" ]; then
    mkdir -p "$SD/DCIM"
fi

HERE=$(cd "$(dirname "$0")" && pwd)

if [ ! -f "$HERE/certs/ca.crt" ] || [ ! -f "$HERE/certs/server.crt" ]; then
    echo "generating certificates${IP:+ for $IP} ..."
    (cd "$HERE" && ./make_certs.sh certs "$IP")
fi

NEEDED="bootup.sh cloud-upload-watch.sh cloudkick tailrelay diagnose.sh wifi-connect.sh wifi-watchdog.sh insupload"
if [ "$MODE" = "tailscale" ]; then
    NEEDED="$NEEDED tailscale-up.sh"
fi
for f in $NEEDED; do
    [ -f "$HERE/$f" ] || { echo "missing $HERE/$f" >&2; exit 1; }
done
for f in certs/ca.crt certs/ca.hash; do
    [ -f "$HERE/$f" ] || { echo "missing $HERE/$f" >&2; exit 1; }
done

cp "$HERE/bootup.sh" "$SD/bootup.sh"
cp "$HERE/cloud-upload-watch.sh" "$SD/cloud-upload-watch.sh"
cp "$HERE/cloudkick" "$SD/cloudkick"
cp "$HERE/tailrelay" "$SD/tailrelay"
cp "$HERE/diagnose.sh" "$SD/diagnose.sh"
cp "$HERE/wifi-connect.sh" "$SD/wifi-connect.sh"
cp "$HERE/wifi-watchdog.sh" "$SD/wifi-watchdog.sh"
cp "$HERE/insupload" "$SD/insupload"
cp "$HERE/certs/ca.crt" "$SD/ca.crt"
cp "$HERE/certs/ca.hash" "$SD/ca.hash"
chmod +x "$SD/bootup.sh" "$SD/cloud-upload-watch.sh" \
         "$SD/cloudkick" "$SD/tailrelay" "$SD/diagnose.sh" \
         "$SD/wifi-connect.sh" "$SD/wifi-watchdog.sh" "$SD/insupload"

if [ "$MODE" = "tailscale" ]; then
    cp "$HERE/tailscale-up.sh" "$SD/tailscale-up.sh"
    chmod +x "$SD/tailscale-up.sh"

    TS_SRC=""
    TS_DIRS="$HERE/tailscale $HERE/../vendor/tailscale/tailscale_1.104.2_arm64 $HERE/../vendor/tailscale/extract/tailscale_1.104.2_arm64"
    [ -n "$TS_DIR_OVERRIDE" ] && TS_DIRS="$TS_DIR_OVERRIDE $TS_DIRS"
    for d in $TS_DIRS; do
        if [ -f "$d/tailscaled" ] && [ -f "$d/tailscale" ]; then
            TS_SRC=$d
            break
        fi
    done
    if [ -n "$TS_SRC" ]; then
        mkdir -p "$SD/tailscale"
        cp "$TS_SRC/tailscaled" "$TS_SRC/tailscale" "$SD/tailscale/"
        chmod +x "$SD/tailscale/tailscaled" "$SD/tailscale/tailscale"
        echo "copied Tailscale binaries from $TS_SRC"
    else
        echo "WARNING: tailscaled/tailscale not found." >&2
        echo "  Copy them to $SD/tailscale/ manually:" >&2
        echo "  tar -xzf tailscale_*_arm64.tgz; cp tailscale_*/{tailscaled,tailscale} $SD/tailscale/" >&2
    fi
fi

# On X4 Air the stock daemon is already running when bootup.sh starts, so
# restart it after DNS + CA are installed for every transport mode.  In
# tailscale mode the same is true on all supported models.
if [ "$MODE" = "tailscale" ] || [ "$X4AIR" = "1" ]; then
    RESTART_AIP=1
else
    RESTART_AIP=0
fi

if [ "$NO_WIFI" = "1" ]; then
    rm -f "$SD/wifi-sta.conf"
    echo "removed station-mode Wi-Fi config"
elif [ -n "$WIFI_SSID" ]; then
    {
        printf 'WIFI_SSID=%s\n' "$WIFI_SSID"
        printf 'WIFI_PASSWORD=%s\n' "$WIFI_PASSWORD"
        printf 'WIFI_COUNTRY=%s\n' "$WIFI_COUNTRY"
        printf 'WIFI_BSSID=%s\n' "$WIFI_BSSID"
    } > "$SD/wifi-sta.conf"
    chmod 600 "$SD/wifi-sta.conf" 2>/dev/null || true
    echo "wrote station-mode Wi-Fi config for '$WIFI_SSID'"
    [ -n "$WIFI_PASSWORD" ] || echo "  WARNING: empty password; this is an open network"
elif [ -f "$SD/wifi-sta.conf" ]; then
    echo "keeping existing $SD/wifi-sta.conf"
else
    echo "no station-mode Wi-Fi config; camera will create its own hotspot"
fi

if [ -f "$SD/cloud-backup.conf" ]; then
    echo "keeping existing $SD/cloud-backup.conf"
    echo "  (set BACKUP_MODE=$MODE and TAILNET_TARGET=$TARGET there if needed)"
else
    sed \
        -e "s|^BACKUP_MODE=.*|BACKUP_MODE=$MODE|" \
        -e "s|^SERVER_IP=.*|SERVER_IP=$IP|" \
        -e "s|^#*TAILNET_TARGET=.*|TAILNET_TARGET=$TARGET|" \
        -e "s|^#*TAILSCALE_AUTHKEY=.*|TAILSCALE_AUTHKEY=$AUTHKEY|" \
        -e "s|^#*TAILSCALE_HOSTNAME=.*|TAILSCALE_HOSTNAME=$TS_HOSTNAME|" \
        -e "s/^RESTART_AIP=.*/RESTART_AIP=$RESTART_AIP/" \
        -e "s/^START_CLOUD_UPLOAD=.*/START_CLOUD_UPLOAD=1/" \
        -e "s/^START_SD_UPLOAD=.*/START_SD_UPLOAD=1/" \
        "$HERE/cloud-backup.conf.example" > "$SD/cloud-backup.conf"
    echo "wrote $SD/cloud-backup.conf"
fi

if [ -n "$UPLOAD_TOKEN" ]; then
    printf '%s\n' "$UPLOAD_TOKEN" > "$SD/sd-upload.token"
    chmod 600 "$SD/sd-upload.token" 2>/dev/null || true
    echo "wrote sd-upload.token"
fi

if [ "$WRAPPER" = "1" ]; then
    cp "$HERE/instaAIP-wrapper" "$SD/instaAIP"
    chmod +x "$SD/instaAIP"
    echo "installed instaAIP wrapper (card must be mounted exec)"
fi

# Expected hashes used by bootup.sh / tailscale-up.sh.  The camera has been
# seen to read newly written large files incorrectly, so every executable is
# copied to the tmpfs and verified before it is allowed to run.  Generate this
# after all binaries are final, or an upgrade records stale hashes.
(
    cd "$SD" || exit 0
    {
        if [ -f insupload ]; then sha256sum insupload; fi
        if [ -f tailscale/tailscaled ]; then sha256sum tailscale/tailscaled; fi
        if [ -f tailscale/tailscale ]; then sha256sum tailscale/tailscale; fi
        true
    } > SHA256SUMS 2>/dev/null
) || true
if [ -s "$SD/SHA256SUMS" ]; then echo "wrote SHA256SUMS"; fi

sync
EXTRA=""
if [ "$WRAPPER" = "1" ]; then
    EXTRA=", instaAIP"
fi
cat <<EOF

Done.  Files on $SD:
  bootup.sh, cloud-upload-watch.sh, cloudkick, tailrelay, insupload,
  wifi-connect.sh, wifi-watchdog.sh, ca.crt, ca.hash, cloud-backup.conf$EXTRA
EOF
if [ -f "$SD/sd-upload.token" ]; then
    echo "  sd-upload.token (direct SD backup)"
fi
if [ -f "$SD/wifi-sta.conf" ]; then
    echo "  wifi-sta.conf (station mode)"
fi

if [ "$X4" = "1" ] || [ "$X4AIR" = "1" ]; then
    : > "$SD/NO_HIBER"
    sync
    echo "created $SD/NO_HIBER (skip hibernation while the card is inserted)"
fi

if [ "$MODE" = "tailscale" ]; then
    cat <<EOF
  tailscale-up.sh, tailscale/{tailscaled,tailscale}

Mode: tailscale, target $TARGET
Next:
  1. On the home server: install Tailscale, run 'tailscale up', then
     'tailscale ip -4' (must match TAILNET_TARGET).
  2. Start inscloud_server.py on port 443. Keep --public-base at its
     default (https://service-test.insta360.com); do not point it at the
     tailnet IP, or the camera will try to route there directly.
  3. Insert the card, power the camera, connect it to Wi-Fi or a hotspot.
  4. Check $SD/bootup.log for 'tailscale-up started', 'tailnet path ready'
     and 'sending actions 870'; check the server's requests.log.
EOF
else
    cat <<EOF
Mode: lan, server $IP
Next:
  1. start inscloud_server.py on $IP (port 443, cert from camera/certs)
  2. insert the card, power the camera, connect it to the same Wi-Fi
  3. watch $SD/bootup.log (or the server's requests.log)

No app toggle is needed: START_CLOUD_UPLOAD=1 makes the boot hook send the
scan/start actions directly to instaAIP.
EOF
fi

if [ "$X4" = "1" ]; then
    cat <<EOF

X4 1.9.21 note:
  Its /etc/init.d/S99bootdone has the SD bootup.sh block commented out, so
  this card alone will not run.  Install the one-time preload hook once:

      python3 x4_bootstrap.py --camera 192.168.42.1

  Connect to the camera's own Wi-Fi AP for that install, then power-cycle.
  See README.md; this writes /usr/lib/libcloudbootstrap.so and
  /etc/ld.so.preload on the camera's writable root filesystem.
EOF
fi

if [ "$X4AIR" = "1" ]; then
    cat <<EOF

X4 Air note:
  Its /etc/init.d/S99bootdone runs /tmp/SD0/bootup.sh directly, so no
  firmware modification or web bootstrap is needed.  NO_HIBER has been
  created so the camera stays awake while charging and backing up.
EOF
fi
