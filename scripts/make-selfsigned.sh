#!/usr/bin/env sh
# Development only: self-signed certificate so the stack starts before the PKI certificate is issued.
# Usage: scripts/make-selfsigned.sh timelog.local
set -eu
FQDN="${1:-localhost}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$DIR"
CONF="$(mktemp)"
trap 'rm -f "$CONF"' EXIT
cat > "$CONF" <<CFG
[req]
prompt = no
distinguished_name = dn
x509_extensions = ext
[dn]
CN = $FQDN
[ext]
subjectAltName = DNS:$FQDN, DNS:localhost, IP:127.0.0.1
extendedKeyUsage = serverAuth
CFG
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 365 \
  -keyout "$DIR/server.key" -out "$DIR/server.crt" -config "$CONF"
chmod 600 "$DIR/server.key"
echo "Self-signed certificate written to $DIR/server.crt (valid 365 days, CN=$FQDN)"
