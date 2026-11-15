# syntax=docker/dockerfile:1.18
FROM node:26.12.1-bookworm-slim AS build

ARG APP_NAME=admin

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm install --global --allow-scripts=pnpm pnpm@12.14.0

WORKDIR /workspace
# This COPY set is the complete input set of `pnpm --filter "@speechnexus/${APP_NAME}"
# build`: the locked install, one Web application's production TypeScript program
# (`web/<app>/tsconfig.build.json`) and the workspace packages and root config it
# reads. Test material under `tests/**` is deliberately absent, so product sources
# must not import it: the dev scenario pages are Vite-globbed behind
# `import.meta.env.DEV` and the test-only fixture helpers are outside the production
# program. `pnpm verify:web-image` builds both applications from this set alone and
# fails when a source or config outside it is required — CI cannot see that gap,
# because the Web job builds with the whole repository present (#834).
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json .npmrc ./
COPY web web
COPY packages/contracts packages/contracts
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    pnpm install --frozen-lockfile
RUN pnpm --filter "@speechnexus/${APP_NAME}" build \
    && cp -R "web/${APP_NAME}/dist" /workspace/web-dist

FROM nginxinc/nginx-unprivileged:1.31-alpine

ENV NGINX_ENVSUBST_FILTER="^WEB_(SPEECH_UPSTREAM|WORKBENCH_UPSTREAM|SPEECH_HOST|WORKBENCH_HOST|DNS_RESOLVER)$"
COPY deploy/web/nginx.conf /etc/nginx/templates/default.conf.template
COPY deploy/web/19-configure-upstreams.envsh /docker-entrypoint.d/19-configure-upstreams.envsh
COPY --from=build /workspace/web-dist /usr/share/nginx/html

EXPOSE 8080
