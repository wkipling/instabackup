#!/bin/sh
# Insta360 camera -> self-hosted backup redirect.
#
# Put this file together with `ca.crt` (and ideally `ca.hash`) plus
# `cloud-backup.conf` in the root of the SD card used by the camera.  The
# stock firmware runs /tmp/SD0/bootup.sh as root on every boot (see
# /etc/init.d/S99bootdone), so no firmware modification is required.
#
# What it does (runtime only, nothing is written to the root filesystem):
#   1. redirects the Insta360 cloud hostnames to the backup target via a local
#      DNS responder for 8.8.8.8 (libcurl in the SDK is built with c-ares and
#      instaAIP sets CURLOPT_DNS_SERVERS=8.8.8.8); a watchdog restarts it if a
#      stock Wi-Fi script runs `killall dnsmasq`;
#   2. bind-mounts a copy of the system CA bundle that also trusts ca.crt
#      (directory overlay so both CAINFO and CAPATH work when ca.hash exists);
#   3. in BACKUP_MODE=tailscale, starts tailscaled in userspace mode plus a
#      local CONNECT relay, so the camera can back up from any Wi-Fi/hotspot
#      without port forwarding (see tailscale-up.sh / tailrelay.c);
#   4. optionally restarts instaAIP / starts a debug telnet shell.
#
# In BACKUP_MODE=lan the cloud hostnames point at SERVER_IP directly, exactly
# as before.  In tailscale mode they point at 127.0.0.1 and the relay tunnels
# through the tailnet to TAILNET_TARGET.
#
# Everything disappears on reboot or when the SD card is removed.

SD=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
[ -n "$SD" ] || SD=/tmp/SD0
LOG="$SD/bootup.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null
}

log "=== cloud-backup bootup.sh start (sd=$SD) ==="

hash_of() {
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

expected_for() {
    awk -v p="$1" '$2 == p { print $1; exit }' "$SD/SHA256SUMS" 2>/dev/null
}

# True when FILE matches the hash recorded on the card at install time.
# An empty expected hash means SHA256SUMS is missing: accept the file but
# say so, so an old card still boots.
file_ok() {
    want=$(expected_for "$1")
    [ -z "$want" ] && return 0
    [ "$(hash_of "$2")" = "$want" ]
}

CONF="$SD/cloud-backup.conf"
if [ -f "$CONF" ]; then
    . "$CONF"
    log "loaded config"
else
    log "ERROR: $CONF missing"
    exit 1
fi

if [ -z "${BACKUP_MODE:-}" ]; then
    BACKUP_MODE=lan
fi

# The paid-cloud fake service (DNS redirect + tailrelay) is not needed by the
# direct uploader.  Leaving it on makes the camera's own connectivity check
# resolve Insta360 hosts to localhost; when that check fails the camera can
# decide the network is broken and stop Wi-Fi.  Off by default now.
DISABLE_CLOUD_REDIRECT=${DISABLE_CLOUD_REDIRECT:-1}

# ---------------------------------------------------------------------------
# 0. Optional card integrity check (runs before the radio is touched)
# ---------------------------------------------------------------------------
# The camera's exFAT driver has produced bad reads for files written by a PC.
# This records its own view of sizes and hashes so a failed boot can be
# diagnosed from the card alone.
if [ "${CARD_CHECK:-0}" = "1" ]; then
    (
        echo "=== cardcheck $(date) ==="
        ls -l "$SD/insupload" "$SD/tailscale/tailscaled" \
              "$SD/tailscale/tailscale" 2>&1
        df -h /tmp "$SD" 2>&1
        for f in insupload bootup.sh tailscale/tailscaled tailscale/tailscale; do
            want=$(expected_for "$f")
            got=$(hash_of "$SD/$f")
            if [ -n "$want" ] && [ "$got" = "$want" ]; then
                echo "OK   $f"
            else
                echo "BAD  $f want=$want got=$got"
            fi
        done
        echo "--- mounts"
        mount 2>&1 | grep -E 'SD0|mmc|vfat|exfat'
        echo "--- dmesg"
        dmesg 2>&1 | grep -i -E 'mmc|exfat|i/o error' | tail -20
    ) >> "$SD/cardcheck.log" 2>&1 &
fi
case "$BACKUP_MODE" in
    lan)
        if [ -z "${SERVER_IP:-}" ]; then
            log "ERROR: BACKUP_MODE=lan but SERVER_IP is empty"
            exit 1
        fi
        DNS_TARGET=$SERVER_IP
        ;;
    tailscale)
        if [ -z "${TAILNET_TARGET:-}" ]; then
            log "ERROR: BACKUP_MODE=tailscale but TAILNET_TARGET is empty"
            log "       (run 'tailscale ip -4' on the home server)"
            exit 1
        fi
        DNS_TARGET=127.0.0.1
        ;;
    *)
        log "ERROR: unknown BACKUP_MODE=$BACKUP_MODE (use lan or tailscale)"
        exit 1
        ;;
