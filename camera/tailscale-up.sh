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

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') tailscale: $*" >> "$LOG" 2>/dev/null
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
CLI_RUN="$CLI"

# Verify the file where it is first.  A matching hash means the bytes the
# kernel will execute were just read correctly, so no tmpfs copy is needed
# (and /tmp stays free for the rest of the system).
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

if ! use_verified "$TSD" tailscale/tailscaled; then
    mkdir -p /tmp/inscloud-tailscale 2>/dev/null
    TSD_RUN=/tmp/inscloud-tailscale/tailscaled
    if ! copy_verified "$TSD" tailscale/tailscaled "$TSD_RUN"; then
        log "ERROR: no verified tailscaled copy; refusing to run it"
        exit 1
    fi
fi

if ! use_verified "$CLI" tailscale/tailscale; then
    mkdir -p /tmp/inscloud-tailscale 2>/dev/null
    CLI_RUN=/tmp/inscloud-tailscale/tailscale
    if ! copy_verified "$CLI" tailscale/tailscale "$CLI_RUN"; then
        # The 25 MB CLI has been observed to return different bytes on every
        # read on this card, while tailscaled (36 MB) reads correctly.  The
        # daemon and its saved node state are enough to bring up the tunnel;
        # only a fresh login would need the CLI.
        log "WARN: tailscale CLI unreadable; continuing with saved node state"
        CLI_RUN=""
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

login() {
    if [ -z "$CLI_RUN" ]; then
        log "no usable CLI; relying on saved node state"
        return 0
    fi

    # Never run `tailscale up` on a node that is already Running: the CLI
    # has been observed to segfault when it is asked to re-apply an
    # unchanged profile, and a watchdog retry used to trigger exactly that.
    state=$("$CLI_RUN" --socket="$SOCK" status --json 2>/dev/null \
        | grep -o '"BackendState"[[:space:]]*:[[:space:]]*"[A-Za-z]*"' \
        | head -n 1)
    case "$state" in
        *Running*)
            log "node already Running; not re-running tailscale up"
            return 0
            ;;
    esac

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
    return 0
}

if ! wait_network; then
    exit 1
fi

if ! start_daemon; then
    exit 1
fi

login
exit $?
