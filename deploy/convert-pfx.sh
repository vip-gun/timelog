#!/usr/bin/env sh
# Convert the PFX file issued by the PKI into certs/server.crt + certs/server.key, using the application image
# (no openssl needed). Usage: ./convert-pfx.sh /path/to/server.pfx     (asks for the PFX password)
set -eu
cd "$(dirname "$0")"
PFX="${1:?usage: $0 <file.pfx>}"
VERSION=$(cat VERSION)
mkdir -p certs
cp "$PFX" certs/.import.pfx
trap 'rm -f certs/.import.pfx' EXIT
docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD/certs:/work:z" --entrypoint python "timelog-app:$VERSION" \
  -m timelog.certtool pfx2pem /work/.import.pfx /work
chmod 600 certs/server.key
echo "Now apply it:  docker compose restart proxy"
