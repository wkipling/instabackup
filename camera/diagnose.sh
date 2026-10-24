#!/bin/sh
# Write a one-shot diagnostic dump to <SD>/diag.log.
#
# Enable with CLOUD_DIAG=1 in cloud-backup.conf.  bootup.sh runs this ~60 s
# after start (CLOUD_DIAG_DELAY), when Wi-Fi and Tailscale have had a chance to
# come up.  Everything is read-only except the log file itself.
#
# If uploads do not start, send this file together with bootup.log.

SD=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
[ -n "$SD" ] || SD=/tmp/SD0
OUT="$SD/diag.log"
CONF="$SD/cloud-backup.conf"

[ -f "$CONF" ] && . "$CONF"

exec >>"$OUT" 2>&1

say() {
    echo
    echo "===== $* ====="
}

have() {
    command -v "$1" >/dev/null 2>&1
}

echo
echo "############################################################"
echo "# cloud-backup diagnostics"
echo "# $(date 2>/dev/null)"
echo "############################################################"

say "system"
uname -a 2>/dev/null
uptime 2>/dev/null

say "config (secrets redacted)"
if [ -f "$CONF" ]; then
    sed -e 's/^\(TAILSCALE_AUTHKEY=\).*/\1<redacted>/' "$CONF"
else
    echo "missing $CONF"
fi

say "files on SD"
ls -l "$SD" 2>/dev/null
ls -l "$SD/tailscale" 2>/dev/null

say "station-mode Wi-Fi"
if [ -f "$SD/wifi-sta.conf" ]; then
    sed -e 's/^WIFI_PASSWORD=.*/WIFI_PASSWORD=<redacted>/' \
        "$SD/wifi-sta.conf" 2>/dev/null
else
    echo "no wifi-sta.conf (AP mode / app-managed Wi-Fi)"
fi
if have iw; then
    iw dev wlan0 link 2>&1
    echo "-- visible SSIDs (wlan0 scan)"
    iw dev wlan0 scan 2>/dev/null | sed -n 's/.*SSID: //p' | sort -u | head -30
fi
if have wpa_cli; then
    wpa_cli -i wlan0 status 2>&1
fi
echo "-- AP/wpa process and config files"
ps 2>/dev/null | grep -E '[h]ostapd|[w]pa_supplicant'
ls -l /pref/wifi.conf /tmp/hostapd.conf /tmp/wpa_supplicant.ap.conf 2>&1
if [ -f /tmp/hostapd.conf ]; then
    echo "-- hostapd.conf (AP PSK redacted)"
    sed -e 's/^wpa_passphrase=.*/wpa_passphrase=<redacted>/' \
        /tmp/hostapd.conf 2>/dev/null
fi

say "interfaces"
ip addr 2>/dev/null
ip route 2>/dev/null

say "resolver"
cat /etc/resolv.conf 2>/dev/null
echo "-- hosts entries"
grep -i insta360 /etc/hosts 2>/dev/null
echo "-- 8.8.8.8 on lo"
ip addr show dev lo 2>/dev/null | grep 8.8.8.8

say "mounts"
mount 2>/dev/null | grep -E 'inscloud|/etc/hosts|/etc/ssl/certs|cacert|ca-bundle'

say "dnsmasq"
cat /tmp/inscloud-dns.pid 2>/dev/null
ps 2>/dev/null | grep '[d]nsmasq'
if have netstat; then
    echo "-- UDP listeners (port 53)"
    netstat -lnu 2>&1 | grep -E 'Local Address|:53' || netstat -lnu 2>&1
fi
if have nslookup; then
    echo "-- nslookup via 8.8.8.8 (canary)"
    nslookup inscloud-probe.insta360.com 8.8.8.8 2>&1
    echo "-- nslookup via 8.8.8.8 (real hosts)"
    nslookup service-test.insta360.com 8.8.8.8 2>&1
    nslookup cloud.insta360.com 8.8.8.8 2>&1
fi
if [ -f /tmp/inscloud-dns.log ]; then
    echo "-- dnsmasq log (tail)"
    tail -n 20 /tmp/inscloud-dns.log
fi

say "instaAIP and SysV queue"
pidof instaAIP 2>/dev/null || echo "instaAIP NOT RUNNING"
ipcs -q 2>/dev/null

say "listeners"
if have netstat; then
    netstat -lntp 2>/dev/null
fi

say "cloudkick"
ls -l /tmp/cloudkick 2>/dev/null

say "tailscale"
if [ -x "$SD/tailscale/tailscale" ]; then
    pidof tailscaled 2>/dev/null || echo "tailscaled NOT RUNNING"
    cat /tmp/inscloud-tailscaled.pid 2>/dev/null
    "$SD/tailscale/tailscale" --socket=/tmp/tailscaled.sock status 2>&1
    "$SD/tailscale/tailscale" --socket=/tmp/tailscaled.sock ip -4 2>&1
else
    echo "tailscale binaries not present (lan mode?)"
fi

say "tailrelay"
cat /tmp/inscloud-tailrelay.pid 2>/dev/null
if [ -n "${TAILNET_TARGET:-}" ] && [ -x "$SD/tailrelay" ]; then
    "$SD/tailrelay" --probe "$TAILNET_TARGET" "${SERVER_PORT:-443}" \
        127.0.0.1 "${TAILSCALE_PROXY_PORT:-8080}" 2>&1
    echo "probe rc=$?"
fi

say "media"
ls /tmp/SD0/DCIM 2>/dev/null | head -20
find /tmp/SD0/DCIM -type f 2>/dev/null | wc -l

say "space"
df -h /tmp /tmp/SD0 2>/dev/null

say "x4 preload log (tail)"
tail -n 20 /tmp/x4preload.log 2>/dev/null

say "bootup.log (tail)"
tail -n 80 "$SD/bootup.log" 2>/dev/null

say "station-mode log (tail, PSK redacted)"
if [ -f /tmp/inscloud-wifi.log ]; then
    sed -e 's/psk=.*/psk=<redacted>/' \
        -e 's/sae_password=.*/sae_password=<redacted>/' \
        /tmp/inscloud-wifi.log > "$SD/wifi-diag.log" 2>/dev/null
    tail -n 80 "$SD/wifi-diag.log" 2>/dev/null
else
    echo "no /tmp/inscloud-wifi.log"
fi

say "tailscaled log (tail)"
tail -n 40 /tmp/inscloud-tailscaled.log 2>/dev/null

say "tailrelay log (tail)"
tail -n 40 /tmp/inscloud-tailrelay.log 2>/dev/null

say "kernel messages (tail)"
dmesg 2>/dev/null | tail -n 20

if have logread; then
    say "syslog: ins_cloud"
    logread 2>/dev/null | grep -i 'ins_cloud' | tail -n 40
fi

say "end"
