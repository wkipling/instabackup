#!/bin/sh
# Trigger the camera's own cloud uploader without the Insta360 app.
#
# The app only enables backup by asking the RTOS to post SysV messages to
# instaAIP's `cloud_msg` thread:
#
#   action 8 -> query uploaded/uploading lists, reconcile the local SQLite DB
#   action 7 -> ins_cloud_sdk_file_group_cfg() scans /tmp/SD0/DCIM and builds
#               the upload list (DAT_00694d70)
#   action 0 -> ins_cloud_sdk_upload_start() runs the TCP tune script and
#               starts InsCloudClient::upload() using that list
#
# This watcher waits until instaAIP and a default route exist, then runs the
# bundled `cloudkick` helper.  It needs no app, account, binding or paid
# subscription.  Uploaded files are recorded in the SDK's SQLite DB, so later
# runs only upload new media.
#
# With BACKUP_MODE=tailscale it additionally waits for tailrelay and
# tailscaled to reach the home server (an actual CONNECT probe, not just a
# route) before sending the actions, and re-checks before every scan.
#
# Config variables (cloud-backup.conf):
#   START_CLOUD_UPLOAD=1        enable this watcher (default 0)
#   CLOUD_UPLOAD_INTERVAL=3600  seconds between scans (default 3600)
#                               (the watcher skips a cycle while the default
#                                interface is carrying upload traffic)
#   CLOUD_UPLOAD_DELAY=15       initial delay after network is up (default 15)
#   CLOUD_UPLOAD_ONCE=0         1 = do one scan then exit
#   CLOUD_UPLOAD_ACTIONS=870    digits sent to instaAIP
#                                (8=query uploaded list, 7=scan, 0=start)
#   CLOUD_UPLOAD_SAVE_DB=1      save the SQLite DB via action 15 when idle
#                                (so a reboot does not re-upload everything)

SD=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
[ -n "$SD" ] || SD=/tmp/SD0
LOG="$SD/bootup.log"
PIDFILE=/tmp/inscloud-upload-watch.pid

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') cloud-upload: $*" >> "$LOG" 2>/dev/null
}

if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
        log "watcher already running (pid $old)"
        exit 0
    fi
fi
echo $$ > "$PIDFILE"

CONF="$SD/cloud-backup.conf"
[ -f "$CONF" ] && . "$CONF"

INTERVAL=${CLOUD_UPLOAD_INTERVAL:-3600}
DELAY=${CLOUD_UPLOAD_DELAY:-15}
ACTIONS=${CLOUD_UPLOAD_ACTIONS:-870}
SAVE_DB=${CLOUD_UPLOAD_SAVE_DB:-1}
ONCE=${CLOUD_UPLOAD_ONCE:-0}
BACKUP_MODE=${BACKUP_MODE:-lan}
SERVER_PORT=${SERVER_PORT:-443}
TAILNET_TARGET=${TAILNET_TARGET:-}
TS_PROXY_PORT=${TAILSCALE_PROXY_PORT:-8080}
RELAY="$SD/tailrelay"
RELAY_PID=/tmp/inscloud-tailrelay.pid

if [ "$BACKUP_MODE" = "tailscale" ] && [ -z "$TAILNET_TARGET" ]; then
    log "ERROR: BACKUP_MODE=tailscale but TAILNET_TARGET is empty"
    exit 1
fi

# The card may be mounted noexec; /tmp is tmpfs and is executable on the X5.
KICK=""
if [ -f "$SD/cloudkick" ]; then
    if cp "$SD/cloudkick" /tmp/cloudkick 2>/dev/null; then
        chmod 755 /tmp/cloudkick 2>/dev/null
        KICK=/tmp/cloudkick
    elif [ -x "$SD/cloudkick" ]; then
        KICK="$SD/cloudkick"
    fi
fi
if [ -z "$KICK" ]; then
    log "ERROR: cloudkick missing or not executable (card noexec?)"
    exit 1
fi

log "watcher start pid=$$ actions=$ACTIONS interval=${INTERVAL}s once=$ONCE kick=$KICK"

# Wait for the daemon and a usable network.  instaAIP creates the cloud queue
# itself, but cloudkick also creates it with IPC_CREAT, so ordering is safe.
wait_ready() {
    while :; do
        if ! pidof instaAIP >/dev/null 2>&1; then
            sleep 3
            continue
        fi
        # a default route means Wi-Fi station mode (or USB tethering) is up
        if ip route 2>/dev/null | grep -q '^default'; then
            return 0
        fi
        sleep 3
    done
}

