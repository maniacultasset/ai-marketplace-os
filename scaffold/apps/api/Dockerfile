# syntax=docker/dockerfile:1.7
# Build from the REPO ROOT: docker build -f apps/api/Dockerfile .
# The context is the root because the image installs the uv workspace.
ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# The `ai` group (langgraph, openai, tiktoken) ships in BOTH roles: the worker
# imports packages/pipeline, and a slimmer api image means two digests to chase.
ARG UV_GROUPS="--group ai"
# Manifests first: this layer caches until a dependency actually changes.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/schemas/pyproject.toml packages/schemas/
COPY packages/pipeline/pyproject.toml packages/pipeline/
COPY apps/api/pyproject.toml apps/api/
RUN uv venv "$VIRTUAL_ENV" \
 && uv sync --frozen --no-dev --no-install-workspace ${UV_GROUPS}
COPY packages/ packages/
COPY apps/api/ apps/api/
COPY scripts/ scripts/
RUN uv sync --frozen --no-dev ${UV_GROUPS}

FROM base AS runtime
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
# Paths are preserved from the builder: uv workspace installs are path-linked,
# so relocating /src here would break `import app` and `import mkos_schemas`.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /src /src
COPY --chmod=0755 apps/api/docker-entrypoint.sh /usr/local/bin/entrypoint
WORKDIR /src/apps/api
ENV APP_ROLE=api PORT=8080 WEB_CONCURRENCY=2 PYTHONPATH=/src/apps/api
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 CMD \
  python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz').read()"
ENTRYPOINT ["entrypoint"]