esac
SERVER_PORT=${SERVER_PORT:-443}
log "mode=$BACKUP_MODE server=${SERVER_IP:-none} target=${TAILNET_TARGET:-$SERVER_IP} port=$SERVER_PORT dns=$DNS_TARGET"
if [ "$SERVER_PORT" != "443" ]; then
    log "WARN: the camera always calls the API on HTTPS port 443;" \
        "SERVER_PORT=$SERVER_PORT is only the relay's upstream port"
fi

# ---------------------------------------------------------------------------
# 0. Optional: switch from the camera's own AP to station mode
# ---------------------------------------------------------------------------
# The camera normally creates its own hotspot.  If wifi-sta.conf exists, the
# stock firmware's station-mode scripts join a real network instead.  This
# must happen before dnsmasq/tailscale setup because change_mode.sh kills
# hostapd/dnsmasq and obtains a DHCP lease on wlan0.
#
# NETWORK_MODE is "station" after a successful join, "ap_fallback" when the
# saved SSID was not visible (wifi-connect.sh restored the stock AP and
# returned 3), and "degraded" for any other join failure.
STATION_CONFIGURED=0
NETWORK_MODE=ap
if [ -f "$SD/wifi-sta.conf" ] && grep -q '^WIFI_SSID=.' "$SD/wifi-sta.conf"; then
    STATION_CONFIGURED=1
    if [ -f "$SD/wifi-connect.sh" ]; then
        log "station mode requested by wifi-sta.conf"
        sh "$SD/wifi-connect.sh" </dev/null >/dev/null 2>&1
        WIFI_RC=$?
        log "station mode rc=$WIFI_RC"
        case "$WIFI_RC" in
            0) NETWORK_MODE=station ;;
            3) NETWORK_MODE=ap_fallback
               log "saved SSID was not visible; camera is back in its" \
                   "normal AP mode; cloud backup skipped this boot" ;;
            *) NETWORK_MODE=degraded ;;
        esac
    else
        log "ERROR: wifi-sta.conf present but wifi-connect.sh missing"
    fi
else
    log "station mode not configured; camera AP mode unchanged"
fi

# The camera turns Wi-Fi off after a couple of minutes of inactivity, which
# kills an upload mid-file.  The watchdog re-runs station mode (without the
# AP wait/fallback) whenever wlan0 loses its address.
if [ "$STATION_CONFIGURED" = "1" ] && \
   [ "$NETWORK_MODE" != "ap_fallback" ] && \
   [ "${START_WIFI_WATCHDOG:-1}" = "1" ] && [ -f "$SD/wifi-watchdog.sh" ]; then
    sh "$SD/wifi-watchdog.sh" </dev/null >/dev/null 2>&1 &
    log "wifi watchdog started (pid $!)"
fi

# ---------------------------------------------------------------------------
# 1. Name resolution
# ---------------------------------------------------------------------------

if [ "$DISABLE_CLOUD_REDIRECT" != "1" ]; then

