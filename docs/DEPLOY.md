# Deployment (GCE VPS)

This document covers the full deployment flow for the GCE single-VM blue/green setup,
including Supabase main DB usage and a worker container that is isolated from the API.

## How To Deploy

### 1) Prereqs
- A GCE VM (Ubuntu 22.04 or Debian) with inbound HTTP (port 80) allowed.
- Supabase project (remote) for the main DB.
- Repo cloned on the VM (recommended path: `/opt/demi`).
- Docker + Docker Compose installed on the VM.

### 2) GCE network setup (static IP + firewall)
You can do this in the GCP Console or via `gcloud`. Example CLI flow:

```bash
# Reserve a static external IP
gcloud compute addresses create demi-ip --region=YOUR_REGION
gcloud compute addresses describe demi-ip --region=YOUR_REGION --format="get(address)"

# Add an HTTP firewall rule (tagged)
gcloud compute firewall-rules create demi-http \
  --allow tcp:80 \
  --target-tags demi

# (Optional) add HTTPS as well
gcloud compute firewall-rules create demi-https \
  --allow tcp:443 \
  --target-tags demi
```

When creating the VM:
- Assign the reserved static IP.
- Add the network tag: `demi`.

If you already have a VM, attach the static IP and add the `demi` tag in the
instance settings, or via CLI:

```bash
gcloud compute instances add-tags YOUR_INSTANCE \
  --tags demi \
  --zone=YOUR_ZONE

gcloud compute instances delete-access-config YOUR_INSTANCE \
  --access-config-name="External NAT" \
  --zone=YOUR_ZONE

gcloud compute instances add-access-config YOUR_INSTANCE \
  --access-config-name="External NAT" \
  --address=demi-ip \
  --zone=YOUR_ZONE
```

DNS: point your domain A record to the static IP once it is attached to the VM.

### 3) Install Docker on the VM
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
newgrp docker
```

### 4) Clone the repo
```bash
sudo mkdir -p /opt/demi
sudo chown $USER:$USER /opt/demi
git clone git@github.com:YOUR_ORG/demi.git /opt/demi
cd /opt/demi
```

### 5) Configure environment
```bash
cp .env.example .env
```
Set at minimum:
- `MAIN_DB_SUPABASE_URL=...`
- `MAIN_DB_SUPABASE_SERVICE_KEY=...`
- `TELEGRAM_BOT_TOKEN=...`
- Other runtime secrets (Vercel, Unsplash, Stripe, etc)

Main DB credentials are forwarded into agent containers so they can load run context.

### 6) Build the agent image
```bash
docker build -f docker/agent.Dockerfile -t demi-agent:local .
```

### 7) Start the stack
```bash
docker compose up -d nginx worker api_blue
```

### 8) Deploy updates (blue/green)
```bash
./scripts/deploy_blue_green.sh
```

### 9) CI/CD (GitHub Actions)
Workflow: `.github/workflows/deploy.yml`

Required GitHub Secrets:
- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`

Optional GitHub Secrets:
- `DEPLOY_PORT` (default `22`)
- `DEPLOY_PATH` (default `/opt/demi`)
- `DEPLOY_BRANCH` (default `main`)

Pushes to `main` run the deploy script on the VM.

## How It Works

- Blue/green API: Two API containers (`api_blue`, `api_green`) are behind nginx.
  The active color is defined in `deploy/nginx/conf.d/upstream.conf`.
- Switching: `scripts/switch_blue_green.sh` updates the upstream and reloads nginx.
- Deploy: `scripts/deploy_blue_green.sh` builds the next color, waits for
  `GET /health`, then switches traffic.
- Worker isolation: `worker` runs `demi.worker_entrypoint` and is separate from
  API containers, so deployments do not interrupt request handling.
- Main DB: The orchestrator/worker use Supabase Postgres via
  `MAIN_DB_SUPABASE_URL` + `MAIN_DB_SUPABASE_SERVICE_KEY`. This keeps runs/messages shared across containers.
- Tenant scratchpad: `tenant.sqlite` may exist in each tenant workspace for execution-agent notes/cache only.
- Agent runtime: The API/worker use the Docker socket to run short-lived agent
  containers. Main DB credentials are forwarded so agents can load run context.
- Reliability: If a deploy restarts the worker, stale runs are re-queued and
  drained on the next worker loop, preventing stuck runs.
