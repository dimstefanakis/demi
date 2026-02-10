FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git nodejs unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:/root/.cargo/bin:${PATH}"

RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT="/app/.venv"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
ENV PATH="/app/.venv/bin:${PATH}"

RUN bun add -g @google/gemini-cli@latest
RUN bun add -g vercel@latest
RUN bun add -g firecrawl-cli@latest
RUN bun add -g supabase@latest

RUN printf '#!/bin/sh\nexec /root/.bun/bin/gemini "$@"\n' > /usr/local/bin/gemini \
    && chmod +x /usr/local/bin/gemini \
    && printf '#!/bin/sh\nset -e\n\n# Vercel CLI does not read VERCEL_TOKEN/VERCEL_SCOPE from env automatically.\n# Inject them when present and not already provided.\nif [ -n "${VERCEL_TOKEN:-}" ]; then\n  has_token=0\n  has_scope=0\n  for arg in "$@"; do\n    if [ "$arg" = "--token" ]; then\n      has_token=1\n    fi\n    if [ "$arg" = "--scope" ]; then\n      has_scope=1\n    fi\n  done\n  if [ "$has_token" -eq 0 ]; then\n    set -- "$@" --token "$VERCEL_TOKEN"\n  fi\n  if [ -n "${VERCEL_SCOPE:-}" ] && [ "$has_scope" -eq 0 ]; then\n    set -- "$@" --scope "$VERCEL_SCOPE"\n  fi\nfi\n\nexec /root/.bun/bin/vercel "$@"\n' > /usr/local/bin/vercel \
    && chmod +x /usr/local/bin/vercel \
    && printf '#!/bin/sh\nset -e\nif [ -x /root/.bun/bin/firecrawl ]; then\n  exec /root/.bun/bin/firecrawl \"$@\"\nfi\nexec /root/.bun/install/global/node_modules/.bin/firecrawl \"$@\"\n' > /usr/local/bin/firecrawl \
    && chmod +x /usr/local/bin/firecrawl \
    && printf '#!/bin/sh\nexec /root/.bun/install/global/node_modules/.bin/supabase "$@"\n' > /usr/local/bin/supabase \
    && chmod +x /usr/local/bin/supabase

COPY . .
ENV PYTHONPATH="/app/src"

CMD ["python", "-m", "demi.runtime.agent_entrypoint"]
