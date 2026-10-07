# Build from the REPO ROOT: docker build -f apps/api/Dockerfile .
# The context is the root because the image installs the uv workspace. Base images are pinned by
# their multi-arch index digests; the stack installs from wheels alone (ADR-0002).
FROM python:3.13-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"

FROM base AS builder
# uv installs into the project environment and ignores VIRTUAL_ENV, so point it at /opt/venv or
# the runtime stage copies an empty venv (CANON-ADDENDUM.md §15).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /usr/local/bin/uv
WORKDIR /src
# Manifests first: this layer caches until a dependency actually changes.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/schemas/pyproject.toml packages/schemas/
COPY packages/pipeline-stub/pyproject.toml packages/pipeline-stub/
COPY apps/api/pyproject.toml apps/api/
# --no-editable on every sync: an editable build makes hatchling fetch `editables` from PyPI,
# unpinned and unhashed; a wheel build needs only the pinned build constraints (uv.lock).
RUN uv venv "$VIRTUAL_ENV" \
 && uv sync --frozen --no-dev --no-editable --no-install-workspace --package mkos-api
COPY packages/schemas/ packages/schemas/
COPY packages/pipeline-stub/ packages/pipeline-stub/
COPY apps/api/ apps/api/
COPY scripts/db/ scripts/db/
RUN uv sync --frozen --no-dev --no-editable --package mkos-api

FROM base AS runtime
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
# The workspace members are wheels in /opt/venv. /src keeps the builder's paths for the files
# outside them (scripts/db), and `import app` resolves to /src/apps/api (PYTHONPATH, WORKDIR).
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /src /src
COPY --chmod=0755 apps/api/docker-entrypoint.sh /usr/local/bin/entrypoint
WORKDIR /src/apps/api
# One uvicorn process per instance; Cloud Run scales by adding instances (ADR-0005).
ENV APP_ROLE=api PORT=8080 PYTHONPATH=/src/apps/api
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 CMD \
  python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz').read()"
ENTRYPOINT ["entrypoint"]
