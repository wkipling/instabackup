#!/bin/sh
# Switch the camera from its own AP to station mode and join a Wi-Fi network.
#
# This is optional.  It runs only when the SD card contains wifi-sta.conf with
# a non-empty WIFI_SSID.  The stock firmware already has the station-mode
# machinery; this wrapper supplies the SSID/password and fixes two ordering
# problems in the stock scripts:
#
#   1. The stock AP runs on wlan1.  change_mode.sh kills hostapd but leaves
#      wlan1 up.  This Broadcom driver cannot scan while the AP interface is
#      holding the radio, so every scan came back empty.  We bring wlan1 down
#      first.
#   2. The stock AP profile may not exist in /pref yet when S99 runs.  We wait
#      for the AP, then save /pref/wifi.conf and /tmp/hostapd.conf so a failed
#      join can restore the camera's own hotspot.
#
# wifi-sta.conf format (values are raw data, not shell code):
#   WIFI_SSID=MyPhoneHotspot
#   WIFI_PASSWORD=secret123
#   WIFI_COUNTRY=AU            # optional; stock default is CN
#   WIFI_BSSID=xx:xx:xx:xx:xx:xx  # optional
#
# The camera cannot be in AP and station mode at the same time.  When this
# succeeds, the camera's own hotspot disappears and the app disconnects.
set -u

SD=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
[ -n "$SD" ] || SD=/tmp/SD0
LOG="$SD/bootup.log"
WIFI_FILE="$SD/wifi-sta.conf"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') wifi: $*" >> "$LOG" 2>/dev/null
}

# Read one value without sourcing the file (so a password can never be
# executed as shell code).
wifi_value() {
    [ -f "$WIFI_FILE" ] || return 0
    sed -n "s/^$1=//p" "$WIFI_FILE" 2>/dev/null | tr -d '\r' | tail -n 1
}

SSID=$(wifi_value WIFI_SSID)
PASSWORD=$(wifi_value WIFI_PASSWORD)
COUNTRY=$(wifi_value WIFI_COUNTRY)
BSSID=$(wifi_value WIFI_BSSID)
BSSID=${BSSID:-xx:xx:xx:xx:xx:xx}

if [ -z "$SSID" ]; then
    log "no WIFI_SSID in wifi-sta.conf; keeping AP mode"
    exit 0
fi
case "$SSID$PASSWORD" in
    *'
'*)
        log "ERROR: WIFI_SSID/WIFI_PASSWORD contains a newline"
        exit 1
        ;;
esac
case "$SSID$PASSWORD" in
    *'"'*|*\\*)
        log "ERROR: quotes and backslashes are not supported by the stock wpa_supplicant parser"
        exit 1
        ;;
esac

CHANGE_MODE=${WIFI_CHANGE_MODE:-/usr/local/share/script/change_mode.sh}
STA_FAST=${WIFI_START_FAST:-/usr/local/share/script/sta_start_fast.sh}
STA_CONNECT=${WIFI_CONNECT:-/usr/local/share/script/sta_connect.sh}
AP_START=${WIFI_AP_START:-/usr/local/share/script/ap_start.sh}
IFACE=${WIFI_IFACE:-wlan0}
AP_IFACE=${WIFI_AP_IFACE:-wlan1}
WAIT=${WIFI_WAIT:-90}
AP_WAIT=${WIFI_AP_WAIT:-25}
CHANGE_TIMEOUT=${WIFI_CHANGE_TIMEOUT:-75}
SKIP_WAIT=${WIFI_SKIP_WAIT:-0}
FALLBACK_AP=${WIFI_FALLBACK_AP:-1}
RECONNECT=${WIFI_RECONNECT:-0}
# The stock sta_connect.sh scans three times before giving up and exits 11
# when the SSID is not visible; that is the ~15 s the user asked for.  A
# boot-time miss restores the camera's own AP.  A watchdog reconnect must
# NOT do that: a brief hotspot outage would otherwise take the camera off
# the network and stop the backup for the rest of the boot.
MISSING_SSID_FALLBACK=${WIFI_MISSING_SSID_FALLBACK:-1}
AP_RESTORE_TIMEOUT=${WIFI_AP_RESTORE_TIMEOUT:-30}
AP_FALLBACK_FLAG=${WIFI_AP_FALLBACK_FLAG:-/tmp/inscloud-ap-fallback}
# A watchdog reconnect happens after the GUI has already stopped Wi-Fi.
# There is no AP to wait for or save, and falling back to AP would fight the
# user's own Wi-Fi toggle, so retry station mode instead.
[ "$RECONNECT" = "1" ] && FALLBACK_AP=0
TMP_CONF=${WIFI_CONF_OUT:-/tmp/wifi.conf}
PREF_DIR=${WIFI_PREF_DIR:-/pref}
AP_BACKUP=/tmp/inscloud-wifi-ap.conf
HOSTAPD_BACKUP=/tmp/inscloud-hostapd.conf
WLOG=/tmp/inscloud-wifi.log

