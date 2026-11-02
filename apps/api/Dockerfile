# Build from the REPO ROOT: docker build -f apps/api/Dockerfile .
# The context is the root because the image installs the uv workspace. Base images are pinned by
# their multi-arch index digests; the stack installs from wheels alone (ADR-0002).
#
# ADR-0024: PIPELINE_REF (a full 40-character mkos-pipeline commit sha) swaps the in-repo stub
# for the AI team's package. Empty (the default, CI and local) builds exactly the stub image and
# never runs the pipeline stages. Set, it needs BuildKit and the read-only deploy key as the
# secret `mkos_pipeline_key` (scripts/deploy/gcp/cloudbuild-pipeline.yaml); the key is mounted
# for one RUN and never lands in a layer.
ARG PIPELINE_REF=""

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

# ---- ADR-0024: the real pipeline, only with PIPELINE_REF (BuildKit skips these stages otherwise) ----
# The source at exactly PIPELINE_REF over SSH, the host key pinned to GitHub's published ed25519
# key (`gh api meta`, SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU). git and ssh live in this
# throwaway stage only; just the checked-out tree (no .git) leaves it.
FROM base AS pipeline-src
ARG PIPELINE_REF
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && printf '%s\n' 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' \
    >/etc/ssh/ssh_known_hosts
RUN --mount=type=secret,id=mkos_pipeline_key,required=true \
    printf '%s\n' "$PIPELINE_REF" | grep -Eqx '[0-9a-f]{40}' \
 && git init -q /pipeline \
 && GIT_SSH_COMMAND='ssh -i /run/secrets/mkos_pipeline_key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ssh/ssh_known_hosts -o HostKeyAlgorithms=ssh-ed25519' \
    git -C /pipeline fetch -q --depth=1 git@github.com:maniacultasset/mkos-pipeline.git "$PIPELINE_REF" \
 && git -C /pipeline checkout -q --detach FETCH_HEAD \
 && test "$(git -C /pipeline rev-parse HEAD)" = "$PIPELINE_REF" \
 && rm -rf /pipeline/.git

# The stub out, mkos-pipeline in with --no-deps: it keeps the workspace mkos-schemas and the
# locked httpx and pydantic (its only dependencies). hatchling builds it under the hash-pinned
# build-constraints.txt. The build fails unless mkos_pipeline imports from mkos-pipeline alone.
FROM builder AS builder-pipeline
RUN --mount=type=bind,from=pipeline-src,source=/pipeline,target=/mnt/mkos-pipeline,rw \
    --mount=type=bind,source=build-constraints.txt,target=/mnt/build-constraints.txt \
    uv pip uninstall mkos-pipeline-stub \
 && uv pip install --no-deps --no-cache --build-constraints /mnt/build-constraints.txt /mnt/mkos-pipeline \
 && rm -rf packages/pipeline-stub \
 && python -c "import importlib.metadata as m, sys; from mkos_pipeline import run_listing_pipeline; names = {d.metadata['Name'] for d in m.distributions()}; sys.exit(0 if 'mkos-pipeline' in names and 'mkos-pipeline-stub' not in names else 'mkos-pipeline check failed')"

# builder (PIPELINE_REF empty) or builder-pipeline (set).
FROM builder${PIPELINE_REF:+-pipeline} AS venv

FROM base AS runtime
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
# The workspace members are wheels in /opt/venv. /src keeps the builder's paths for the files
# outside them (scripts/db), and `import app` resolves to /src/apps/api (PYTHONPATH, WORKDIR).
COPY --from=venv /opt/venv /opt/venv
COPY --from=venv /src /src
COPY --chmod=0755 apps/api/docker-entrypoint.sh /usr/local/bin/entrypoint
WORKDIR /src/apps/api
# One uvicorn process per instance; Cloud Run scales by adding instances (ADR-0005).
ENV APP_ROLE=api PORT=8080 PYTHONPATH=/src/apps/api
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 CMD \
  python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz').read()"
ENTRYPOINT ["entrypoint"]
