#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/root}"
mkdir -p "${HOME}"

REPO_DIR="/opt/demi"
BRANCH="main"

apt-get update
apt-get install -y docker.io git curl ca-certificates python3

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

# Avoid git "dubious ownership" failures in startup scripts.
git config --global --add safe.directory "${REPO_DIR}"

REMOTE_URL="$(git remote get-url origin)"
if [[ "${REMOTE_URL}" =~ ^git@([^:]+):(.+)$ ]]; then
  REMOTE_URL="https://${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
  git remote set-url origin "${REMOTE_URL}"
fi

GIT_PAT="${GIT_PAT:-}"
GIT_PAT_SECRET_NAME="${GIT_PAT_SECRET_NAME:-demi-git-pat}"
GIT_USERNAME="${GIT_USERNAME:-x-access-token}"

if [ -z "${GIT_PAT}" ]; then
  PROJECT_ID="$(
    curl -fsS -H "Metadata-Flavor: Google" \
      "http://metadata.google.internal/computeMetadata/v1/project/project-id"
  )"
  ACCESS_TOKEN="$(
    curl -fsS -H "Metadata-Flavor: Google" \
      "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
    | python3 - <<'PY'
import json, sys
print(json.load(sys.stdin)["access_token"])
PY
  )"
  GIT_PAT="$(
    curl -fsS -H "Authorization: Bearer ${ACCESS_TOKEN}" \
      "https://secretmanager.googleapis.com/v1/projects/${PROJECT_ID}/secrets/${GIT_PAT_SECRET_NAME}/versions/latest:access" \
    | python3 - <<'PY'
import base64, json, sys
payload = json.load(sys.stdin)["payload"]["data"]
print(base64.b64decode(payload).decode("utf-8"))
PY
  )"
fi

if [ -z "${GIT_PAT}" ]; then
  echo "GIT_PAT not set and Secret Manager lookup failed." >&2
  exit 1
fi

AUTH_B64="$(printf '%s:%s' "${GIT_USERNAME}" "${GIT_PAT}" | base64 | tr -d '\n')"
GIT_AUTH_HEADER="Authorization: Basic ${AUTH_B64}"

git -c http.extraHeader="${GIT_AUTH_HEADER}" fetch --all --prune
git -c http.extraHeader="${GIT_AUTH_HEADER}" checkout "${BRANCH}"
git -c http.extraHeader="${GIT_AUTH_HEADER}" pull --ff-only origin "${BRANCH}"

if [ -f "${REPO_DIR}/.env.production" ]; then
  cp "${REPO_DIR}/.env.production" "${REPO_DIR}/.env"
  chmod 600 "${REPO_DIR}/.env"
fi

docker build -f docker/agent.Dockerfile -t demi-agent:local .
COMPOSE_PARALLEL_LIMIT=1 docker compose up -d --build nginx worker api_blue api_green

docker container prune -f || true
docker image prune -f || true
docker builder prune -af || true
docker compose exec -T api_blue python scripts/cleanup_pool_slots.py \
  --stop-pool-containers || docker compose exec -T api_green python \
  scripts/cleanup_pool_slots.py --stop-pool-containers || true
