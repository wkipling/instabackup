#!/bin/sh
# Start tailscaled in userspace-networking mode and join the tailnet.
#
# The camera kernel has no /dev/net/tun, so userspace mode is required.  In
# that mode tailscaled provides a local HTTP proxy (CONNECT) that dials peers
# through its netstack:
#
#   tailscaled --tun=userspace-networking \
#              --outbound-http-proxy-listen=127.0.0.1:8080
#
# tailrelay uses that proxy; nothing else on the camera does.
#
# Config (cloud-backup.conf):
#   BACKUP_MODE=tailscale
#   TAILNET_TARGET=100.x.y.z        home server's `tailscale ip -4`
#   TAILSCALE_AUTHKEY=tskey-auth-...  reusable key (only needed for first login)
#   TAILSCALE_HOSTNAME=insta360-cam
#   TAILSCALE_PROXY_PORT=8080
#   TAILSCALE_NETWORK_WAIT=600      seconds to wait for a default route
#
# The state file lives on the SD card, so the node stays logged in across
# reboots.  Treat that file like a password.
set -u

SD=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
[ -n "$SD" ] || SD=/tmp/SD0
LOG="$SD/bootup.log"
TSLOG=/tmp/inscloud-tailscaled.log
PIDFILE=/tmp/inscloud-tailscaled.pid
SD_DEBUG="$SD/tailscale.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') tailscale: $*" >> "$LOG" 2>/dev/null
}

# The daemon log lives in /tmp and disappears on reboot.  Copy the tail to the
# card when a start fails, so the next pull of the card explains why.
save_debug() {
    [ -f "$SD/bootup.log" ] || return 0
    if [ -f "$SD_DEBUG" ] && [ "$(wc -c < "$SD_DEBUG" 2>/dev/null || echo 0)" -gt 200000 ]; then
        mv -f "$SD_DEBUG" "$SD_DEBUG.1" 2>/dev/null
    fi
    {
        echo "=== $(date '+%Y-%m-%d %H:%M:%S') tailscale start failed ==="
        echo "--- tailscaled ---"
        tail -n 60 "$TSLOG" 2>/dev/null
        echo "--- up ---"
        tail -n 30 /tmp/inscloud-tailscale-up.log 2>/dev/null
        echo
    } >> "$SD_DEBUG" 2>/dev/null
}

CONF="$SD/cloud-backup.conf"
if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF"
fi

MODE=${BACKUP_MODE:-lan}
if [ "$MODE" != "tailscale" ]; then
    log "BACKUP_MODE=$MODE, not starting Tailscale"
    exit 0
fi

TS_DIR=${TAILSCALE_DIR:-$SD/tailscale}
TSD=${TAILSCALE_DAEMON:-$TS_DIR/tailscaled}
CLI=${TAILSCALE_CLI:-$TS_DIR/tailscale}
STATE=${TAILSCALE_STATE:-$SD/tailscale/tailscaled.state}
SOCK=${TAILSCALE_SOCKET:-/tmp/tailscaled.sock}
PROXY_PORT=${TAILSCALE_PROXY_PORT:-8080}
HOSTNAME=${TAILSCALE_HOSTNAME:-insta360-cam}
AUTHKEY=${TAILSCALE_AUTHKEY:-}
NET_WAIT=${TAILSCALE_NETWORK_WAIT:-600}
MEMMAX=${TAILSCALE_GOMEMLIMIT:-96MiB}

if [ ! -x "$TSD" ] || [ ! -x "$CLI" ]; then
    log "ERROR: missing $TSD or $CLI (copy the tailscale_*_arm64 files there)"
    exit 1
fi

