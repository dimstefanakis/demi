# Blue/Green (single VM)

This setup runs two API containers (blue/green) behind nginx, plus a separate worker container.
The active color is controlled by `deploy/nginx/conf.d/upstream.conf` and reloaded on switch.

## Prereqs
- Docker + Docker Compose on the host
- `.env` populated (see `.env.example`)
- `data/` directory persisted on disk
- Build the agent image used by the orchestrator:
  ```
  docker build -f docker/agent.Dockerfile -t claudius-agent:local .
  ```

## Start
```
docker compose up -d nginx worker api_blue
```

## Deploy (blue/green)
```
./scripts/deploy_blue_green.sh
```

## CI/CD
GitHub Actions workflow: `.github/workflows/deploy.yml`

Required secrets:
- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`

Optional secrets:
- `DEPLOY_PORT` (defaults to 22)
- `DEPLOY_PATH` (defaults to `/opt/claudius`)
- `DEPLOY_BRANCH` (defaults to `main`)

## Manually switch
```
./scripts/switch_blue_green.sh blue
./scripts/switch_blue_green.sh green
```

## Notes
- `worker` runs `claudius.worker_entrypoint` (no HTTP server).
- Agent containers still run on the host via the Docker socket.
- `MAIN_DB_BACKEND=supabase` is required for multi-instance safety.
