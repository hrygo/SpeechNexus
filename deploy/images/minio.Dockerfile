# syntax=docker/dockerfile:1.18
# The pinned community Docker Hub tag is unavailable. Use official, verified
# release packages; keep runtime data and this session's CA outside the image.
# Both release download paths serve the identical immutable bytes and they fail
# independently. The CDN path answered HTTP 504 for about an hour on 2026-11-08.
# The REST asset endpoint answers HTTP 403 "rate limit exceeded" on CI runners,
# because unauthenticated api.github.com allows 60 requests per hour per egress
# IP and runners share addressing. The CDN is tried first because it has no
# quota; the asset endpoint is the fallback, and an optional github_token secret
# raises its quota. Whichever path succeeds is verified against the same pinned
# digest below.
# Asset ids are per release: 404655648 is minio_20250907161309.0.0_amd64.deb
# and 404655618 is minio_20250907161309.0.0_arm64.deb of
# RELEASE.2025-09-07T16-13-09Z. Moving to another tag means re-resolving the
# asset ids and the digests together.
FROM python:3.15.0-slim-bookworm AS download
ARG TARGETARCH
RUN --mount=type=secret,id=proxy_ca \
    --mount=type=secret,id=github_token,required=false <<'SH'
set -eu
if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi
python - "$TARGETARCH" <<'PY'
import hashlib
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RELEASE = "RELEASE.2025-09-07T16-13-09Z"
PACKAGE = "minio_20250907161309.0.0_{arch}.deb"
# Bounded retries per channel: two waits from the 1s/2s/4s schedule, because a
# third attempt has no next attempt to back off from. 4xx answers are never
# retried; they mean the channel cannot serve this asset and the build moves on.
ATTEMPTS_PER_CHANNEL = 3
BACKOFF_SECONDS = (1, 2, 4)
REQUEST_TIMEOUT = 30
# Hard wall-clock ceiling for all download attempts, so a stalled endpoint
# cannot hold the build open for attempts x timeout.
ATTEMPT_BUDGET_SECONDS = 60
MAX_BYTES = 50 * 1024 * 1024

assets = {
    "amd64": {
        "id": 404655648,
        "sha256": "eeda08f699f6592d1b868ac8bda864ae2cacdb5ee1b888663366e8c8ff566249",
    },
    "arm64": {
        "id": 404655618,
        "sha256": "f9466ed832ac4926c8870b3b8378d77861a87b41529fecae7789b9bc54acd78b",
    },
}
arch = sys.argv[1]
if arch not in assets:
    raise SystemExit("MinIO package architecture must be amd64 or arm64")
asset = assets[arch]

token_path = pathlib.Path("/run/secrets/github_token")
token = token_path.read_text().strip() if token_path.is_file() else ""


class StripCrossOriginAuth(urllib.request.HTTPRedirectHandler):
    """The asset endpoint answers 302 to a signed blob URL on another host.

    That URL carries its own signature, so forwarding the bearer token across
    origins only leaks it. The token stays on the API host it was meant for.
    """

    def redirect_request(self, request, fp, code, msg, headers, new_url):
        redirected = super().redirect_request(
            request, fp, code, msg, headers, new_url
        )
        if redirected is None:
            return None
        if urllib.parse.urlsplit(new_url).netloc != urllib.parse.urlsplit(
            request.full_url
        ).netloc:
            redirected.remove_header("Authorization")
        return redirected


opener = urllib.request.build_opener(StripCrossOriginAuth())

cdn_url = f"https://github.com/minio/minio/releases/download/{RELEASE}/" + PACKAGE.format(
    arch=arch
)
api_url = f"https://api.github.com/repos/minio/minio/releases/assets/{asset['id']}"
api_headers = {"Accept": "application/octet-stream"}
if token:
    api_headers["Authorization"] = f"Bearer {token}"

# The high-quota path first; the rate-limited asset endpoint only as fallback.
channels = [
    ("CDN", cdn_url, {}),
    ("API", api_url, api_headers),
]
target = pathlib.Path("/tmp/minio.deb")


