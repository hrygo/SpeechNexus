# syntax=docker/dockerfile:1.18
# The two things this build reads from the network are arguments whose defaults are
# the values this file has always used, so an ordinary build is unchanged: no
# argument means the product base images and the registry npm/pnpm resolve by
# themselves. They exist because a host whose route to Docker Hub is throttled cannot
# build this file at all — on MegaCube (Linux aarch64, 2026-11-15) `docker pull
# node:26.12.1-bookworm-slim` sat at 13.27 KiB/s, which needs about 70 minutes for that
# one layer, and the 900 s Compose build window expired (#572/#829). Redirecting the
# arguments is documented in README.md ("Build window and registry route") with the
# measured criteria for deciding that a host needs it.
#
# Changing where bytes come from must not change how they are trusted: there is no
# `strict-ssl=false`, no `NODE_TLS_REJECT_UNAUTHORIZED` and no `--insecure` here, a
# deployment or session CA still arrives as the `proxy_ca` BuildKit secret, and no
# credential belongs in this file or in a layer that consumes it.

# Global scope so both `FROM` lines can read their argument.
ARG NODE_BASE_IMAGE=node:26.12.1-bookworm-slim
ARG NGINX_BASE_IMAGE=nginxinc/nginx-unprivileged:1.31-alpine

FROM ${NODE_BASE_IMAGE} AS build

ARG APP_NAME=admin
# Optional package-registry redirect (for example `https://registry.npmmirror.com`).
# Empty — the default — leaves npm and pnpm on their own registry, so only a host that
# needs the redirect pays for it.
ARG NPM_REGISTRY=

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    if [ -n "${NPM_REGISTRY}" ]; then npm config set registry "${NPM_REGISTRY}" --location=user; fi; \
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

FROM ${NGINX_BASE_IMAGE}

ENV NGINX_ENVSUBST_FILTER="^WEB_(SPEECH_UPSTREAM|WORKBENCH_UPSTREAM|SPEECH_HOST|WORKBENCH_HOST|DNS_RESOLVER)$"
COPY deploy/web/nginx.conf /etc/nginx/templates/default.conf.template
COPY deploy/web/19-configure-upstreams.envsh /docker-entrypoint.d/19-configure-upstreams.envsh
COPY --from=build /workspace/web-dist /usr/share/nginx/html

EXPOSE 8080
