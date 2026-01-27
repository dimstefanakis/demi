---
name: bun-next-shadcn
description: Bootstrap a Next.js app with Bun and initialize shadcn UI. Use when setting up new site codebases or when the user asks to create a new Next.js project with Bun and shadcn.
---

# Bun + Next.js + shadcn Bootstrap

Use this skill to create a fresh Next.js app using Bun and initialize shadcn UI.

## Workflow
1. Always operate inside the tenant workspace `site/` directory.
2. Create the app using Bun (no npm/yarn/pnpm):
   - `bun create next-app@latest <app-name> --yes`
3. Enter the app directory:
   - `cd <app-name>`
4. Initialize shadcn with Bun:
   - `bunx --bun shadcn@latest init`
5. Save the chosen app name to `tasks/app_name.txt`.

## Notes
- Keep the app name short and relevant to the business.
- Do not implement UI here; Gemini handles design.
