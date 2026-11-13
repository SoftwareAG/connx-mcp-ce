#!/usr/bin/env bash
# Seed the test stack's HashiCorp Vault for the Credential Vault.
#
# TEST AND DEMO ONLY - never part of the product image (see docker-compose.yml). Runs in the `seed`
# container; its configuration comes from that container's environment, and its entries from
# ENTRIES_FILE, which render.py made from the users file. It needs only curl and jq and talks to the
# HTTP API, never the vault CLI.
#   seed.sh                 seed once (idempotent)
#   seed.sh --loop          seed, then keep it seeded: re-seed whenever the read-only token is gone (dev mode is
#                           in-memory, so a Vault restart empties it). Touches /tmp/seeded after the first success.
#                           Every start of the loop seeds, so restarting the container applies a changed entries file
#   seed.sh --show-entries  print each entry's subject, identity and CONNX login (password only as set/empty) and
#                           exit, without touching the network
#
# What it provisions, exactly as a customer's administrator would (the Credential Vault setup guide):
#   1. the read-only policy connx-mcp-read on the callers prefix
#   2. a token under that policy, with the fixed id VAULT_READ_TOKEN - the server under test and the
#      vault_live tests use THIS token, never root, so the policy is what gets exercised
#   3. every entry in ENTRIES_FILE: one per mapped identity per identity provider, keyed by the subject its
#      tokens carry, plus the two deliberately bad ones for the deny paths (wrong issuer, no password); and
#      it deletes any other entry under the callers prefix, so an identity taken out of the users file is gone
#
# The subjects are not looked up: render.py pins them (Keycloak's user id, dex's subject computed from it), and
# the live subject check proves them against real tokens.
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-https://vault:8200}"
ROOT_TOKEN="${VAULT_ROOT_TOKEN:-connx-mcp-dev-root}"
READ_TOKEN="${VAULT_READ_TOKEN:-connx-mcp-dev-read}"
ENTRIES_FILE="${ENTRIES_FILE:-/entries/entries.json}"
CALLERS="connx-mcp/callers"
CHECK_SECONDS="${CHECK_SECONDS:-10}"

CURL_TLS=()
if [ -n "${VAULT_CACERT:-}" ]; then
  CURL_TLS=(--cacert "${VAULT_CACERT}")
fi

read_token_exists() {
  # True when HashiCorp Vault answers and the read-only token is still there. curl's own error (a
  # refused connection, a certificate the CA does not sign) reaches the log, so a TLS problem is not
  # mistaken for a store that merely needs seeding.
  curl -sS -o /dev/null -w '%{http_code}' "${CURL_TLS[@]}" -H "X-Vault-Token: ${ROOT_TOKEN}" -X POST \
    --data "$(jq -n --arg t "${READ_TOKEN}" '{token: $t}')" "${VAULT_ADDR}/v1/auth/token/lookup" \
    | grep -q '^200$'
}

if [ "${1:-}" = "--loop" ]; then
  rm -f /tmp/seeded
  seeded=no
  while true; do
    if [ "${seeded}" = no ] || ! read_token_exists; then
      if "$0"; then
        touch /tmp/seeded
        seeded=yes
      else
        echo "seed.sh: seeding failed; retrying in ${CHECK_SECONDS}s" >&2
      fi
    fi
    sleep "${CHECK_SECONDS}"
  done
fi

if [ ! -r "${ENTRIES_FILE}" ] || ! jq -e 'type == "array"' "${ENTRIES_FILE}" >/dev/null 2>&1; then
  echo "seed.sh: ${ENTRIES_FILE} is missing or not a JSON list - run startContainers.sh, which renders it" >&2
  exit 1
fi
if [ "${1:-}" = "--show-entries" ]; then
  jq -r '.[] | "\(.subject) identity=\(.identity // "-") provider=\(.provider // "-") username=\(.data.username)"
    + " password=\(if .data | has("password") | not then "absent" elif .data.password == "" then "empty" else "set" end)"' \
    "${ENTRIES_FILE}"
  exit 0
fi

vault_api() {
  # vault_api METHOD PATH [JSON-BODY] - as root; prints the response body.
  local method="$1" path="$2" body="${3:-}"
  if [ -n "${body}" ]; then
    curl -sS -f "${CURL_TLS[@]}" -X "${method}" -H "X-Vault-Token: ${ROOT_TOKEN}" \
      -H "Content-Type: application/json" --data "${body}" "${VAULT_ADDR}/v1/${path}"
  else
    curl -sS -f "${CURL_TLS[@]}" -X "${method}" -H "X-Vault-Token: ${ROOT_TOKEN}" "${VAULT_ADDR}/v1/${path}"
  fi
}

echo "seed.sh: waiting for ${VAULT_ADDR}"
for _ in $(seq 1 30); do
  curl -sS -f "${CURL_TLS[@]}" "${VAULT_ADDR}/v1/sys/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -sS -f "${CURL_TLS[@]}" "${VAULT_ADDR}/v1/sys/health" >/dev/null \
  || { echo "seed.sh: ${VAULT_ADDR} not reachable" >&2; exit 1; }

# 1. Read-only policy - the one the customer doc tells an administrator to create, verbatim
#    (the MCP server repository's unit tests pin the literal, so keep it a literal).
POLICY_HCL=$(cat <<HCL
path "secret/data/connx-mcp/callers/*" {
  capabilities = ["read"]
}
path "secret/metadata/connx-mcp/callers/*" {
  capabilities = ["list"]
}
HCL
)
vault_api PUT "sys/policies/acl/connx-mcp-read" "$(jq -n --arg p "${POLICY_HCL}" '{policy: $p}')" >/dev/null
echo "seed.sh: policy connx-mcp-read written"

# 2. The read-only token, under a fixed id so nothing has to parse this script's output.
if read_token_exists; then
  echo "seed.sh: token ${READ_TOKEN} already exists"
else
  vault_api POST "auth/token/create" \
    "$(jq -n --arg id "${READ_TOKEN}" '{id: $id, policies: ["connx-mcp-read"], ttl: "720h"}')" >/dev/null
  echo "seed.sh: token ${READ_TOKEN} minted"
fi

# 3. The entries, as render.py wrote them. The subject is the entry's key; `data` is the entry itself.
count=0
while IFS= read -r entry; do
  subject=$(jq -r .subject <<<"${entry}")
  vault_api POST "secret/data/${CALLERS}/${subject}" "$(jq -c '{data: .data}' <<<"${entry}")" >/dev/null
  echo "seed.sh: entry $(jq -r '"\(.subject) (\(.identity // "bad entry")\(if .provider then ", " + .provider else "" end)) -> CONNX user \(.data.username)"' <<<"${entry}")"
  count=$((count + 1))
done < <(jq -c '.[]' "${ENTRIES_FILE}")

# 4. Entries the file no longer has: an identity removed from the users file must stop resolving.
existing=$(vault_api LIST "secret/metadata/${CALLERS}" 2>/dev/null | jq -r '.data.keys[]?' || true)
for subject in ${existing}; do
  if ! jq -e --arg s "${subject}" 'any(.[]; .subject == $s)' "${ENTRIES_FILE}" >/dev/null; then
    vault_api DELETE "secret/metadata/${CALLERS}/${subject}" >/dev/null
    echo "seed.sh: entry ${subject} deleted (not in the entries file)"
  fi
done
echo "seed.sh: done, ${count} entries"
