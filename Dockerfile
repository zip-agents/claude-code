# syntax=docker/dockerfile:1

# Pinned versions; bump deliberately (Dependabot watches this file). Pin by digest
# (image:tag@sha256:...) for fully reproducible builds once a registry is available.
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.0

FROM ${UV_IMAGE} AS uv

# --- build: resolve the locked runtime dependencies into a virtualenv ----------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first so source changes do not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# --- runtime: interpreter + virtualenv only, non-root, read-only friendly ------------------
FROM ${PYTHON_IMAGE}
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Bytecode was compiled at build time; never write it at runtime (read-only root fs).
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health/live' % os.environ.get('PORT', '8000'), timeout=2)"]
STOPSIGNAL SIGTERM
CMD ["python", "-m", "zip_service"]
