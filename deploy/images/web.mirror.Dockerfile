# syntax=docker/dockerfile:1.18
# BUILD-ONLY twin of `web.Dockerfile`. It is not the product image definition and is
# never referenced by `deploy/compose.yaml`; the product file stays authoritative.
#
# Why it exists: `web.Dockerfile` pins both base images and the package registry
# absolutely, and neither is reachable through an argument that file declares. A host
# whose route to Docker Hub is throttled therefore cannot build it at all — on
# MegaCube (Linux aarch64, 2026-11-15) `docker pull node:26.12.1-bookworm-slim` sat at
# 13.27 KiB/s, that single layer needed about 70 minutes at that rate, and the Compose
# `web` build expired its 900 s window (exit 124). This file repeats the same build
# with the two network inputs exposed as arguments whose defaults are the product
# values:
#
#   NODE_BASE_IMAGE   default `node:26.12.1-bookworm-slim`              (product value)
#   NGINX_BASE_IMAGE  default `nginxinc/nginx-unprivileged:1.31-alpine` (product value)
#   NPM_REGISTRY      default empty = whatever registry npm/pnpm use by themselves
#
# Rules this file holds:
#   * With no overrides it compiles the same instructions as `web.Dockerfile`, so the
#     default build path is unchanged. `tests/ci/test_web_mirror_build.py` compares the
#     two files and fails when they drift.
#   * It changes where bytes come from, never what those bytes are trusted to be: no
#     `strict-ssl=false`, no `NODE_TLS_REJECT_UNAUTHORIZED`, no `--insecure`. A
#     deployment or session CA still arrives as the `proxy_ca` BuildKit secret,
#     exactly as in the product build.
#   * No credential belongs in this file or in a layer. A mirror that requires
#     authentication is configured on the Docker daemon, not here.
#   * The produced image is only guaranteed byte-identical to the public-registry
#     build when the mirror is a transparent proxy (same manifest digest per base
#     image). `deploy/README.md` gives the check; the application bundle in
#     `web-dist` is identical either way, because it is built from the same lockfile.

# Global scope so both `FROM` lines can read their argument.
ARG NODE_BASE_IMAGE=node:26.12.1-bookworm-slim
ARG NGINX_BASE_IMAGE=nginxinc/nginx-unprivileged:1.31-alpine

FROM ${NODE_BASE_IMAGE} AS build

ARG APP_NAME=admin
ARG NPM_REGISTRY=

# Registry redirection is one layer of its own so the drift guard in
# `tests/ci/test_web_mirror_build.py` can drop exactly these lines and compare the
# rest of the file with `web.Dockerfile` instruction for instruction. An empty
# `NPM_REGISTRY` makes the layer a no-op.
RUN if [ -n "${NPM_REGISTRY}" ]; then \
        npm config set registry "${NPM_REGISTRY}" --location=user; \
    fi

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm install --global --allow-scripts=pnpm pnpm@12.14.0

WORKDIR /workspace
# Same complete input set as `web.Dockerfile`; see the comment there for what the set
# contains and why test material is absent.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json .npmrc ./
COPY web web
COPY packages/contracts packages/contracts
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    pnpm install --frozen-lockfile
RUN pnpm --filter "@speechnexus/${APP_NAME}" build \
    && cp -R "web/${APP_NAME}/dist" /workspace/web-dist

FROM ${NGINX_BASE_IMAGE}

ENV NGINX_ENVSUBST_FILTER="^WEB_(SPEECH_UPSTREAM|WORKBENCH_UPSTREAM|SPEECH_HOST|WORKBENCH_HOST|DNS_RESOLVER)$"
COPY deploy/web/nginx.conf /etc/nginx/templates/default.conf.template
COPY deploy/web/19-configure-upstreams.envsh /docker-entrypoint.d/19-configure-upstreams.envsh
COPY --from=build /workspace/web-dist /usr/share/nginx/html

EXPOSE 8080
