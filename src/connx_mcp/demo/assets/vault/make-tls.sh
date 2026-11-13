#!/usr/bin/env bash
# Create the HashiCorp Vault TLS material in /out (generated/tls on the host); startContainers.sh runs it in the seed image.
#
# TEST AND DEMO ONLY - never part of the product image (see docker-compose.yml).
#
#   ca.pem / ca-key.pem          a self-signed CA, created once and then kept, so a remote MCP server's
#                                VAULT_CACERT stays valid across restarts and host changes
#   server.pem / server-key.pem  Vault's certificate, re-issued when the SAN list changes or it is near expiry
#
# SANs: localhost, 127.0.0.1, vault (the seed's in-network name) and STACK_HOST.
set -euo pipefail

OUT=/out
STACK_HOST="${STACK_HOST:-localhost}"
cd "${OUT}"

if [[ "${STACK_HOST}" =~ ^[0-9]+(\.[0-9]+){3}$ || "${STACK_HOST}" == *:* ]]; then
  HOST_SAN="IP:${STACK_HOST}"
else
  HOST_SAN="DNS:${STACK_HOST}"
fi
SANS="DNS:localhost,IP:127.0.0.1,DNS:vault,${HOST_SAN}"

if [ ! -s ca.pem ] || [ ! -s ca-key.pem ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -sha256 \
    -keyout ca-key.pem -out ca.pem -subj "/CN=CONNX MCP test stack CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
  echo "make-tls.sh: CA created (generated/tls/ca.pem)"
fi

reissue=no
if [ ! -s server.pem ] || [ ! -s server-key.pem ] || [ "$(cat sans.txt 2>/dev/null)" != "${SANS}" ]; then
  reissue=yes
elif ! openssl x509 -in server.pem -noout -checkend $((30 * 24 * 3600)) >/dev/null; then
  reissue=yes
elif ! openssl verify -CAfile ca.pem server.pem >/dev/null 2>&1; then
  reissue=yes
fi

if [ "${reissue}" = yes ]; then
  cat >server.ext <<EXT
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${SANS}
EXT
  openssl req -new -newkey rsa:2048 -nodes -keyout server-key.pem -out server.csr \
    -subj "/CN=${STACK_HOST}"
  openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial \
    -days 825 -sha256 -extfile server.ext -out server.pem
  rm -f server.csr server.ext ca.srl
  echo "${SANS}" >sans.txt
  echo "make-tls.sh: Vault certificate issued for ${SANS}"
else
  echo "make-tls.sh: Vault certificate current for ${SANS}"
fi

# Vault runs as an unprivileged user and must read its own key; this is throwaway test material.
# The CA key is needed by nothing but this script.
chmod 644 ca.pem server.pem server-key.pem sans.txt
chmod 600 ca-key.pem
if [ -n "${HOST_UID:-}" ]; then
  chown "${HOST_UID}:${HOST_GID:-${HOST_UID}}" ./*.pem sans.txt
fi
