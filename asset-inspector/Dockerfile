# syntax=docker/dockerfile:1.18
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.14.9-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae AS builder
COPY --from=uv /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
# uv must discover every workspace member, even when installing only one.
COPY backend/README.md backend/pyproject.toml backend/
COPY execution-gateway/README.md execution-gateway/pyproject.toml execution-gateway/
COPY node-controller/README.md node-controller/pyproject.toml node-controller/
COPY packages/contracts/README.md packages/contracts/pyproject.toml packages/contracts/
COPY packages/media/README.md packages/media/pyproject.toml packages/media/source-inventory.json packages/media/
COPY asset-inspector/README.md asset-inspector/pyproject.toml asset-inspector/
COPY packages/media/src packages/media/src
COPY asset-inspector/src asset-inspector/src

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --package speechnexus-asset-inspector --no-dev --no-editable

# Fail the build when the decoder or the service surface cannot be imported.
RUN /app/.venv/bin/python - <<'PY'
import av
import numpy
import speechnexus_asset_inspector.app
import speechnexus_media.inspection

print("av", av.__version__, "numpy", numpy.__version__)
PY

FROM python:3.14.9-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae AS runtime

# prlimit is the only util-linux tool kept: the process never creates a
# namespace, mounts a filesystem, enters another namespace or changes privilege.
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/bin/unshare /usr/bin/mount /usr/bin/umount /usr/bin/nsenter /usr/bin/setpriv \
    && groupadd --gid 10001 speechnexus \
    && useradd --uid 10001 --gid speechnexus --no-create-home --shell /usr/sbin/nologin speechnexus \
    && install -d -m 0700 -o 10001 -g 10001 /run/inspection

WORKDIR /app
# The application files stay root-owned; the only writable path is tmpfs.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPEECHNEXUS_INSPECTION_HOST=0.0.0.0 \
    SPEECHNEXUS_INSPECTION_PORT=8080

EXPOSE 8080
HEALTHCHECK NONE
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/speechnexus-asset-inspector"]
