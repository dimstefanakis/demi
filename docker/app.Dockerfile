FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git docker.io \
    && rm -rf /var/lib/apt/lists/*

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
CMD ["python", "-m", "uvicorn", "claudius.app:app", "--host", "0.0.0.0", "--port", "8000"]