# c-ares in the SDK is told to use 8.8.8.8.  Make that address local and run a
# tiny dnsmasq instance on it.  The camera's AP-mode dnsmasq binds UDP/TCP 53
# on 0.0.0.0, so we must NOT start ours until station mode has replaced the AP
# (or the stock script has killed its own dnsmasq).  Starting too early causes
# an endless "Address already in use" loop; the watchdog below waits for a
# default route and only then takes the port.
if ! ip addr show dev lo 2>/dev/null | grep -q "8.8.8.8"; then
    ip addr add 8.8.8.8/32 dev lo 2>>"$LOG"
    log "added 8.8.8.8/32 to lo (rc=$?)"
else
    log "8.8.8.8 already on lo"
fi

DNSMASQ=/usr/sbin/dnsmasq
DNS_PID=/tmp/inscloud-dns.pid
DNS_LOG=/tmp/inscloud-dns.log
DNS_WATCH_PID=/tmp/inscloud-dns-watchdog.pid
DNS_PROBE=inscloud-probe.insta360.com
DNS_UPSTREAM_ARGS=""

build_upstreams() {
    # Only used for names other than the redirected hosts.  The uploader only
    # asks for the two Insta360 hosts, so a failure here is not fatal.
    DNS_UPSTREAM_ARGS=""
    if [ -f /etc/resolv.conf ]; then
        for ns in $(awk '/^[[:space:]]*nameserver[[:space:]]/ {print $2}' \
                    /etc/resolv.conf 2>/dev/null); do
            case "$ns" in
                127.*|8.8.8.8) continue ;;
            esac
            case " $DNS_UPSTREAM_ARGS " in
                *" --server=$ns "*) ;;
                *) DNS_UPSTREAM_ARGS="$DNS_UPSTREAM_ARGS --server=$ns" ;;
            esac
        done
    fi
    [ -n "$DNS_UPSTREAM_ARGS" ] || \
        DNS_UPSTREAM_ARGS="--server=1.1.1.1 --server=8.8.4.4"
}

