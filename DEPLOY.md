# Deployment (GCE + blue/green)

This repo runs a single VM in GCE with:
- A Google HTTPS Load Balancer (managed certs).
- Two API containers (blue/green) behind nginx.
- A separate worker container for background jobs.
- A Supabase Postgres main DB (tenant SQLite remains local).

## 1) GCE networking (Load Balancer + DNS)

### Reserve a global LB IP
```bash
gcloud compute addresses create demi-lb-ip --global
gcloud compute addresses describe demi-lb-ip --global --format="get(address)"
```

### Create unmanaged instance group and add your VM
```bash
gcloud compute instance-groups unmanaged create demi-ig --zone us-central1-c
gcloud compute instance-groups unmanaged add-instances demi-ig \
  --zone us-central1-c \
  --instances instance-20260203-123701
gcloud compute instance-groups unmanaged set-named-ports demi-ig \
  --zone us-central1-c \
  --named-ports http:80
```

### Health check + backend service
```bash
gcloud compute health-checks create http demi-hc --port 80 --request-path /health
gcloud compute backend-services create demi-backend \
  --global --protocol HTTP --port-name http --health-checks demi-hc
gcloud compute backend-services add-backend demi-backend \
  --global --instance-group demi-ig --instance-group-zone us-central1-c
```

### URL map + HTTPS proxy + managed cert
```bash
gcloud compute url-maps create demi-map --default-service demi-backend
gcloud compute ssl-certificates create demi-cert-v2 \
  --domains hiredemi.com,www.hiredemi.com
gcloud compute target-https-proxies create demi-https-proxy \
  --ssl-certificates demi-cert-v2 --url-map demi-map
gcloud compute forwarding-rules create demi-https-fr \
  --global \
  --target-https-proxy demi-https-proxy \
  --ports 443 \
  --address demi-lb-ip
```

### Optional HTTP listener
```bash
gcloud compute target-http-proxies create demi-http-proxy --url-map demi-map
gcloud compute forwarding-rules create demi-http-fr \
  --global \
  --target-http-proxy demi-http-proxy \
  --ports 80 \
  --address demi-lb-ip
```

### Firewall rule for LB health checks
```bash
gcloud compute firewall-rules create demi-allow-lb \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:80 \
  --source-ranges=35.191.0.0/16,130.211.0.0/22 \
  --target-tags=demi
```

### DNS (Porkbun)
- A record `@` -> LB IP (global).
- CNAME `www` -> `hiredemi.com`.

Managed certs activate once DNS points to the LB IP.

## 2) VM bootstrap (one-time)

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
newgrp docker
```

## 3) GitHub deploy key (recommended)

Create a deploy key on the VM:
```bash
ssh-keygen -t ed25519 -C "demi-deploy" -f ~/.ssh/demi_deploy_key
chmod 600 ~/.ssh/demi_deploy_key
cat ~/.ssh/demi_deploy_key.pub
```

Add the public key in GitHub → Repo → Settings → Deploy Keys (read-only).

Configure SSH:
```bash
printf "Host github.com\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/demi_deploy_key\n  IdentitiesOnly yes\n" > ~/.ssh/config
chmod 600 ~/.ssh/config
ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts
ssh -T git@github.com
```

Clone:
```bash
sudo mkdir -p /opt/demi
sudo chown $USER:$USER /opt/demi
git clone git@github.com:dimstefanakis/demi.git /opt/demi
```

## 4) Environment
Copy production env:
```bash
cp /opt/demi/.env.production /opt/demi/.env
chmod 600 /opt/demi/.env
```

Minimum required:
- `MAIN_DB_BACKEND=supabase`
- `MAIN_DB_SUPABASE_URL=...`
- `MAIN_DB_SUPABASE_SERVICE_KEY=...`
- `TELEGRAM_BOT_TOKEN=...`
- `PUBLIC_BASE_URL=https://hiredemi.com`
- `EVENT_URL=https://hiredemi.com/events`

## 5) Build + start
```bash
cd /opt/demi
docker build -f docker/agent.Dockerfile -t demi-agent:local .
docker compose up -d --build nginx worker api_blue
```

## 6) Blue/green deploy
```bash
./scripts/deploy_blue_green.sh
```

## 7) Startup script (Option A)
Script: `scripts/gce_startup.sh`

Attach it:
```bash
gcloud compute instances add-metadata instance-20260203-123701 \
  --zone us-central1-c \
  --metadata-from-file startup-script=scripts/gce_startup.sh
```

Run on next boot (or manual):
```bash
sudo bash /opt/demi/scripts/gce_startup.sh
```

## 8) CI/CD (GitHub Actions)
Workflow: `.github/workflows/deploy.yml`

Required secrets:
- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`

Optional:
- `DEPLOY_PORT` (default 22)
- `DEPLOY_PATH` (default `/opt/demi`)
- `DEPLOY_BRANCH` (default `main`)

## How it works
- nginx routes traffic to `api_blue` or `api_green` via `deploy/nginx/conf.d/upstream.conf`.
- `scripts/deploy_blue_green.sh` builds the inactive color, health checks `/health`,
  then switches upstream and reloads nginx.
- `worker` runs `demi.worker_entrypoint` and handles outbox + pending runs.
- Main DB is Supabase; tenant SQLite stays under `/opt/demi/data`.
- Agent containers run via the Docker socket; main DB secrets are not forwarded into them.
