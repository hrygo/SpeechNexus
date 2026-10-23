# syntax=docker/dockerfile:1.18
FROM node:26.12.1-bookworm-slim AS build

ARG APP_NAME=admin

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm install --global --allow-scripts=pnpm pnpm@12.14.0

WORKDIR /workspace
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY web web
COPY packages/contracts packages/contracts
COPY scripts/dev-env/validate-design-tokens.mjs scripts/dev-env/validate-design-tokens.d.mts scripts/dev-env/
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    pnpm install --frozen-lockfile
RUN pnpm --filter "@speechnexus/${APP_NAME}" build \
    && cp -R "web/${APP_NAME}/dist" /workspace/web-dist

FROM nginxinc/nginx-unprivileged:1.31-alpine

COPY deploy/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/web-dist /usr/share/nginx/html

EXPOSE 8080
