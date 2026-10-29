# syntax=docker/dockerfile:1.18
# The pinned community Docker Hub tag is unavailable. Use official, verified
# release packages; keep runtime data and this session's CA outside the image.
FROM python:3.15.0-slim-bookworm AS download
ARG TARGETARCH
RUN --mount=type=secret,id=proxy_ca <<'SH'
set -eu
if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi
python - "$TARGETARCH" <<'PY'
import hashlib
import pathlib
import subprocess
import sys
import urllib.request

release = "RELEASE.2025-09-07T16-13-09Z"
digests = {
    "amd64": "eeda08f699f6592d1b868ac8bda864ae2cacdb5ee1b888663366e8c8ff566249",
    "arm64": "f9466ed832ac4926c8870b3b8378d77861a87b41529fecae7789b9bc54acd78b",
}
arch = sys.argv[1]
if arch not in digests:
    raise SystemExit("MinIO package architecture must be amd64 or arm64")
name = f"minio_20250907161309.0.0_{arch}.deb"
url = f"https://github.com/minio/minio/releases/download/{release}/{name}"
target = pathlib.Path("/tmp/minio.deb")
hasher = hashlib.sha256()
size = 0
with urllib.request.urlopen(url, timeout=30) as response, target.open("wb") as output:
    while chunk := response.read(1024 * 1024):
        size += len(chunk)
        if size > 50 * 1024 * 1024:
            raise SystemExit("MinIO release package exceeds the reviewed size limit")
        hasher.update(chunk)
        output.write(chunk)
if hasher.hexdigest() != digests[arch]:
    raise SystemExit("MinIO release package checksum mismatch")
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
