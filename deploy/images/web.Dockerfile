# syntax=docker/dockerfile:1.18
FROM node:26.12.1-bookworm-slim AS build

ARG APP_NAME=admin

RUN npm install --global --allow-scripts=pnpm pnpm@12.14.0

WORKDIR /workspace
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY web web
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
RUN pnpm --filter "@speechnexus/${APP_NAME}" build \
    && cp -R "web/${APP_NAME}/dist" /workspace/web-dist

FROM nginxinc/nginx-unprivileged:1.29-alpine

COPY deploy/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/web-dist /usr/share/nginx/html

EXPOSE 8080
