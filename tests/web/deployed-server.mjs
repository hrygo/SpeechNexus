#!/usr/bin/env node
/**
 * Serve one built `dist` tree with the headers `deploy/web/nginx.conf` sends.
 *
 * `vite preview` (the existing browser gates) serves the same `dist` bytes with no
 * `Content-Security-Policy`, so a page that needs `'unsafe-eval'` renders there and
 * blanks in the image (#840). This server is deliberately not a general nginx
 * emulator: it reproduces the parts of the deployment surface the gate depends on —
 * the security headers, the SPA fallback, immutable hashed assets and the proxied
 * API paths failing like an unreachable upstream — so the gate can assert first
 * paint under the real policy without Docker.
 *
 * With a third argument the origin also answers the same-version identity route
 * (`GET /v1/nexus/version`) with that contract digest, which is how the gate proves
 * the version alert is actually wired into the entry instead of trusting that a
 * missing component leaves no trace (#901 P2-5).
 *
 * Usage: node deployed-server.mjs <dist-directory> <port> [identity-contract-digest]
 */
import { createServer } from "node:http";
import { readFileSync, statSync } from "node:fs";
import { extname, join, normalize, resolve } from "node:path";

import { readDeploymentHeadersFromRepository } from "./deployed-headers.mjs";

const [distArgument, portArgument, identityDigest] = process.argv.slice(2);
if (!distArgument || !portArgument) {
  throw new Error("Usage: deployed-server.mjs <dist-directory> <port> [identity-digest]");
}
if (identityDigest !== undefined && !/^sha256:[0-9a-f]{64}$/u.test(identityDigest)) {
  throw new Error(`Invalid identity digest: ${identityDigest}`);
}
const distRoot = resolve(distArgument);
const port = Number(portArgument);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error(`Invalid port: ${portArgument}`);
}

const repositoryRoot = resolve(import.meta.dirname, "../..");
const { headers } = readDeploymentHeadersFromRepository(repositoryRoot);

const contentTypes = new Map([
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".mjs", "text/javascript; charset=utf-8"],
  [".css", "text/css; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".svg", "image/svg+xml"],
  [".png", "image/png"],
  [".ico", "image/x-icon"],
  [".woff2", "font/woff2"],
  [".map", "application/json; charset=utf-8"],
]);

/** The deployment proxies these prefixes to the backend; without one, refuse. */
const PROXIED_PREFIXES = ["/v1/", "/v2/"];

const identityBody = () =>
  JSON.stringify({ version: "0.1.0.dev0", contract_digest: identityDigest });

function send(response, status, body, contentType) {
  response.writeHead(status, {
    ...headers,
    "content-type": contentType,
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

function sendApiUnavailable(response) {
  // A refusal the shipped clients can parse, so the page reaches its own error
  // state instead of failing on a non-JSON 404 from a file server.
  send(
    response,
    503,
    JSON.stringify({
      error: {
        code: "service_unavailable",
        type: "service_unavailable_error",
        message: "Deployed-headers gate: no backend is attached to this origin",
        param: null,
        request_id: "req-deployed-headers-gate",
        retryable: true,
      },
    }),
    contentTypes.get(".json"),
  );
}

function readFileInside(root, candidate) {
  const normalized = normalize(candidate).replace(/^(\.\.[/\\])+/, "");
  const absolute = join(root, normalized);
  if (!absolute.startsWith(root)) return null;
  try {
    if (!statSync(absolute).isFile()) return null;
    return readFileSync(absolute);
  } catch {
    return null;
  }
}

const server = createServer((request, response) => {
  const pathname = new URL(request.url ?? "/", `http://127.0.0.1:${port}`).pathname;
  if (identityDigest !== undefined && pathname === "/v1/nexus/version") {
    // A second revision of the same deployment: the API answers, with another
    // contract digest than the one baked into these bundles.
    response.writeHead(200, {
      ...headers,
      "cache-control": "no-store",
      "content-type": contentTypes.get(".json"),
      "content-length": Buffer.byteLength(identityBody()),
    });
    response.end(identityBody());
    return;
  }
  if (PROXIED_PREFIXES.some((prefix) => pathname.startsWith(prefix))) {
    sendApiUnavailable(response);
    return;
  }

  // `location /` answers with `try_files $uri /index.html`: an unknown client route
  // still renders the SPA, while `/assets/` (`try_files $uri =404`) must not.
  const direct = pathname === "/" ? "/index.html" : pathname;
  const body = readFileInside(distRoot, direct);
  if (body !== null) {
    send(response, 200, body, contentTypes.get(extname(direct)) ?? "application/octet-stream");
    return;
  }
  if (pathname.startsWith("/assets/") || extname(pathname) !== "") {
    send(response, 404, "not found\n", "text/plain; charset=utf-8");
    return;
  }
  const index = readFileInside(distRoot, "/index.html");
  if (index === null) {
    send(response, 500, "dist/index.html missing\n", "text/plain; charset=utf-8");
    return;
  }
  send(response, 200, index, contentTypes.get(".html"));
});

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`deployed-headers gate serving ${distRoot} on http://127.0.0.1:${port}\n`);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
  });
}
