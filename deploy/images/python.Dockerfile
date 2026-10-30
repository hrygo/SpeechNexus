# syntax=docker/dockerfile:1.18
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.14.9-slim-bookworm AS builder
COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY backend/README.md backend/pyproject.toml backend/
COPY execution-gateway/README.md execution-gateway/pyproject.toml execution-gateway/
COPY node-controller/README.md node-controller/pyproject.toml node-controller/
COPY packages/contracts/pyproject.toml packages/contracts/
COPY packages/media/README.md packages/media/pyproject.toml packages/media/source-inventory.json packages/media/
COPY backend/src backend/src
COPY execution-gateway/src execution-gateway/src
COPY node-controller/src node-controller/src
COPY packages/contracts/src packages/contracts/src
COPY packages/media/src packages/media/src
COPY deploy/engines/cpu-vad/requirements.txt /tmp/cpu-vad-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --all-packages --all-extras --no-dev --no-editable \
    && uv pip install --python /app/.venv/bin/python --require-hashes \
       -r /tmp/cpu-vad-requirements.txt

FROM python:3.14.9-slim-bookworm AS runtime

RUN groupadd --gid 10001 speechnexus \
    && useradd --uid 10001 --gid speechnexus --no-create-home --shell /usr/sbin/nologin speechnexus

WORKDIR /app
COPY --from=builder --chown=speechnexus:speechnexus /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
