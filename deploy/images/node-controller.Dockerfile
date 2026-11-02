# syntax=docker/dockerfile:1.18
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.14.9-slim-bookworm AS builder
COPY --from=uv /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
# uv must discover every workspace member, even when installing only the controller.
COPY backend/README.md backend/pyproject.toml backend/
COPY execution-gateway/README.md execution-gateway/pyproject.toml execution-gateway/
COPY packages/contracts/pyproject.toml packages/contracts/
COPY packages/media/README.md packages/media/pyproject.toml packages/media/
COPY node-controller/README.md node-controller/pyproject.toml node-controller/
COPY node-controller/src node-controller/src

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=proxy_ca \
    set -eu; \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --package speechnexus-node-controller \
        --no-dev --no-default-groups --no-editable \
    && uv pip check --python /app/.venv/bin/python

# Check the installed distribution and factory imports without starting the service.
RUN /app/.venv/bin/python - <<'PY'
from importlib.metadata import distribution

import speechnexus_node_controller.app

entrypoint = next(
    entry for entry in distribution("speechnexus-node-controller").entry_points
    if entry.group == "console_scripts" and entry.name == "speechnexus-node-controller"
)
assert callable(entrypoint.load())
PY

FROM python:3.14.9-slim-bookworm AS runtime

RUN groupadd --gid 10001 speechnexus \
    && useradd --uid 10001 --gid speechnexus --no-create-home --shell /usr/sbin/nologin speechnexus \
    && install -d -m 0700 -o 10001 -g 10001 /var/lib/speechnexus/controller \
    && install -d -m 0755 /etc/speechnexus/controller /run/speechnexus-controller

WORKDIR /app
# Keep installed application files root-owned; only node state needs application writes.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPEECHNEXUS_CONTROLLER_ENVIRONMENT=production \
    SPEECHNEXUS_CONTROLLER_HOST=0.0.0.0 \
    SPEECHNEXUS_CONTROLLER_PORT=8020 \
    SPEECHNEXUS_CONTROLLER_APPROVED_TEMPLATES_FILE=/etc/speechnexus/controller/approved-templates.json \
    SPEECHNEXUS_CONTROLLER_INTENT_STORE_PATH=/var/lib/speechnexus/controller/intents.sqlite3 \
    SPEECHNEXUS_CONTROLLER_DOCKER_SOCKET_PATH=/run/speechnexus-controller/docker.sock

EXPOSE 8020
HEALTHCHECK NONE
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/speechnexus-node-controller"]
