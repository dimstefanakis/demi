# Repository Guidelines

## Product & Architecture Overview
- Telegram-first (WhatsApp later) chat agent that builds and edits SMB websites via conversation.
- Core flow: chat orchestrator → agent runtime (Claude) → Gemini CLI with `DESIGN.md` → build → Vercel deploy.
- Per-tenant root lives under `data/` and includes `projects/<project_name>/` which contains
  `memory.md`, `DESCRIPTION.md`, `tasks/`, `assets/`, and `site/` per project.
- Product constraints to preserve: no templates, fast first result, edit-by-chat loop, Unsplash-sourced images (no AI art).

## Project Structure & Module Organization
- `src/demi/` is the main package: `app.py` (FastAPI), `orchestrator.py`, `agent/`, `messaging/`, `workspace/`, `db/`, `domains/`, `payments/`.
- `prompts/` holds agent prompt files used at runtime.
- `tests/` contains pytest suites (e.g., `test_orchestrator_flow.py`, `test_telegram_parser.py`).
- Root docs (`PRODUCT.md`, `PRD.md`, `IMPLEMENTATION.md`, `DESIGN.md`) define product scope and prompt contracts.
- `docs/SPEC.md` is the living architecture spec for Demi.

## Documentation Hygiene
- Update `docs/SPEC.md` whenever changes affect architecture, message flow, storage, runtimes, or deployment.

## Build, Test, and Development Commands
- Install Python deps with uv:
  - `uv sync`
- Install CLI tooling for Gemini/Vercel (local `node_modules/.bin`):
  - `bun install`
- Run the API locally (via uv):
  - `uv run uvicorn demi.app:app --reload`
  - Requires `TELEGRAM_BOT_TOKEN` in `.env`.
- Run tests:
  - `uv run pytest`

## Coding Style & Naming Conventions
- Python 3.10+, 4-space indentation, max line length 100 (`pyproject.toml`).
- Prefer type hints and `from __future__ import annotations` as used in existing modules.
- Files in `snake_case.py`, classes in `PascalCase`, constants in `UPPER_SNAKE_CASE`.
- Centralize settings in `src/demi/config.py` (`Settings`), avoid ad-hoc env reads.
- Tooling policy: use `bun` for JS tooling and `uv` for Python; do not use `npm`/`pip`.

## Testing Guidelines
- Framework: `pytest` with `pytest-asyncio`.
- Test files use `test_*.py` naming in `tests/`.
- For edits affecting chat flow, add coverage around idempotency and message parsing.

## Commit & Pull Request Guidelines
- Use short, imperative commit subjects (e.g., “interaction architecture”).
- PRs should include: intent summary, tests run, and any config or prompt changes.
- If user-facing copy or chat responses change, include before/after snippets.

## Configuration & Secrets
- Use `.env` for local settings. Common keys: `TELEGRAM_BOT_TOKEN`, optional Stripe keys, Vercel/Gemini CLI tokens.
- Never commit secrets. Keep production values out of the repo.
