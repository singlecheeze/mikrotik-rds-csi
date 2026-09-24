#!/usr/bin/env bash
set -euo pipefail

: "${RDS_API_ENDPOINT:?set RDS_API_ENDPOINT, e.g. https://rds.example.com}"
: "${RDS_USERNAME:?set RDS_USERNAME}"
: "${RDS_POOL_SLOT:?set RDS_POOL_SLOT to the mounted RouterOS disk/pool slot}"

RDS_TLS_VERIFY="${RDS_TLS_VERIFY:-true}"
RDS_CA_FILE="${RDS_CA_FILE:-}"

read -rsp "RouterOS password for ${RDS_USERNAME}: " RDS_PASSWORD
echo

curl_tls=()
case "${RDS_TLS_VERIFY,,}" in
  true|yes|1|on)
    if [[ -n "${RDS_CA_FILE}" ]]; then
      curl_tls+=(--cacert "${RDS_CA_FILE}")
    fi
    ;;
  false|no|0|off)
    curl_tls+=(-k)
    ;;
  *)
    echo "RDS_TLS_VERIFY must be true or false" >&2
    exit 2
    ;;
esac

curl --fail --silent --show-error \
  "${curl_tls[@]}" \
  -u "${RDS_USERNAME}:${RDS_PASSWORD}" \
  "${RDS_API_ENDPOINT%/}/rest/disk" |
  jq --arg pool "${RDS_POOL_SLOT}" '
    .[] |
    select(.slot == $pool or .type == "file") |
    {
      slot,
      type,
      fs,
      mounted,
      state,
      free,
      file_path:."file-path",
      file_size:."file-size",
      exported:."nvme-tcp-export",
      nqn:."nvme-tcp-server-nqn",
      port:."nvme-tcp-server-port"
    }'
