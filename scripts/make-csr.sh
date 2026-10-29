#!/usr/bin/env sh
# Generate a private key + CSR for the Microsoft PKI (AD CS).
# Usage: scripts/make-csr.sh timelog.corp.example.lu [extra-dns-name ...]
set -eu
FQDN="${1:?usage: $0 <fqdn> [extra SAN dns names...]}"
shift
DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$DIR"
CONF="$(mktemp)"
trap 'rm -f "$CONF"' EXIT

SANS="DNS.1 = $FQDN"
i=2
for name in "$@"; do SANS="$SANS
DNS.$i = $name"; i=$((i + 1)); done

cat > "$CONF" <<CFG
[req]
default_bits = 3072
prompt = no
distinguished_name = dn
req_extensions = ext
[dn]
CN = $FQDN
[ext]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
$SANS
CFG

openssl req -new -newkey rsa:3072 -nodes -sha256 -keyout "$DIR/server.key" -out "$DIR/server.csr" -config "$CONF"
chmod 600 "$DIR/server.key"
cat <<MSG

Created $DIR/server.key and $DIR/server.csr

Next steps (on a Windows host enrolled in the domain):
  certreq -submit -attrib "CertificateTemplate:WebServer" server.csr server.cer
  (or paste the CSR in the AD CS web enrolment page /certsrv)

Then build the PEM chain expected by nginx (server first, then issuing CA, then root optional):
  openssl x509 -inform DER -in server.cer -out server.pem   # skip if already Base64/PEM
  cat server.pem issuing-ca.pem > certs/server.crt
Restart:  docker compose restart proxy
MSG
