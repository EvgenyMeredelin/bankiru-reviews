# syntax=docker/dockerfile:1.7
#
# Single image, three services (api, parser, ui). The compose file selects
# the service via `command:`. Built with uv for fast, reproducible installs.

ARG PYTHON_VERSION=3.13

# ── builder ─────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

# Resolve & install deps first so this layer caches across code edits.
COPY pyproject.toml uv.lock* /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev 2>/dev/null \
    || uv sync --no-install-project --no-dev

# Now copy the package source and install the project itself.
COPY src /app/src
COPY assets /app/assets
COPY README.md /app/README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev 2>/dev/null \
    || uv sync --no-dev

# ── runtime ─────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app
USER app

# Default command is the API; parser service overrides via compose.
CMD ["python", "-m", "bankiru.api"]
