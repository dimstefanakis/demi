---
name: vercel-cli
description: Use when deploying, linking, or managing Vercel projects via the Vercel CLI, including prod deployments, env vars, logs, and project configuration. Triggers: requests about `vercel` commands, deployments, project linking, domains, env management, or Vercel CLI setup.
---

# Vercel CLI

Use this skill to run Vercel CLI operations for deployments and project management.

## Workflow
1. Prefer the local CLI if available: `./node_modules/.bin/vercel`.
2. If running in CI or non-interactive contexts, always use `--token`.
3. Default deploy command for production: `vercel --prod --yes`.
4. Parse the deployment URL from CLI output and persist it.

## Commands (common)
- Deploy prod: `vercel --prod --yes`
- Check version: `vercel --version`
- Env management: `vercel env ls|add|rm`
- Project info: `vercel project ls|inspect`
- Logs: `vercel logs <deployment-url>`

## References
- See `references/vercel-cli.md` for detailed command list and usage patterns.
