---
name: vercel-cli
description: Use when deploying, linking, or managing Vercel projects via the Vercel CLI, including prod deployments, env vars, logs, and project configuration. Triggers: requests about `vercel` commands, deployments, project linking, domains, env management, or Vercel CLI setup.
---

# Vercel CLI

Use this skill to run Vercel CLI operations for deployments and project management.

## Workflow
1. Prefer the local CLI if available: `./node_modules/.bin/vercel`.
2. If running in CI or non-interactive contexts, always use `--token "$VERCEL_TOKEN"` (and `--scope "$VERCEL_SCOPE"` if set).
3. Default deploy command for production (non-interactive): `vercel --prod --yes --token "$VERCEL_TOKEN"`.
4. Parse the deployment URL from CLI output and persist it.
5. For domain purchase requests, **never buy immediately**. Quote first, ask user to proceed, then buy.

## Commands (common)
- Deploy prod (non-interactive): `vercel --prod --yes --token "$VERCEL_TOKEN"`
- Check version: `vercel --version`
- Env management: `vercel env ls|add|rm`
- Project info: `vercel project ls|inspect`
- Logs: `vercel logs <deployment-url>`

## Domain purchase flow
1. **Quote price/availability** (non-interactive):
   - `printf "n\n" | vercel domains buy <domain> [--token $VERCEL_TOKEN] [--scope $VERCEL_SCOPE]`
   - Parse output: if it shows “Buy now for $X (1yr)” and “available”, the domain is purchasable at that price.
2. **Ask user to proceed** with the quoted price.
3. **Collect payment** (Stripe link or other).
4. **Buy after payment**:
   - `printf "y\n" | vercel domains buy <domain> [--token $VERCEL_TOKEN] [--scope $VERCEL_SCOPE]`
5. **Log the purchase** in the DB for billing.

## References
- See `references/vercel-cli.md` for detailed command list and usage patterns.
