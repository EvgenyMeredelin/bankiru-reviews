# syntax=docker/dockerfile:1.7
#
# Multi-stage Dockerfile that produces a single image shared by all three
# services (api, parser, ui). The docker-compose.yml selects which service
# to run via the `command:` directive. Using one image avoids building and
# storing three nearly-identical images.
#
# Build tool: uv (https://github.com/astral-sh/uv) — a fast, deterministic
# Python package installer written in Rust. It replaces pip + pip-tools.

# Default Python version; can be overridden at build time with --build-arg.
ARG PYTHON_VERSION=3.13

# ── builder stage ───────────────────────────────────────────────────────────
# This stage installs all Python dependencies into a virtual environment.
# It is discarded after the build — only the resulting /app directory is
# copied into the runtime stage, keeping the final image small.
FROM python:${PYTHON_VERSION}-slim AS builder

# PYTHONDONTWRITEBYTECODE=1  — skip .pyc generation (uv compiles bytecode
#                               itself via UV_COMPILE_BYTECODE=1)
# PYTHONUNBUFFERED=1         — flush stdout/stderr immediately (important
#                               for Docker log visibility)
# UV_LINK_MODE=copy          — copy files instead of symlinking, so the
#                               venv is self-contained and portable
# UV_COMPILE_BYTECODE=1      — pre-compile .py → .pyc for faster startup
# UV_PYTHON_DOWNLOADS=never  — never auto-download a Python interpreter;
#                               use the one from the base image
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Copy the uv binary from the official uv image (pinned version for
# reproducibility). This avoids installing uv via curl/pip.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

# ── Layer 1: install dependencies only ──────────────────────────────────────
# Copy only the dependency manifests first. This layer is cached as long as
# pyproject.toml and uv.lock don't change, so code edits don't trigger a
# full dependency reinstall.
# The `--frozen` flag requires an exact lockfile match; the fallback
# (`|| uv sync ...`) handles the case where no lockfile exists yet.
COPY pyproject.toml uv.lock* /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev 2>/dev/null \
    || uv sync --no-install-project --no-dev

# ── Layer 2: install the project package ────────────────────────────────────
# Now copy the actual source code and install the project itself (editable
# install into the venv). This layer rebuilds on every code change, but
# the dependency layer above stays cached.
COPY src /app/src
COPY assets /app/assets
COPY README.md /app/README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev 2>/dev/null \
    || uv sync --no-dev

# ── runtime stage ───────────────────────────────────────────────────────────
# Minimal image: only the Python interpreter and the pre-built venv from
# the builder stage. No build tools, no uv, no pip.
FROM python:${PYTHON_VERSION}-slim AS runtime

# Same Python flags as the builder, plus PATH pointing to the venv so
# `python` resolves to the venv's interpreter without activation.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Run as a non-root user for security. The `app` user owns /app.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app
# Copy the entire /app tree (venv + source + assets) from the builder,
# owned by the non-root `app` user.
COPY --from=builder --chown=app:app /app /app
USER app

# Default command starts the FastAPI API service. The parser and UI
# services override this via docker-compose.yml `command:` directives.
CMD ["python", "-m", "bankiru.api"]
