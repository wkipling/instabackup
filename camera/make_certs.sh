#!/bin/sh
# Generate a local CA and a TLS server certificate for the Insta360 camera
# cloud-upload redirect.
#
#   ./make_certs.sh [OUTPUT_DIR] [SERVER_IP]
#
# Produces:
#   ca.crt / ca.key          - install ca.crt on the camera (SD card)
#   ca.hash                  - OpenSSL subject hash for CAPATH (copy to SD)
#   server.crt / server.key  - use with inscloud_server.py --cert/--key
#
# The certificate covers the hostnames used by the camera firmware:
#   service-test.insta360.com   (default endpoint in instaAIP)
#   cloud.insta360.com          (production endpoint / ping)
#
# Validity is backdated to 2020-01-01 (when supported by the local OpenSSL).
# The camera gets its clock from the RTOS/app and can be behind real time
# after a long power-off; a certificate that starts "now" would then fail
# verification.  Backdating the CA and leaf removes that failure mode.
set -eu

OUT=${1:-certs}
IP=${2:-}
mkdir -p "$OUT"
cd "$OUT"

if [ -f ca.key ] && [ -f ca.crt ] && [ -f server.key ] && [ -f server.crt ]; then
    openssl x509 -in ca.crt -noout -subject_hash > ca.hash
    echo "certificates already exist in $PWD (delete them to regenerate)"
    exit 0
fi

echo "==> CA"
# OpenSSL 3.2+ can set explicit validity.  Older versions use the current time.
DATE_ARGS=""
if openssl req -help 2>&1 | grep -q -- '-not_before'; then
    DATE_ARGS="-not_before 20200101000000Z -not_after 20350101000000Z"
    echo "    validity: 2020-01-01 .. 2035-01-01 (backdated for camera clocks)"
else
    echo "    WARN: this OpenSSL cannot backdate; certificate starts now"
fi
# shellcheck disable=SC2086
openssl req -x509 -newkey rsa:2048 -nodes -sha256 $DATE_ARGS \
    -keyout ca.key -out ca.crt \
    -subj "/CN=Insta360 Local Backup CA/O=Local Backup" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1

echo "==> server key + CSR"
openssl req -newkey rsa:2048 -nodes -sha256 \
    -keyout server.key -out server.csr \
    -subj "/CN=service-test.insta360.com/O=Local Backup" >/dev/null 2>&1

SAN="DNS:service-test.insta360.com,DNS:cloud.insta360.com,DNS:*.insta360.com,DNS:insta360.com"
if [ -n "$IP" ]; then
    SAN="$SAN,IP:$IP"
fi

cat > server.ext <<EOF
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $SAN
EOF

echo "==> sign server certificate"
# shellcheck disable=SC2086
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    $DATE_ARGS -sha256 -extfile server.ext -out server.crt >/dev/null 2>&1

rm -f server.csr server.ext
openssl x509 -in ca.crt -noout -subject_hash > ca.hash

echo
echo "created:"
echo "  $PWD/ca.crt      -> copy to the SD card as ca.crt"
echo "  $PWD/ca.hash     -> copy to the SD card as ca.hash (CAPATH support)"
echo "  server.crt  -> inscloud_server.py --cert"
echo "  server.key  -> inscloud_server.py --key"
echo
openssl x509 -in server.crt -noout -startdate -enddate | sed 's/^/  /'
echo
echo "SAN: $SAN"
