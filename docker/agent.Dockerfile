FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git unzip \
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

RUN printf '#!/bin/sh\nexec bunx --bun @google/gemini-cli "$@"\n' > /usr/local/bin/gemini \
    && chmod +x /usr/local/bin/gemini \
    && printf '#!/bin/sh\nexec bunx --bun vercel "$@"\n' > /usr/local/bin/vercel \
    && chmod +x /usr/local/bin/vercel

COPY . .
ENV PYTHONPATH="/app/src"

CMD ["python", "-m", "claudius.runtime.agent_entrypoint"]