# The camera's exFAT driver has produced different bytes for files written
# by a PC (same file hashes correctly on the host).  Never execute a binary
# straight from the card: copy it to the tmpfs, check it against SHA256SUMS
# from install time, and retry the read a few times before giving up.
hash_of() {
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

expected_for() {
    awk -v p="$1" '$2 == p { print $1; exit }' "$SD/SHA256SUMS" 2>/dev/null
}

copy_verified() {  # src key dst
    src=$1; key=$2; dst=$3
    want=$(expected_for "$key")
    i=0
    while [ "$i" -lt 4 ]; do
        if cp "$src" "$dst" 2>/dev/null; then
            got=$(hash_of "$dst")
            if [ -z "$want" ] || [ "$got" = "$want" ]; then
                chmod 755 "$dst" 2>/dev/null
                log "integrity: $key ok"
                return 0
            fi
            log "integrity: $key mismatch try=$((i + 1)) want=$want got=$got"
            echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
            rm -f "$dst"
        fi
        i=$((i + 1))
    done
    return 1
}

TSD_RUN="$TSD"
CLI_RUN=""
TSD_TMP=/tmp/inscloud-tailscale/tailscaled
CLI_TMP=/tmp/inscloud-tailscale/tailscale
mkdir -p /tmp/inscloud-tailscale 2>/dev/null

# Verify the file where it is first.  A matching hash means the bytes read
# just now are correct; execution still demand-pages from the card later, so
# this is only a fallback for when /tmp cannot hold a copy.
use_verified() {  # path key
    path=$1; key=$2
    want=$(expected_for "$key")
    i=0
    while [ "$i" -lt 3 ]; do
        got=$(hash_of "$path")
        if [ -z "$want" ] || [ "$got" = "$want" ]; then
            log "integrity: $key ok (in place)"
            return 0
        fi
        log "integrity: $key mismatch in place try=$((i + 1)) want=$want got=$got"
        echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
        i=$((i + 1))
        sleep 1
    done
    return 1
}

# On a normal boot the saved node state is enough: tailscaled reconnects on
# its own and the 25 MB CLI never has to run (it is the component that has
# been seen to segfault after a bad card read).  Stage the daemon in tmpfs so
# the kernel is not demand-paging a 36 MB executable off exFAT.
if [ -s "$STATE" ]; then
    if pidof tailscaled >/dev/null 2>&1; then
        # Another retry already started the daemon; never overwrite a
        # running executable (ETXTBSY) just to stage an unused copy.
        log "tailscaled already running; keeping the current binary"
        TSD_RUN="$TSD_TMP"
        [ -x "$TSD_RUN" ] || TSD_RUN="$TSD"
    elif copy_verified "$TSD" tailscale/tailscaled "$TSD_TMP"; then
        TSD_RUN="$TSD_TMP"
    elif use_verified "$TSD" tailscale/tailscaled; then
        log "WARN: /tmp could not hold tailscaled; running from the card"
    else
        save_debug
        log "ERROR: no verified tailscaled copy; refusing to run it"
        exit 1
    fi
else
    # First login needs both binaries at once.  /tmp cannot hold 61 MB, so
    # keep the daemon on the card for this one boot and stage the CLI, which
    # is the smaller and more read-sensitive of the two.
    if ! use_verified "$TSD" tailscale/tailscaled; then
        save_debug
        log "ERROR: no verified tailscaled copy; refusing to run it"
        exit 1
    fi
fi

daemon_alive() {
    pidof tailscaled >/dev/null 2>&1 && return 0
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE" 2>/dev/null)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
    fi
    return 1
}

wait_network() {
    waited=0
    while ! ip route 2>/dev/null | grep -q '^default'; do
        if [ "$waited" -ge "$NET_WAIT" ]; then
            log "no default route after ${NET_WAIT}s"
            return 1
        fi
        sleep 5
        waited=$((waited + 5))
    done
    return 0
}

start_daemon() {
    if daemon_alive; then
        log "tailscaled already running"
        return 0
    fi

    mkdir -p "$(dirname "$STATE")" 2>/dev/null
    # /tmp is only 78 MB on this camera and the daemon's log lives there.
    # Cap it so a retry loop cannot fill tmpfs and take Wi-Fi down with it.
    if [ -f "$TSLOG" ] && [ "$(wc -c < "$TSLOG" 2>/dev/null || echo 0)" -gt 262144 ]; then
        tail -c 65536 "$TSLOG" > "$TSLOG.tmp" 2>/dev/null && \
            mv -f "$TSLOG.tmp" "$TSLOG" 2>/dev/null
    fi
    log "starting tailscaled (userspace, proxy 127.0.0.1:$PROXY_PORT)"
    GOMEMLIMIT="$MEMMAX" GOGC=50 GODEBUG=asyncpreemptoff=1 "$TSD_RUN" \
        --tun=userspace-networking \
        --outbound-http-proxy-listen="127.0.0.1:$PROXY_PORT" \
        --state="$STATE" \
        --socket="$SOCK" \
        >>"$TSLOG" 2>&1 &
    echo $! > "$PIDFILE"

    i=0
    while [ "$i" -lt 45 ]; do
        if ! daemon_alive; then
            log "ERROR: tailscaled exited during startup (see $TSLOG)"
            return 1
        fi
        # The socket is created when the local API is ready.  Do NOT require
        # `tailscale status` to succeed: it exits 1 while logged out, which is
        # exactly the first-boot state.
        if [ -S "$SOCK" ]; then
            log "tailscaled is up (pid $(cat "$PIDFILE" 2>/dev/null))"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    log "ERROR: tailscaled did not become ready (see $TSLOG)"
    return 1
}

prepare_cli() {
    [ -n "$CLI_RUN" ] && return 0
    if copy_verified "$CLI" tailscale/tailscale "$CLI_TMP"; then
        CLI_RUN="$CLI_TMP"
    elif use_verified "$CLI" tailscale/tailscale; then
        log "WARN: /tmp could not hold the tailscale CLI; running it from the card"
        CLI_RUN="$CLI"
    else
        log "WARN: tailscale CLI unreadable; continuing with saved node state"
        return 1
    fi
    return 0
}

login() {
    # A saved node state is a complete login.  tailscaled reconnects by
    # itself, so do not involve the CLI at all: running `tailscale up` here
    # only duplicated work and exposed the flaky 25 MB binary.
    if [ -s "$STATE" ]; then
        log "saved node state; tailscaled will reconnect without the CLI"
        touch "$TS_DIR/.logged-in" 2>/dev/null
        return 0
    fi

    if ! prepare_cli; then
        log "ERROR: no saved state and no usable CLI; cannot join tailnet"
        return 1
    fi

    if [ -n "$AUTHKEY" ]; then
        log "running tailscale up with auth key"
        "$CLI_RUN" --socket="$SOCK" up \
            --auth-key="$AUTHKEY" \
            --hostname="$HOSTNAME" \
            --accept-dns=false \
            --accept-routes=false \
            --netfilter-mode=off >>"$TSLOG" 2>&1
        rc=$?
        log "tailscale up rc=$rc"
    elif [ -s "$STATE" ]; then
        log "no auth key; reusing saved node state"
        "$CLI_RUN" --socket="$SOCK" up \
            --hostname="$HOSTNAME" \
            --accept-dns=false \
            --accept-routes=false \
            --netfilter-mode=off >>"$TSLOG" 2>&1
        rc=$?
        log "tailscale up rc=$rc"
    else
        log "ERROR: no TAILSCALE_AUTHKEY and no saved state; cannot join tailnet"
        return 1
    fi
    if [ "$rc" -ne 0 ]; then
        return "$rc"
    fi

    ip4=$("$CLI_RUN" --socket="$SOCK" ip -4 2>/dev/null | head -n 1)
    if [ -n "$ip4" ]; then
        log "tailnet address: $ip4"
    else
        log "WARN: no tailnet IPv4 yet (still connecting?)"
    fi
    # rc=0 means the control plane accepted the key; the address can arrive
    # a moment later.  Record the login so later boots skip the CLI.
    touch "$TS_DIR/.logged-in" 2>/dev/null
    # The CLI is only needed for this one login; free /tmp for the daemon.
    rm -f "$CLI_TMP" 2>/dev/null
    return 0
}

if ! wait_network; then
    exit 1
fi

if ! start_daemon; then
    save_debug
    exit 1
fi

login
rc=$?
[ "$rc" -eq 0 ] || save_debug
exit $rc
