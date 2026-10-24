#!/bin/sh
# Re-establish station mode when the camera's own power management turns
# Wi-Fi off.
#
# The X4 Air stops Wi-Fi after a couple of minutes of inactivity (visible in
# the GUI), even with NO_HIBER set.  The stock scripts only need wlan0 brought
# back up and wpa_supplicant restarted, which sta_start.sh already does.
#
# This watchdog never restores AP mode: at a hotel that would just hide the
# problem behind an unreachable hotspot.  It retries station mode forever,
# with a cooldown, so an upload resumes from the server's byte offset.
set -u

SD=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
[ -n "$SD" ] || SD=/tmp/SD0
LOG="$SD/bootup.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') wifi-watchdog: $*" >> "$LOG" 2>/dev/null
}

[ -f "$SD/wifi-sta.conf" ] || exit 0

# wifi-connect.sh leaves this flag when it gave the camera back to its own
# AP because the saved SSID was not visible at boot.  In that state the user
# asked for completely normal camera behaviour, so do not cycle the radio.
AP_FALLBACK_FLAG=${WIFI_AP_FALLBACK_FLAG:-/tmp/inscloud-ap-fallback}
if [ -f "$AP_FALLBACK_FLAG" ]; then
    log "camera was returned to its own AP at boot; watchdog idle"
    exit 0
fi

IFACE=${WIFI_IFACE:-wlan0}
INTERVAL=${WIFI_WATCHDOG_INTERVAL:-20}
FAILS_BEFORE_ACTION=${WIFI_WATCHDOG_FAILS:-3}
COOLDOWN=${WIFI_WATCHDOG_COOLDOWN:-60}

log "start (iface=$IFACE interval=${INTERVAL}s)"
fails=0
last_action=0
attempts=0
ap_idle_logged=0

have_link() {
    ip -4 addr show dev "$IFACE" 2>/dev/null | grep -q 'inet ' || return 1
    ip route 2>/dev/null | grep -q '^default' || return 1
    # The GUI can stop Wi-Fi while leaving the address and wpa_cli's cached
    # COMPLETED state in place.  iw asks the driver, which reports Not
    # connected when the radio is actually off.
    iw dev "$IFACE" link 2>/dev/null | grep -q 'Connected to' || return 1
    return 0
}

while :; do
    # If hostapd is running, the camera is deliberately broadcasting its own
    # hotspot (stock AP mode).  Tearing the radio down to retry station mode
    # would fight the user and make the camera unreachable, so stay idle.
    if pidof hostapd >/dev/null 2>&1; then
        if [ "$ap_idle_logged" = "0" ]; then
            log "camera AP is active (hostapd); watchdog idle"
            ap_idle_logged=1
        fi
        fails=0
        sleep "$INTERVAL"
        continue
    fi
    ap_idle_logged=0
    if have_link; then
        if [ "$fails" -ne 0 ]; then
            log "link is back (${IFACE} associated)"
        fi
        fails=0
    else
        fails=$((fails + 1))
        now=$(date +%s 2>/dev/null || echo 0)
        if [ "$fails" -ge "$FAILS_BEFORE_ACTION" ] && \
           [ $((now - last_action)) -ge "$COOLDOWN" ]; then
            attempts=$((attempts + 1))
            last_action=$now
            log "link check failed (${fails}): $(ip -4 addr show dev "$IFACE" 2>/dev/null | grep 'inet ' | head -1) | $(ip route 2>/dev/null | grep '^default' | head -1) | $(iw dev "$IFACE" link 2>/dev/null | head -1)"
            if [ $((attempts % 3)) -eq 0 ]; then
                log "escalating: cycling $IFACE down/up"
                ifconfig "$IFACE" down 2>/dev/null
                sleep 1
                ifconfig "$IFACE" up 2>/dev/null
            fi
            log "reconnect attempt $attempts"
            # Make sure wlan0 exists; if the driver was unloaded, this
            # reloads it.  wifi_start.sh exits immediately when bcmdhd is
            # already loaded.
            if ! ip link show "$IFACE" >/dev/null 2>&1; then
                log "wlan0 missing; asking wifi_start.sh to bring it up"
                /usr/local/share/script/wifi_start.sh >>"$LOG" 2>&1
                sleep 3
            fi
            WIFI_RECONNECT=1 WIFI_FALLBACK_AP=0 WIFI_SKIP_WAIT=0 \
                sh "$SD/wifi-connect.sh" >>"$LOG" 2>&1
            rc=$?
            log "reconnect attempt $attempts rc=$rc"
            iw dev "$IFACE" set power_save off 2>/dev/null
            if [ "$rc" = "0" ]; then
                fails=0
            fi
        fi
    fi
    sleep "$INTERVAL"
done