dns_running() {
    pid=$(cat "$DNS_PID" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

dns_ok() {
    # Query through the exact path the SDK uses.  The probe name is only in
    # our dnsmasq, never in /etc/hosts, so this cannot be a false positive.
    if command -v nslookup >/dev/null 2>&1; then
        if command -v timeout >/dev/null 2>&1; then
            out=$(timeout 3 nslookup "$DNS_PROBE" 8.8.8.8 2>/dev/null)
        else
            out=$(nslookup "$DNS_PROBE" 8.8.8.8 2>/dev/null)
        fi
        echo "$out" | grep -q "^Address:[[:space:]]*$DNS_TARGET" && return 0
        return 1
    fi
    dns_running
}

start_dns() {
    [ -x "$DNSMASQ" ] || return 1
    build_upstreams
    if dns_running; then
        kill "$(cat "$DNS_PID")" 2>/dev/null
        sleep 0.3
    fi
    rm -f "$DNS_PID"
    # --keep-in-foreground keeps $! as the real dnsmasq PID.  The old daemon
    # mode wrote a PID that did not always match the process holding the
    # socket, which made the watchdog start duplicates.
    # shellcheck disable=SC2086
    "$DNSMASQ" -u root --keep-in-foreground --pid-file="$DNS_PID" \
        --listen-address=8.8.8.8 --bind-interfaces --port=53 \
        --no-resolv --no-hosts --no-poll $DNS_UPSTREAM_ARGS \
        --address=/service-test.insta360.com/$DNS_TARGET \
        --address=/cloud.insta360.com/$DNS_TARGET \
        --address=/$DNS_PROBE/$DNS_TARGET \
        --local=/service-test.insta360.com/ \
        --local=/cloud.insta360.com/ \
        --local=/$DNS_PROBE/ \
        >>"$DNS_LOG" 2>&1 &
    echo $! > "$DNS_PID"
    sleep 0.4
    if dns_running; then
        return 0
    fi
    return 1
}

ensure_dns() {
    dns_ok && return 0
    if dns_running; then
        # It may still be starting.  Give it one chance before replacing it.
        sleep 1
        dns_ok && return 0
    fi
    if start_dns && sleep 1 && dns_ok; then
        log "dns redirect ready: insta360 hosts -> $DNS_TARGET"
        return 0
    fi
    # A default route means station mode is active, so the AP's DHCP/DNS
    # server is no longer needed.  Take port 53 from a lingering stock
    # dnsmasq as a last resort, then retry once.
    if ip route 2>/dev/null | grep -q '^default'; then
        log "dns restart failed; taking over port 53 from stock dnsmasq"
        killall dnsmasq 2>/dev/null
        sleep 1
        if start_dns && sleep 1 && dns_ok; then
            log "dns redirect ready after takeover: insta360 hosts -> $DNS_TARGET"
            return 0
        fi
    fi
    log "ERROR: dns redirect is not working; see $DNS_LOG"
    return 1
}

start_watchdog() {
    if [ -f "$DNS_WATCH_PID" ]; then
        kill "$(cat "$DNS_WATCH_PID")" 2>/dev/null
        sleep 0.2
    fi
    (
        fails=0
        while :; do
            if ! ip addr show dev lo 2>/dev/null | grep -q "8.8.8.8"; then
                ip addr add 8.8.8.8/32 dev lo 2>/dev/null
                log "watchdog: re-added 8.8.8.8/32 to lo"
            fi
            # In AP mode there is no default route and no upload path.  Do
            # not fight the AP's own DHCP/DNS server.
            if ip route 2>/dev/null | grep -q '^default'; then
                if ! dns_ok; then
                    fails=$((fails + 1))
                    # Retry immediately, then back off to once a minute so a
                    # permanently broken setup does not fill bootup.log.
                    if [ "$fails" -le 2 ] || [ $((fails % 12)) -eq 0 ]; then
                        ensure_dns
                    fi
                else
                    fails=0
                fi
            else
                fails=0
            fi
            sleep 5
        done
    ) </dev/null >>"$LOG" 2>&1 &
    echo $! > "$DNS_WATCH_PID"
}

if [ -x "$DNSMASQ" ]; then
    if ip route 2>/dev/null | grep -q '^default'; then
        ensure_dns
    else
        log "no default route yet; DNS redirect will start when Wi-Fi is up"
    fi
    start_watchdog
    log "dnsmasq watchdog pid=$(cat "$DNS_WATCH_PID" 2>/dev/null)"
else
    log "WARN: dnsmasq not found; relying on /etc/hosts only"
fi

# Also add static hosts entries (c-ares may consult the hosts file, and any
# glibc-based resolver definitely will).
if [ ! -f /tmp/inscloud-hosts.done ]; then
    if [ -e /etc/hosts ]; then
        cp /etc/hosts /tmp/hosts 2>/dev/null || : > /tmp/hosts
        {
            echo "# added by cloud-backup bootup.sh"
            echo "$DNS_TARGET service-test.insta360.com"
            echo "$DNS_TARGET cloud.insta360.com"
        } >> /tmp/hosts
        mount --bind /tmp/hosts /etc/hosts 2>>"$LOG"
        log "hosts bind-mount rc=$?"
    else
        log "WARN: /etc/hosts missing; relying on dnsmasq for the redirect"
    fi
    touch /tmp/inscloud-hosts.done
fi

fi  # !DISABLE_CLOUD_REDIRECT

# ---------------------------------------------------------------------------
# 2. Trust the local CA
# ---------------------------------------------------------------------------

CA="$SD/ca.crt"
CA_HASH_FILE="$SD/ca.hash"

setup_ca() {
    if [ ! -f "$CA" ]; then
        log "WARN: $CA missing, TLS will fail"
        return
    fi

    # Preferred: overlay the whole directory so OpenSSL CAPATH can find the CA
    # under its subject-hash name as well as CAINFO finding it in cacert.pem.
    if [ -f "$CA_HASH_FILE" ]; then
        CA_HASH=$(tr -d '[:space:]' < "$CA_HASH_FILE")
        if [ -n "$CA_HASH" ]; then
            rm -rf /tmp/inscloud-certs
            mkdir -p /tmp/inscloud-certs
            if cp -R /etc/ssl/certs/. /tmp/inscloud-certs/ 2>/dev/null; then
                for bundle in cacert.pem ca-bundle.crt; do
                    [ -f "/tmp/inscloud-certs/$bundle" ] || continue
                    {
                        echo ""
                        echo "# Insta360 local backup CA"
                        cat "$CA"
                    } >> "/tmp/inscloud-certs/$bundle"
                done
                cp "$CA" "/tmp/inscloud-certs/$CA_HASH.0" 2>/dev/null
                if mount --bind /tmp/inscloud-certs /etc/ssl/certs 2>>"$LOG"; then
                    log "CA directory overlay mounted (hash $CA_HASH)"
                    return
                fi
                log "CA directory overlay failed; using per-file mounts"
            fi
        fi
    fi

    # Fallback: append the CA to each bundle file in place (CAINFO path).
    for bundle in /etc/ssl/certs/cacert.pem /etc/ssl/certs/ca-bundle.crt; do
        [ -f "$bundle" ] || continue
        tmp="/tmp/$(basename "$bundle")"
        cp "$bundle" "$tmp" 2>/dev/null || continue
        {
            echo ""
            echo "# Insta360 local backup CA"
            cat "$CA"
        } >> "$tmp"
        mount --bind "$tmp" "$bundle" 2>>"$LOG"
        log "CA bind-mount $bundle rc=$?"
    done
}

if [ ! -f /tmp/inscloud-ca.done ]; then
    setup_ca
    touch /tmp/inscloud-ca.done
else
    log "CA setup already done"
fi

# ---------------------------------------------------------------------------
# 3. Remote backup tunnel (Tailscale userspace + CONNECT relay)
# ---------------------------------------------------------------------------

TS_DIR=${TAILSCALE_DIR:-$SD/tailscale}
TS_CLI=${TAILSCALE_CLI:-$TS_DIR/tailscale}
TS_SOCKET=${TAILSCALE_SOCKET:-/tmp/tailscaled.sock}
TS_PROXY_PORT=${TAILSCALE_PROXY_PORT:-8080}
TS_UP_PID=/tmp/inscloud-tailscale-up.pid
TS_UP_LOG=/tmp/inscloud-tailscale-up.log
TS_DEBUG="$SD/tailscale.log"

ts_save_debug() {
    if [ -f "$TS_DEBUG" ] && [ "$(wc -c < "$TS_DEBUG" 2>/dev/null || echo 0)" -gt 200000 ]; then
        mv -f "$TS_DEBUG" "$TS_DEBUG.1" 2>/dev/null
    fi
    {
        echo "=== $(date '+%Y-%m-%d %H:%M:%S') tailscale retry diagnostics ==="
        echo "--- daemon ---"
        tail -n 60 /tmp/inscloud-tailscaled.log 2>/dev/null
        echo "--- up ---"
        tail -n 30 "$TS_UP_LOG" 2>/dev/null
        echo
    } >> "$TS_DEBUG" 2>/dev/null
}
RELAY="$SD/tailrelay"
RELAY_PID=/tmp/inscloud-tailrelay.pid
RELAY_LOG=/tmp/inscloud-tailrelay.log
RELAY_PORT=${CLOUD_RELAY_PORT:-443}
WATCHDOG_PID=/tmp/inscloud-tunnel-watchdog.pid

start_tailscale_login() {
    if [ -f "$TS_UP_PID" ]; then
        pid=$(cat "$TS_UP_PID" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    if [ ! -f "$SD/tailscale-up.sh" ]; then
        log "ERROR: tailscale-up.sh missing"
        return 1
    fi

    # Crash-loop guard.  Six starts inside five minutes is a genuine loop.
    # Wait five minutes, then reset the counter and try again; the previous
    # version rewrote the timestamp on every call, so the window never
    # expired and one bad afternoon disabled Tailscale until the next boot.
    now=$(date +%s 2>/dev/null || echo 0)
    if [ -f /tmp/inscloud-ts-backoff ]; then
        until=$(cat /tmp/inscloud-ts-backoff 2>/dev/null || echo 0)
        if [ "$now" -lt "$until" ]; then
            return 1
        fi
        rm -f /tmp/inscloud-ts-backoff /tmp/inscloud-ts-start-count \
              /tmp/inscloud-ts-last-start
    fi
    last=$(cat /tmp/inscloud-ts-last-start 2>/dev/null || echo 0)
    count=$(cat /tmp/inscloud-ts-start-count 2>/dev/null || echo 0)
    if [ $((now - last)) -ge 300 ]; then
        count=0
    fi
    if [ "$count" -ge 6 ]; then
        ts_save_debug
        echo $((now + 300)) > /tmp/inscloud-ts-backoff
        log "ERROR: tailscale failed $count starts in 5 min; retrying in 5 min"
        return 1
    fi
    count=$((count + 1))
    echo "$now" > /tmp/inscloud-ts-last-start
    echo "$count" > /tmp/inscloud-ts-start-count

    sh "$SD/tailscale-up.sh" >>"$TS_UP_LOG" 2>&1 &
    echo $! > "$TS_UP_PID"
    log "tailscale-up started (pid $!)"
}

relay_alive() {
    if [ -f "$RELAY_PID" ]; then
        pid=$(cat "$RELAY_PID" 2>/dev/null)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
    fi
    return 1
}

spawn_relay() {
    "$1" "$RELAY_PORT" "$TAILNET_TARGET" "$SERVER_PORT" \
        127.0.0.1 "$TS_PROXY_PORT" 127.0.0.1 >>"$RELAY_LOG" 2>&1 &
    echo $! > "$RELAY_PID"
    sleep 1
    relay_alive
}

start_relay() {
    relay_alive && return 0
    if [ ! -x "$RELAY" ]; then
        log "ERROR: $RELAY missing or not executable"
        return 1
    fi
    if spawn_relay "$RELAY"; then
        log "tailrelay listening on 127.0.0.1:$RELAY_PORT -> $TAILNET_TARGET:$SERVER_PORT via 127.0.0.1:$TS_PROXY_PORT"
        return 0
    fi
    # Defensive fallback for a noexec card (bootup.sh itself normally proves
    # the card is executable, but mounts can differ).
    log "tailrelay failed to start from SD; retrying from /tmp"
    if cp "$RELAY" /tmp/inscloud-tailrelay 2>/dev/null; then
        chmod 755 /tmp/inscloud-tailrelay
        if spawn_relay /tmp/inscloud-tailrelay; then
            RELAY=/tmp/inscloud-tailrelay
            log "tailrelay running from /tmp"
            return 0
        fi
    fi
    log "ERROR: tailrelay could not start; see $RELAY_LOG"
    return 1
}

tunnel_watchdog() {
    log "tunnel watchdog start"
    retry_tick=0
    while :; do
        if ! pidof tailscaled >/dev/null 2>&1; then
            # start_tailscale_login is pid-guarded and logs only on a real
            # start; while it waits for Wi-Fi, do not log every 15 s.
            start_tailscale_login
        elif ! tailscale_logged_in; then
            # NeedsLogin (first boot, expired node key, or revoked key).
            # status exits 1 in this state; do not confuse it with a dead
            # daemon.  Retry at most every ~60 s to avoid hammering the
            # control plane when a key is wrong.
            retry_tick=$((retry_tick + 1))
            if [ $((retry_tick % 4)) -eq 1 ]; then
                log "watchdog: tailscale is not logged in, retrying"
                start_tailscale_login
            fi
        else
            rm -f /tmp/inscloud-ts-start-count /tmp/inscloud-ts-last-start \
                  /tmp/inscloud-ts-backoff
        fi
        if [ "$DISABLE_CLOUD_REDIRECT" != "1" ] && ! relay_alive; then
            log "watchdog: tailrelay is gone, restarting"
            start_relay
        fi
        sleep 15
    done
}

tailscale_logged_in() {
    if [ -n "${TS_CLI:-}" ] && [ -x "$TS_CLI" ]; then
        "$TS_CLI" --socket="$TS_SOCKET" status >/dev/null 2>&1 && return 0
    fi
    if [ -x /tmp/inscloud-tailscale/tailscale ]; then
        /tmp/inscloud-tailscale/tailscale --socket="$TS_SOCKET" status \
            >/dev/null 2>&1 && return 0
    fi
    # A flaky SD read can make the CLI unusable (different SHA-256 on every
    # read).  The daemon's userspace proxy is what the uploader needs, so a
    # live daemon listening on 8080 counts as up.
    if pidof tailscaled >/dev/null 2>&1 && \
       netstat -lnt 2>/dev/null | grep -q '127.0.0.1:8080'; then
        return 0
    fi
    return 1
}

if [ "$BACKUP_MODE" = "tailscale" ] && [ "$NETWORK_MODE" != "ap_fallback" ]; then
    start_tailscale_login
    [ "$DISABLE_CLOUD_REDIRECT" = "1" ] || start_relay
    if [ -f "$WATCHDOG_PID" ]; then
        wd=$(cat "$WATCHDOG_PID" 2>/dev/null)
        if [ -n "$wd" ] && kill -0 "$wd" 2>/dev/null; then
            log "tunnel watchdog already running (pid $wd)"
        else
            tunnel_watchdog </dev/null >/dev/null 2>&1 &
            echo $! > "$WATCHDOG_PID"
            log "tunnel watchdog pid=$!"
        fi
    else
        tunnel_watchdog </dev/null >/dev/null 2>&1 &
        echo $! > "$WATCHDOG_PID"
        log "tunnel watchdog pid=$!"
    fi
else
    if [ "$NETWORK_MODE" = "ap_fallback" ]; then
        log "AP fallback: tunnel and relay not started (no network)"
    else
        log "lan mode: no tunnel or relay started"
    fi
fi

# ---------------------------------------------------------------------------
# 3b. Direct SD-card backup (independent of the camera's paid cloud)
# ---------------------------------------------------------------------------
# The paid uploader (instaAIP) is not required for this path.  insupload
# walks the media tree, asks the receiver how much it already has, and
# appends the missing bytes over the tailnet (or the LAN).  It keeps running
# across passes so clips recorded later are picked up without a reboot.
if [ "${START_SD_UPLOAD:-0}" = "1" ] && [ "$NETWORK_MODE" = "ap_fallback" ]; then
    log "sd uploader not started: camera is in AP fallback (no network)"
fi
if [ "${START_SD_UPLOAD:-0}" = "1" ] && [ "$NETWORK_MODE" != "ap_fallback" ]; then
    UPLOAD_URL=${SD_UPLOAD_URL:-}
    if [ -z "$UPLOAD_URL" ]; then
        case "$BACKUP_MODE" in
            tailscale) UPLOAD_URL="http://$TAILNET_TARGET:${SD_UPLOAD_PORT:-8844}" ;;
            *)         UPLOAD_URL="http://$SERVER_IP:${SD_UPLOAD_PORT:-8844}" ;;
        esac
    fi
    UPLOAD_ROOT=${SD_UPLOAD_ROOTS:-$SD/DCIM}

    if [ -f "$SD/insupload" ]; then
        # Verify in place first; only use the tmpfs when the read is bad.
        RUN=""
        want=$(expected_for insupload)
        got=$(hash_of "$SD/insupload")
        if [ -z "$want" ] || [ "$got" = "$want" ]; then
            RUN="$SD/insupload"
        else
            i=0
            while [ "$i" -lt 4 ]; do
                if cp "$SD/insupload" /tmp/insupload 2>/dev/null; then
                    got=$(hash_of /tmp/insupload)
                    if [ -z "$want" ] || [ "$got" = "$want" ]; then
                        chmod 755 /tmp/insupload 2>/dev/null
                        RUN=/tmp/insupload
                        break
                    fi
                    log "insupload integrity mismatch try=$((i + 1)) want=$want got=$got"
                    rm -f /tmp/insupload
                fi
                i=$((i + 1))
            done
        fi
        if [ -x "$RUN" ]; then
            # Keep the card-side log bounded; it is append-only across boots.
            if [ -f "$SD/upload.log" ]; then
                logsize=$(wc -c < "$SD/upload.log" 2>/dev/null || echo 0)
                [ "${logsize:-0}" -gt 1048576 ] && \
                    mv -f "$SD/upload.log" "$SD/upload.log.1"
            fi
            set -- --url "$UPLOAD_URL" --root "$UPLOAD_ROOT" \
                   --interval "${SD_UPLOAD_INTERVAL:-300}" \
                   --wait "${SD_UPLOAD_WAIT:-600}"
            [ -n "${SD_UPLOAD_LAN_URL:-}" ] && \
                set -- "$@" --url2 "$SD_UPLOAD_LAN_URL"
            # tailscaled runs in userspace mode (no tun), so tailnet dials
            # must go through its HTTP CONNECT proxy.  The receiver URL still
            # names the tailnet address; the proxy resolves it.
            if [ -z "${SD_UPLOAD_PROXY:-}" ] && [ "$BACKUP_MODE" = "tailscale" ]; then
                SD_UPLOAD_PROXY="127.0.0.1:$TS_PROXY_PORT"
            fi
            if [ -n "${SD_UPLOAD_PROXY:-}" ]; then
                set -- "$@" --proxy "$SD_UPLOAD_PROXY"
            fi
            [ -f "$SD/sd-upload.token" ] && \
                set -- "$@" --token-file "$SD/sd-upload.token"
            [ "${SD_UPLOAD_DELETE:-0}" = "1" ] && set -- "$@" --delete
            "$RUN" "$@" >>"$SD/upload.log" 2>&1 &
            log "sd uploader started (pid $!, $UPLOAD_URL, root=$UPLOAD_ROOT)"
        else
            log "ERROR: no verified copy of insupload; not starting it"
        fi
    else
        log "WARN: START_SD_UPLOAD=1 but insupload is missing"
    fi
fi

# ---------------------------------------------------------------------------
# 4. Optional restart / debug access
# ---------------------------------------------------------------------------

if [ "${RESTART_AIP:-0}" = "1" ] && [ "${CLOUD_BACKUP_EARLY:-0}" != "1" ]; then
    if command -v killall >/dev/null 2>&1; then
        killall instaAIP 2>/dev/null
    else
        for p in $(pidof instaAIP 2>/dev/null); do kill "$p" 2>/dev/null; done
    fi
    sleep 1
    /usr/bin/instaAIP >>"$LOG" 2>&1 &
    log "restarted instaAIP"
fi

if [ "${START_TELNETD:-0}" = "1" ]; then
    # The camera may have a default-DROP filter table.  Open only 2323 for
    # this temporary debug shell; it is runtime-only and disappears on reboot.
    if command -v iptables >/dev/null 2>&1; then
        iptables -I INPUT -p tcp --dport 2323 -j ACCEPT 2>>"$LOG" && \
            log "opened tcp/2323 in the input firewall"
    fi
    if command -v telnetd >/dev/null 2>&1; then
        telnetd -l /bin/sh -p 2323 >>"$LOG" 2>&1 &
        log "telnetd started on port 2323"
    else
        log "WARN: telnetd not available"
    fi
fi

# Trigger the camera's uploader directly.  This is the part that replaces the
# app's "enable cloud backup" switch: see cloud-upload-watch.sh and
# cloudkick_build.py.  It waits for Wi-Fi and instaAIP by itself.
if [ "${START_CLOUD_UPLOAD:-0}" = "1" ]; then
    if [ -x "$SD/cloud-upload-watch.sh" ] || [ -f "$SD/cloud-upload-watch.sh" ]; then
        sh "$SD/cloud-upload-watch.sh" >>"$LOG" 2>&1 &
        log "cloud upload watcher started"
    else
        log "WARN: START_CLOUD_UPLOAD=1 but cloud-upload-watch.sh is missing"
    fi
fi

# One-shot diagnostic dump, useful when uploads do not start and telnet is not
# available: everything is written to $SD/diag.log.
if [ "${CLOUD_DIAG:-0}" = "1" ] && [ -f "$SD/diagnose.sh" ] && \
   [ ! -f /tmp/inscloud-diag.done ]; then
    touch /tmp/inscloud-diag.done
    ( sleep "${CLOUD_DIAG_DELAY:-60}"; sh "$SD/diagnose.sh" ) \
        </dev/null >/dev/null 2>&1 &
    log "diagnostics scheduled in ${CLOUD_DIAG_DELAY:-60}s ($SD/diag.log)"
fi

log "=== cloud-backup bootup.sh done ==="
exit 0
