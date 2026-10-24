#!/bin/sh
# Pre-flight check for a prepared camera SD card.
#
# Usage:
#   ./verify_sd.sh /run/media/$USER/SDCARD
#
# Read-only: it does not modify the card.  Run it on the PC after
# install_to_sd.sh and before inserting the card into the camera.
set -u

SD=${1:-}
if [ -z "$SD" ] || [ ! -d "$SD" ]; then
    echo "usage: $0 <sd-mountpoint>" >&2
    exit 2
fi

ERRORS=0
WARNS=0

ok()   { echo "  OK   $*"; }
warn() { echo "  WARN $*"; WARNS=$((WARNS + 1)); }
bad()  { echo "  FAIL $*"; ERRORS=$((ERRORS + 1)); }

echo "== files"
for f in bootup.sh cloud-upload-watch.sh cloudkick tailrelay wifi-connect.sh \
         wifi-watchdog.sh insupload ca.crt ca.hash cloud-backup.conf; do
    if [ ! -f "$SD/$f" ]; then
        bad "missing $f"
    elif [ ! -x "$SD/$f" ] && [ "$f" != "ca.crt" ] && [ "$f" != "ca.hash" ] \
         && [ "$f" != "cloud-backup.conf" ]; then
        warn "$f is not executable (FAT mounts may emulate this)"
    else
        ok "$f"
    fi
done

if [ ! -f "$SD/cloud-backup.conf" ]; then
    echo
    echo "Cannot continue without cloud-backup.conf"
    exit 1
fi

# shellcheck disable=SC1090
. "$SD/cloud-backup.conf"

MODE=${BACKUP_MODE:-lan}
echo
echo "== mode: $MODE"
case "$MODE" in
    lan)
        if [ -n "${SERVER_IP:-}" ]; then
            ok "SERVER_IP=$SERVER_IP"
        else
            bad "BACKUP_MODE=lan but SERVER_IP is empty"
        fi
        ;;
    tailscale)
        T=${TAILNET_TARGET:-}
        if [ -z "$T" ]; then
            bad "TAILNET_TARGET is empty"
        else
            o1=${T%%.*}
            rest=${T#*.}
            o2=${rest%%.*}
            case "$o1.$o2" in
                100.6[4-9]|100.[7-9]?|100.1[0-1]?|100.12[0-7])
                    ok "TAILNET_TARGET=$T (looks like a Tailscale 100.64/10 address)"
                    ;;
                *)
                    warn "TAILNET_TARGET=$T is outside 100.64.0.0/10;"
                    warn "confirm it is the home server's 'tailscale ip -4'"
                    ;;
            esac
        fi
        if [ -x "$SD/tailscale/tailscaled" ] && [ -x "$SD/tailscale/tailscale" ]; then
            ARCH=$(file "$SD/tailscale/tailscaled" 2>/dev/null)
            case "$ARCH" in
                *ARM\ aarch64*) ok "tailscale binaries are ARM64" ;;
                *) bad "tailscale/tailscaled is not an ARM64 binary" ;;
            esac
        else
            bad "tailscale/{tailscaled,tailscale} missing or not executable"
        fi
        if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
            ok "TAILSCALE_AUTHKEY is set (first login)"
        elif [ -f "$SD/tailscale/tailscaled.state" ]; then
            ok "saved tailscaled.state present (no auth key needed)"
        else
            bad "neither TAILSCALE_AUTHKEY nor tailscale/tailscaled.state"
        fi
        if [ "${CLOUD_RELAY_PORT:-443}" != "443" ]; then
            bad "CLOUD_RELAY_PORT must be 443 (the camera cannot use another)"
        fi
        ;;
    *)
        bad "unknown BACKUP_MODE=$MODE"
        ;;
esac

echo
echo "== station-mode Wi-Fi"
WIFI_FILE="$SD/wifi-sta.conf"
if [ -f "$WIFI_FILE" ]; then
    WSSID=$(sed -n 's/^WIFI_SSID=//p' "$WIFI_FILE" 2>/dev/null | tr -d '\r' | tail -n 1)
    WPASS=$(sed -n 's/^WIFI_PASSWORD=//p' "$WIFI_FILE" 2>/dev/null | tr -d '\r' | tail -n 1)
    WCOUNTRY=$(sed -n 's/^WIFI_COUNTRY=//p' "$WIFI_FILE" 2>/dev/null | tr -d '\r' | tail -n 1)
    if [ -n "$WSSID" ]; then
        ok "WIFI_SSID=$WSSID"
    else
        bad "wifi-sta.conf has no WIFI_SSID"
    fi
    if [ -n "$WPASS" ]; then
        ok "WIFI_PASSWORD is set"
    else
        warn "WIFI_PASSWORD is empty (open network, or add a password)"
    fi
    if [ -n "$WCOUNTRY" ]; then
        ok "WIFI_COUNTRY=$WCOUNTRY"
    fi
    if [ -f "$SD/wifi-connect.sh" ]; then
        ok "wifi-connect.sh present (camera will leave AP mode at boot)"
    else
        bad "wifi-sta.conf present but wifi-connect.sh is missing"
    fi
    case "$WSSID$WPASS" in
        *'"'*|*\\*)
            warn "quotes/backslashes in SSID/password may break stock wpa_supplicant"
            ;;
    esac
