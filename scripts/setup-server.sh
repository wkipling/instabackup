#!/bin/sh
# One-command receiver setup using Docker (no Compose required, no podman).
#
# On the Docker host:
#   ./scripts/setup-server.sh --data /mnt/user/MyShare/insta360-backup/sdcard
#
# It creates appdata/sd-token if needed, pulls the published image, and starts
# the receiver on port 8844.  Use --replace to recreate an existing container.
set -eu

IMAGE=${IMAGE:-ghcr.io/wkipling/instabackup:latest}
NAME=${NAME:-insta360-sink}
PORT=${PORT:-8844}
DATA=${DATA:-}
APPDATA=${APPDATA:-}
USERSPEC=${USERSPEC:-}
REPLACE=0
DRY=0

usage() {
    cat <<'EOF'
Usage: setup-server.sh [options]

  --data DIR       upload storage (default: ./data)
  --appdata DIR    receiver state/token (default: ./appdata)
  --port N         host port (default: 8844)
  --user UID:GID   runtime user (default: 99:100 if /mnt/user exists,
                   otherwise the current user)
  --replace        remove and recreate an existing container
  --dry-run        print the docker command, change nothing
  -h, --help       show this text

The receiver needs only Docker.  It does not use podman.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --data) DATA=${2:-}; shift 2 ;;
        --appdata) APPDATA=${2:-}; shift 2 ;;
        --port) PORT=${2:-}; shift 2 ;;
        --user) USERSPEC=${2:-}; shift 2 ;;
        --replace) REPLACE=1; shift ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$DATA" ] || DATA=$(pwd)/data
[ -n "$APPDATA" ] || APPDATA=$(pwd)/appdata
if [ -z "$USERSPEC" ]; then
    if [ -d /mnt/user ]; then
        USERSPEC=99:100
    else
        USERSPEC=$(id -u):$(id -g)
    fi
fi

if [ "$DRY" = "1" ]; then
    echo "docker run -d --name $NAME --restart unless-stopped --user $USERSPEC \\"
    echo "  -p $PORT:8844 -e SDCARD_ROOT=/data -e SDCARD_PORT=8844 \\"
    echo "  -e SDCARD_TOKEN_FILE=/app/sd-token \\"
    echo "  -v $APPDATA:/app:ro -v $DATA:/data $IMAGE"
    exit 0
fi

command -v docker >/dev/null 2>&1 || {
    echo "setup-server.sh: docker is required" >&2
    exit 1
}

mkdir -p "$DATA" "$APPDATA"
TOKEN="$APPDATA/sd-token"
if [ ! -s "$TOKEN" ]; then
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24 > "$TOKEN"
    else
        head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKEN"
        printf '\n' >> "$TOKEN"
    fi
    chmod 600 "$TOKEN" 2>/dev/null || true
    echo "created token: $TOKEN"
else
    echo "using token:   $TOKEN"
fi

if docker inspect "$NAME" >/dev/null 2>&1; then
    if [ "$REPLACE" = "1" ]; then
        echo "removing existing container $NAME"
        docker rm -f "$NAME" >/dev/null
    else
        docker start "$NAME" >/dev/null 2>&1 || true
        echo "container $NAME already exists; started it"
        echo "use --replace to recreate it with new settings"
        echo "dashboard: http://<server>:$PORT/"
        echo "token:     $TOKEN"
        exit 0
    fi
fi

docker pull "$IMAGE"
docker run -d --name "$NAME" --restart unless-stopped --user "$USERSPEC" \
    -p "$PORT:8844" \
    -e SDCARD_ROOT=/data \
    -e SDCARD_PORT=8844 \
    -e SDCARD_TOKEN_FILE=/app/sd-token \
    -v "$APPDATA:/app:ro" \
    -v "$DATA:/data" \
    "$IMAGE" >/dev/null

echo
echo "receiver started"
echo "  dashboard: http://<server>:$PORT/"
echo "  token:     $TOKEN"
echo "  storage:   $DATA"
echo
echo "Next: python3 camera/setup_tui.py"
echo "      use token file: $TOKEN"
