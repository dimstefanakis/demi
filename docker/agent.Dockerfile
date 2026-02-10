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
    && printf '#!/bin/sh\nexec /root/.bun/bin/vercel "$@"\n' > /usr/local/bin/vercel \
    && chmod +x /usr/local/bin/vercel \
    && printf '#!/bin/sh\nset -e\nif [ -x /root/.bun/bin/firecrawl ]; then\n  exec /root/.bun/bin/firecrawl \"$@\"\nfi\nexec /root/.bun/install/global/node_modules/.bin/firecrawl \"$@\"\n' > /usr/local/bin/firecrawl \
    && chmod +x /usr/local/bin/firecrawl \
    && printf '#!/bin/sh\nexec /root/.bun/install/global/node_modules/.bin/supabase "$@"\n' > /usr/local/bin/supabase \
    && chmod +x /usr/local/bin/supabase

COPY . .
ENV PYTHONPATH="/app/src"

CMD ["python", "-m", "demi.runtime.agent_entrypoint"]
