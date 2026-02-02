#!/usr/bin/env bash
set -euo pipefail

COLOR=${1:-}
if [[ "${COLOR}" != "blue" && "${COLOR}" != "green" ]]; then
  echo "Usage: $0 blue|green" >&2
  exit 1
fi

CONF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/nginx/conf.d"
UPSTREAM_FILE="${CONF_DIR}/upstream.conf"

cat > "${UPSTREAM_FILE}" <<EOF
upstream demi_upstream {
    server api_${COLOR}:8000;
}
EOF

docker compose exec -T nginx nginx -s reload

echo "Switched to ${COLOR}."
