#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_FILE="${ROOT_DIR}/deploy/nginx/conf.d/upstream.conf"

current_color="blue"
if [[ -f "${UPSTREAM_FILE}" ]] && grep -q "api_green" "${UPSTREAM_FILE}"; then
  current_color="green"
fi

if [[ "${current_color}" == "blue" ]]; then
  target_color="green"
else
  target_color="blue"
fi

echo "Current: ${current_color}. Deploying ${target_color}."

docker build -f docker/agent.Dockerfile -t demi-agent:local .
docker compose up -d --build nginx worker
docker compose up -d --build "api_${target_color}"

# Wait for health
for i in {1..30}; do
  if docker compose exec -T "api_${target_color}" curl -fs http://127.0.0.1:8000/health >/dev/null; then
    echo "${target_color} is healthy."
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "${target_color} did not become healthy." >&2
    exit 1
  fi
  sleep 2
done

"${ROOT_DIR}/scripts/switch_blue_green.sh" "${target_color}"

# Optional: stop old api container to save resources
# docker compose stop "api_${current_color}"

echo "Pruning unused Docker resources..."
docker container prune -f || true
docker image prune -af || true
docker builder prune -af || true

echo "Deploy complete."
