FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git docker.io ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG DOCKER_CLI_VERSION=26.1.4
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
    && chmod +x /usr/local/bin/docker

RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:/root/.cargo/bin:${PATH}"

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT="/app/.venv"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
ENV PATH="/app/.venv/bin:${PATH}"

COPY . .
ENV PYTHONPATH="/app/src"

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "demi.app:app", "--host", "0.0.0.0", "--port", "8000"]
