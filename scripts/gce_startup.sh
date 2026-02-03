#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/demi"
BRANCH="main"

apt-get update
apt-get install -y docker.io git curl ca-certificates

if ! docker compose version >/dev/null 2>&1; then
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL \
    https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

if [ ! -d "${REPO_DIR}/.git" ]; then
  echo "Repo missing at ${REPO_DIR}. Clone it manually first." >&2
  exit 1
fi

cd "${REPO_DIR}"
git fetch --all --prune
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"

if [ -f "${REPO_DIR}/.env.production" ]; then
  cp "${REPO_DIR}/.env.production" "${REPO_DIR}/.env"
  chmod 600 "${REPO_DIR}/.env"
fi

docker build -f docker/agent.Dockerfile -t demi-agent:local .
docker compose up -d --build nginx worker api_blue