def fetch(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers)
    hasher = hashlib.sha256()
    size = 0
    with opener.open(request, timeout=timeout) as response, target.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                raise SystemExit(
                    "MinIO release package exceeds the reviewed size limit"
                )
            hasher.update(chunk)
            output.write(chunk)
    return size, hasher.hexdigest()


def retryable(exc):
    # 403/404 mean this channel cannot serve the asset; retrying cannot help.
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(
        exc,
        (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError),
    )


def describe(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"connection error: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


deadline = time.monotonic() + ATTEMPT_BUDGET_SECONDS
failures = []
downloaded = False
for label, url, headers in channels:
    last_error = "not attempted"
    if label == "API":
        auth = "github token from the optional secret" if token else "unauthenticated"
        print(f"[minio] API channel: {auth} request")
    for attempt in range(1, ATTEMPTS_PER_CHANNEL + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_error = f"attempt budget of {ATTEMPT_BUDGET_SECONDS}s exhausted"
            print(f"[minio] {label} channel: {last_error}", file=sys.stderr)
            break
        print(f"[minio] {label} channel: attempt {attempt}/{ATTEMPTS_PER_CHANNEL} {url}")
        try:
            size, digest = fetch(url, headers, max(0.5, min(REQUEST_TIMEOUT, remaining)))
        except Exception as exc:  # every transport error is classified below
            last_error = describe(exc)
            print(f"[minio] {label} channel: attempt {attempt} failed: {last_error}")
            if not retryable(exc):
                print(f"[minio] {label} channel: not retried, switching channel")
                break
            if attempt == ATTEMPTS_PER_CHANNEL:
                break
            delay = BACKOFF_SECONDS[attempt - 1]
            if time.monotonic() + delay >= deadline:
                print(f"[minio] {label} channel: no time left to back off {delay}s")
                break
            time.sleep(delay)
            continue
        print(f"[minio] {label} channel: downloaded {size} bytes")
        if digest != asset["sha256"]:
            raise SystemExit("MinIO release package checksum mismatch")
        print(f"[minio] {label} channel: sha256 verified")
        downloaded = True
        break
    if downloaded:
        break
    failures.append((label, last_error))

if not downloaded:
    detail = "; ".join(f"{label}: {error}" for label, error in failures)
    raise SystemExit(
        "MinIO package download failed: both download channels failed "
        f"after {ATTEMPTS_PER_CHANNEL} attempts each (last errors -> {detail})"
    )

subprocess.run(["dpkg-deb", "--extract", str(target), "/opt/minio-release"], check=True)
PY
SH
RUN --mount=type=secret,id=proxy_ca <<'SH'
set -eu
if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi
python - <<'PY'
import hashlib
import pathlib
import urllib.request
url = "https://raw.githubusercontent.com/minio/minio/RELEASE.2025-09-07T16-13-09Z/LICENSE"
with urllib.request.urlopen(url, timeout=30) as response:
    data = response.read(65537)
expected = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
if len(data) > 65536 or hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit("MinIO license checksum mismatch")
pathlib.Path("/opt/MINIO-LICENSE").write_bytes(data)
PY
SH

FROM python:3.15.0-slim-bookworm AS runtime
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
    org.opencontainers.image.version="RELEASE.2025-09-07T16-13-09Z" \
    org.opencontainers.image.licenses="AGPL-3.0-only"
COPY --from=download /opt/minio-release/usr/local/bin/minio /usr/local/bin/minio
COPY --from=download /opt/MINIO-LICENSE /usr/share/doc/minio/LICENSE
RUN groupadd --gid 10001 minio \
    && useradd --uid 10001 --gid minio --no-create-home --shell /usr/sbin/nologin minio \
    && mkdir -m 0700 /data && chown minio:minio /data
ENV MINIO_UPDATE=off MINIO_BROWSER=off
USER 10001:10001
EXPOSE 9000
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data", "--console-address", ":9001"]