have() {
    command -v "$1" >/dev/null 2>&1
}

# The AP uses wlan1 with 192.168.42.1/24.  Remove its address and connected
# route before station DHCP runs: if the joined network is also
# 192.168.42.0/24 (as this home LAN is), udhcpc cannot install a default route
# while 192.168.42.1 still resolves out the AP interface.
drop_ap_address() {
    for ifc in "$IFACE" "$AP_IFACE"; do
        if ip -4 addr show dev "$ifc" 2>/dev/null | \
           grep -q 'inet 192\.168\.42\.1/'; then
            ip addr del 192.168.42.1/24 dev "$ifc" 2>/dev/null && \
                log "removed AP address 192.168.42.1/24 from $ifc"
            ip route del 192.168.42.0/24 dev "$ifc" 2>/dev/null && \
                log "removed stale AP route on $ifc"
            ip route del default dev "$ifc" 2>/dev/null
        fi
    done
}

# Run a command with a hard deadline.  change_mode.sh has no internal
# timeout and a wedged wpa_cli/hostapd start would otherwise block S99.
run_bounded() {
    limit=$1
    label=$2
    shift 2
    "$@" >>"$WLOG" 2>&1 &
    pid=$!
    n=0
    while kill -0 "$pid" 2>/dev/null && [ "$n" -lt "$limit" ]; do
        sleep 1
        n=$((n + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        log "$label still running after ${limit}s; killing it"
        kill "$pid" 2>/dev/null
        sleep 1
        kill -9 "$pid" 2>/dev/null
        return 124
    fi
    wait "$pid"
    return $?
}

mark_ap_fallback() {
    : > "$AP_FALLBACK_FLAG" 2>/dev/null || true
}

ap_address_up() {
    for ifc in "$AP_IFACE" "$IFACE"; do
        if ip -4 addr show dev "$ifc" 2>/dev/null | \
           grep -q 'inet 192\.168\.42\.1/'; then
            return 0
        fi
    done
    return 1
}

# The address alone is not enough: change_ap_mode() can assign it before the
# driver is actually beaconing.  Ask the driver for the interface type.
ap_beacon_up() {
    have iw || return 0
    for ifc in "$AP_IFACE" "$IFACE"; do
        if iw dev "$ifc" info 2>/dev/null | grep -q 'type AP'; then
            return 0
        fi
    done
    return 1
}

restore_ap() {
    restore_rc=1
    # Preferred: put the original /pref profile back and let the stock script
    # start the exact same AP, with its original SSID, password, address and
    # DHCP range.
    if [ -f "$AP_BACKUP" ] && [ -f "$CHANGE_MODE" ]; then
        log "restoring the camera's own AP from $AP_BACKUP"
        cp "$AP_BACKUP" "$TMP_CONF" 2>/dev/null
        sed -i 's/^WIFI_MODE=.*/WIFI_MODE=ap/' "$TMP_CONF" 2>/dev/null
        run_bounded "$AP_RESTORE_TIMEOUT" "change_mode.sh (AP)" \
            sh "$CHANGE_MODE" "$COUNTRY"
        restore_rc=$?
        log "AP restore rc=$restore_rc"
        # change_ap_mode() returns 0 even when the interface never got its
        # address (the driver can still be settling).  Check the result.
        n=0
        while [ "$n" -lt 10 ] && ! ap_address_up; do
            sleep 1
            n=$((n + 1))
        done
        if ap_address_up && ap_beacon_up; then
            log "camera AP is up and beaconing ($((n + 1))s after restore)"
            restore_rc=0
        else
            log "WARN: camera AP did not come up (address=$(ap_address_up && echo yes || echo no) beacon=$(ap_beacon_up && echo yes || echo no)); trying hostapd"
            restore_rc=1
        fi
    fi
    if [ "$restore_rc" -eq 0 ]; then
        mark_ap_fallback
        return 0
    fi
    # Fallback: hostapd's own config is enough to bring the beacon and DHCP
    # back, even if /pref/wifi.conf was never written.
    if [ -f "$HOSTAPD_BACKUP" ] && have hostapd; then
        log "restoring camera AP from $HOSTAPD_BACKUP"
        ifconfig "$AP_IFACE" 192.168.42.1 up 2>/dev/null
        hostapd -B "$HOSTAPD_BACKUP" >>"$WLOG" 2>&1
        restore_rc=$?
        if ! pidof dnsmasq >/dev/null 2>&1; then
            dnsmasq --user=root --nodns -5 -K -R -n \
                --dhcp-range=192.168.42.2,192.168.42.6,infinite \
                >>"$WLOG" 2>&1
        fi
        hostapd_cli -i"$AP_IFACE" -B \
            -a /usr/local/share/script/hostapd_event.sh \
            >>"$WLOG" 2>&1 &
        n=0
        while [ "$n" -lt 5 ] && ! ap_beacon_up; do
            sleep 1
            n=$((n + 1))
        done
        if ap_beacon_up; then
            log "AP hostapd restore rc=$restore_rc; beaconing"
        else
            log "WARN: hostapd restore rc=$restore_rc but no AP beacon"
            restore_rc=1
        fi
        [ "$restore_rc" -eq 0 ] && mark_ap_fallback
        return "$restore_rc"
    fi
    log "ERROR: no saved AP profile/config; remove the card to recover"
    return 1
}

# Wait for the stock AP to finish coming up.  S99 can run before ap_start.sh
# has written /pref/wifi.conf, and switching too early leaves the AP half-up.
if [ "$RECONNECT" != "1" ]; then
i=0
ap_ready=0
while [ "$i" -lt "$AP_WAIT" ]; do
    if ip -4 addr show dev "$AP_IFACE" 2>/dev/null | \
       grep -q 'inet 192\.168\.42\.1/'; then
        # The address is assigned before hostapd starts.  Wait for the AP
        # daemon too, otherwise hostapd can start and reclaim wlan1 while we
        # are scanning on wlan0.
        if pidof hostapd >/dev/null 2>&1 || \
           pidof wpa_supplicant >/dev/null 2>&1; then
            log "stock AP is up on $AP_IFACE"
            ap_ready=1
            break
        fi
    fi
    sleep 1
    i=$((i + 1))
done
[ "$ap_ready" = "1" ] || \
    log "WARN: stock AP daemon did not appear within ${AP_WAIT}s; continuing"

# Save both possible AP configurations.  /pref/wifi.conf is the stock
# high-level profile; /tmp/hostapd.conf is what hostapd actually loaded.
if [ -f "$PREF_DIR/wifi.conf" ]; then
    cp "$PREF_DIR/wifi.conf" "$AP_BACKUP" 2>/dev/null && \
        log "saved stock AP profile"
else
    log "WARN: no $PREF_DIR/wifi.conf to save"
fi
if [ -f /tmp/hostapd.conf ]; then
    cp /tmp/hostapd.conf "$HOSTAPD_BACKUP" 2>/dev/null && \
        log "saved hostapd AP config"
fi
fi  # !RECONNECT

umask 077
{
    printf 'WIFI_MODE=sta\n'
    printf 'ESSID=%s\n' "$SSID"
    printf 'PASSWORD=%s\n' "$PASSWORD"
    printf 'BSSID=%s\n' "$BSSID"
} > "$TMP_CONF" || exit 1
chmod 600 "$TMP_CONF" 2>/dev/null || true

# change_mode.sh copies /tmp/wifi.conf to /pref/wifi.conf and switches mode.
mkdir -p "$PREF_DIR" 2>/dev/null || true

# Do NOT send the stock script output to the SD card: it logs the generated
# wpa_supplicant.conf, including the PSK.  Keep it in tmpfs instead.
: > "$WLOG" 2>/dev/null || true
log "joining SSID '$SSID' in station mode"

# The AP's wlan1 must be down before wlan0 can scan.  ap_stop.sh does exactly
# this; change_mode.sh does not.  Stop the AP daemons first so hostapd cannot
# immediately bring the interface back while we scan.
killall hostapd hostapd_cli hostapd_event.sh dnsmasq 2>/dev/null
sleep 0.5
if ip link show "$AP_IFACE" >/dev/null 2>&1; then
    ifconfig "$AP_IFACE" down 2>/dev/null
    log "brought $AP_IFACE down before scanning"
fi
# Do this before any udhcpc run, not after it.
drop_ap_address

try_sta_connect() {
    [ -f "$STA_FAST" ] && [ -f "$STA_CONNECT" ] || return 1
    log "using sta_start_fast.sh + sta_connect.sh"
    rm -f /tmp/wifi_scan_list.txt
    sh "$STA_FAST" "$COUNTRY" >>"$WLOG" 2>&1
    fast_rc=$?
    if [ "$fast_rc" -ne 0 ]; then
        log "sta_start_fast.sh rc=$fast_rc"
        return "$fast_rc"
    fi

    # Do not scan separately here: sta_connect.sh performs the stock scan
    # (three passes, about 15 s) and exits 11 when the SSID is absent, which
    # is handled below.  Scanning twice only slowed boot down.

    sh "$STA_CONNECT" >>"$WLOG" 2>&1
    connect_rc=$?
    # sta_connect.sh exits 2 after a successful DHCP lease (wait_ip_done).
    [ "$connect_rc" -eq 2 ] && connect_rc=0
    log "sta_connect.sh rc=$connect_rc"
    # Exit 11 is the stock "SSID not found after scanning" result (three
    # scan passes, about 15 s).  Put the camera back exactly as stock: its
    # own AP, original SSID/password and 192.168.42.0/24 DHCP.  Only do this
    # at boot; the watchdog keeps retrying station mode instead.
    if [ "$connect_rc" -eq 11 ] && [ "$RECONNECT" != "1" ] && \
       [ "$MISSING_SSID_FALLBACK" = "1" ]; then
        log "SSID '$SSID' not visible; returning the camera to its normal AP"
        restore_ap
        exit 3
    fi
    return "$connect_rc"
}

# change_mode.sh has no internal timeout around wpa_cli -A.  Run it in the
# background and kill a hung scan instead of blocking the boot forever.
run_change_mode() {
    [ -f "$CHANGE_MODE" ] || return 1
    sh "$CHANGE_MODE" "$COUNTRY" >>"$WLOG" 2>&1 &
    cm=$!
    n=0
    while kill -0 "$cm" 2>/dev/null && [ "$n" -lt "$CHANGE_TIMEOUT" ]; do
        sleep 1
        n=$((n + 1))
    done
    if kill -0 "$cm" 2>/dev/null; then
        log "change_mode.sh still running after ${CHANGE_TIMEOUT}s; killing it"
        kill "$cm" 2>/dev/null
        sleep 1
        kill -9 "$cm" 2>/dev/null
        killall wpa_cli wpa_event.sh 2>/dev/null
        return 124
    fi
    wait "$cm"
    return $?
}

rc=1
if [ -f "$STA_FAST" ] && [ -f "$STA_CONNECT" ]; then
    # Prefer the newer stock path: bounded scans and WPA3/SAE support.
    try_sta_connect
    rc=$?
else
    log "sta_start_fast.sh/sta_connect.sh not found"
fi

if ! ip route 2>/dev/null | grep -q '^default' && [ -f "$CHANGE_MODE" ]; then
    log "still no route; trying change_mode.sh"
    run_change_mode
    rc=$?
fi
log "station-mode script rc=$rc"

# If association completed but DHCP was slow, ask once more.  sta_connect.sh
# and change_mode.sh both start udhcpc with -b, so a lease may still arrive.
if ! ip route 2>/dev/null | grep -q '^default' && have wpa_cli; then
    if wpa_cli -i "$IFACE" status 2>/dev/null | grep -q 'wpa_state=COMPLETED'; then
        log "wlan0 is associated but has no default route; retrying DHCP"
        udhcpc -i "$IFACE" -A 1 -b >>"$WLOG" 2>&1
    fi
fi

# Clean up again in case a stock script re-added the AP address.
drop_ap_address

[ "$SKIP_WAIT" = "1" ] && exit "$rc"

i=0
while [ "$i" -lt "$WAIT" ]; do
    if ip route 2>/dev/null | grep -q '^default'; then
        ip4=$(ip -4 addr show dev "$IFACE" 2>/dev/null | \
            awk '/inet / {print $2; exit}')
        log "station mode ready (${ip4:-no IPv4}); default route present"
        # Power save makes Broadcom throughput bursty and can trigger
        # disconnects.  The uploader needs a steady link.
        iw dev "$IFACE" set power_save off 2>/dev/null && \
            log "disabled Wi-Fi power save on $IFACE"
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done

log "ERROR: no default route after ${WAIT}s; see /tmp/inscloud-wifi.log via debug shell"
log "hint: enable START_TELNETD=1 in cloud-backup.conf to inspect wlan0/wpa_cli"

if [ "$FALLBACK_AP" = "1" ]; then
    restore_ap
fi
exit 1
