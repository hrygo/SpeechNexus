# syntax=docker/dockerfile:1.18
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.14.9-slim-bookworm AS builder
COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY backend/README.md backend/pyproject.toml backend/
COPY execution-gateway/README.md execution-gateway/pyproject.toml execution-gateway/
COPY backend/src backend/src
COPY execution-gateway/src execution-gateway/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-packages --no-dev --no-editable

FROM python:3.14.9-slim-bookworm AS runtime

RUN groupadd --gid 10001 speechnexus \
    && useradd --uid 10001 --gid speechnexus --no-create-home --shell /usr/sbin/nologin speechnexus

WORKDIR /app
COPY --from=builder --chown=speechnexus:speechnexus /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