net_bytes() {
    awk -v i="$1:" '$1 == i { print $2 + $10; exit }' /proc/net/dev 2>/dev/null
}

# A keep-alive TCP connection is not a reliable "upload in progress" signal,
# so sample the default interface's byte counters instead.  A stalled upload
# is treated as idle (and retried), which is the desired behaviour.
upload_busy() {
    ifc=$(ip route 2>/dev/null | awk '/^default/ { for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }')
    [ -n "$ifc" ] || return 1
    a=$(net_bytes "$ifc")
    [ -n "$a" ] || return 1
    sleep 2
    b=$(net_bytes "$ifc")
    [ -n "$b" ] || return 1
    [ $((b - a)) -gt 131072 ]
}

relay_alive() {
    if [ -f "$RELAY_PID" ]; then
        pid=$(cat "$RELAY_PID" 2>/dev/null)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
    fi
    return 1
}

# Open and immediately close one CONNECT through tailscaled to the home
# server.  This checks exactly the path the uploader will use (peer online,
# ACL allows camera -> server:443, proxy alive) without sending data.
# tailscale ping is not used because ACLs can block it while TCP works.
tunnel_probe() {
    "$RELAY" --probe "$TAILNET_TARGET" "$SERVER_PORT" \
        127.0.0.1 "$TS_PROXY_PORT" >/dev/null 2>&1
    rc=$?
    case "$rc" in
        126|127)
            # noexec card: bootup.sh runs the relay from /tmp
            [ -x /tmp/inscloud-tailrelay ] || return "$rc"
            /tmp/inscloud-tailrelay --probe "$TAILNET_TARGET" "$SERVER_PORT" \
                127.0.0.1 "$TS_PROXY_PORT" >/dev/null 2>&1
            ;;
        *) return "$rc" ;;
    esac
}

wait_tunnel() {
    [ "$BACKUP_MODE" = "tailscale" ] || return 0
    n=0
    while :; do
        if relay_alive && tunnel_probe; then
            [ "$n" -gt 0 ] && log "tailnet path ready after $n failed attempt(s)"
            return 0
        fi
        n=$((n + 1))
        if [ $((n % 6)) -eq 1 ]; then
            if ! relay_alive; then
                log "WARN: tailrelay is not running (bootup watchdog should restart it)"
            else
                log "tailnet path not ready yet (attempt $n)"
            fi
        fi
        sleep 10
    done
}

wait_ready
log "instaAIP and default route present; waiting ${DELAY}s"
# TLS will fail if the camera clock is far behind the certificate's notBefore.
# The camera normally gets time over Wi-Fi/RTOS; this is only a diagnostic.
year=$(date +%Y 2>/dev/null)
if [ -n "$year" ] && [ "$year" -lt 2025 ] 2>/dev/null; then
    log "WARN: camera clock is $(date) - TLS may reject the certificate"
fi
sleep "$DELAY"

if [ "$BACKUP_MODE" = "tailscale" ]; then
    log "waiting for the tailnet path to the home server"
    wait_tunnel
fi

while :; do
    if ! pidof instaAIP >/dev/null 2>&1; then
        log "instaAIP is gone; waiting for restart"
        wait_ready
    fi
    # Do not start a scan/upload when the tunnel is down: the SDK would fail
    # the whole batch and only retry on the next cycle.
    if [ "$BACKUP_MODE" = "tailscale" ]; then
        wait_tunnel
    fi
    # Starting a second scan/upload while the SDK is moving data can race its
    # task manager.  Skip this cycle if the default interface is busy.
    if upload_busy; then
        log "upload traffic still flowing; skipping this cycle"
        [ "$ONCE" = "1" ] && break
        sleep "$INTERVAL"
        continue
    fi
    # Save records from the previous cycle to the RTOS/FL0 before scanning.
    # Only do this when the interface is idle, so the SQLite file is quiescent.
    if [ "$SAVE_DB" = "1" ]; then
        "$KICK" f >>"$LOG" 2>&1
        log "db save (action 15) rc=$?"
    fi
    log "sending actions $ACTIONS"
    "$KICK" "$ACTIONS" >>"$LOG" 2>&1
    rc=$?
    log "cloudkick rc=$rc"
    [ "$ONCE" = "1" ] && break
    sleep "$INTERVAL"
done

rm -f "$PIDFILE"
log "watcher exit"
exit 0