else
    ok "no wifi-sta.conf: camera keeps its own hotspot / app-managed Wi-Fi"
fi

if [ "${SERVER_PORT:-443}" != "443" ]; then
    warn "SERVER_PORT=${SERVER_PORT} (camera API is always 443; this is the relay target port)"
fi
if [ "${START_CLOUD_UPLOAD:-0}" != "1" ] && \
   [ "${START_SD_UPLOAD:-0}" != "1" ]; then
    warn "START_CLOUD_UPLOAD is not 1: the app-free trigger will not run"
fi
if [ -f "$SD/NO_HIBER" ]; then
    ok "NO_HIBER present (unattended upload will not hibernate)"
else
    warn "NO_HIBER missing: camera may hibernate during an unattended upload"
fi
if [ "${CLOUD_UPLOAD_ACTIONS:-870}" = "870" ]; then
    ok "CLOUD_UPLOAD_ACTIONS=870 (sync list, scan, start)"
fi

echo
echo "== direct SD uploader"
if [ "${START_SD_UPLOAD:-0}" = "1" ]; then
    ok "START_SD_UPLOAD=1 (independent of instaAIP)"
    ok "receiver port ${SD_UPLOAD_PORT:-8844}"
    if [ -f "$SD/sd-upload.token" ]; then
        ok "sd-upload.token present"
    else
        warn "no sd-upload.token: the receiver must run without --token"
    fi
    if [ "$MODE" = "tailscale" ]; then
        ok "uploader will dial through the tailscaled CONNECT proxy"
    fi
else
    warn "START_SD_UPLOAD is not 1: direct SD backup will not run"
fi

echo
echo "== binary architecture"
if [ -f "$SD/cloudkick" ]; then
    file "$SD/cloudkick" 2>/dev/null | grep -q 'ARM aarch64' \
        && ok "cloudkick is ARM64" || bad "cloudkick is not ARM64"
fi
if [ -f "$SD/tailrelay" ]; then
    file "$SD/tailrelay" 2>/dev/null | grep -q 'ARM aarch64' \
        && ok "tailrelay is ARM64" || bad "tailrelay is not ARM64"
fi
if [ -f "$SD/insupload" ]; then
    file "$SD/insupload" 2>/dev/null | grep -q 'ARM aarch64' \
        && ok "insupload is ARM64" || bad "insupload is not ARM64"
fi

echo
echo "== CA"
if [ -f "$SD/ca.crt" ]; then
    if command -v openssl >/dev/null 2>&1; then
        openssl x509 -in "$SD/ca.crt" -noout -subject -startdate -enddate \
            -checkend 0 2>/dev/null | sed 's/^/  /'
        if openssl x509 -in "$SD/ca.crt" -noout -checkend 0 >/dev/null 2>&1; then
            ok "CA is currently valid"
        else
            bad "CA is expired or not yet valid (check camera clock)"
        fi
        # The camera checks this CA's extensions.
        if openssl x509 -in "$SD/ca.crt" -noout -text 2>/dev/null | \
           grep -q 'CA:TRUE'; then
            ok "CA has basicConstraints CA:TRUE"
        else
            bad "CA lacks basicConstraints CA:TRUE"
        fi
    else
        warn "openssl not available; cannot check ca.crt"
    fi
fi

echo
echo "== hostname redirection"
if [ "$MODE" = "tailscale" ]; then
    ok "expect dnsmasq to answer service-test.insta360.com -> 127.0.0.1"
else
    ok "expect dnsmasq to answer service-test.insta360.com -> ${SERVER_IP:-?}"
fi

echo
if [ "$ERRORS" -eq 0 ]; then
    echo "RESULT: ready ($WARNS warning(s))"
    exit 0
fi
echo "RESULT: $ERRORS error(s), $WARNS warning(s)"
exit 1
