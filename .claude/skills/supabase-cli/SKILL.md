---
name: supabase-cli
description: Use when managing Supabase projects or migrations via the Supabase CLI, including local dev, migrations, and remote push/pull workflows.
---

# Supabase CLI

Use this skill for Supabase CLI workflows (local dev, migrations, linking, push/pull).

## Workflow
1. Prefer local development workflows before pushing changes remotely.
2. Keep migrations in `supabase/migrations` and treat them as the source of truth.
3. Use `supabase db push` to apply migrations to remote projects.
4. Use `supabase db pull` to sync remote schema back to local migrations when needed.

## References
- See `references/supabase-cli.md` for the full CLI reference.
