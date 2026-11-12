# `asset-inspector`

Isolated audio inspection service for the private contract in
[`contracts/internal/asset-inspection.openapi.yaml`](../contracts/internal/asset-inspection.openapi.yaml).
It decodes one untrusted WAV per request and returns only media counts and
digests. It has no database client, no object-storage client, no outbound
network path and no persistent state. It never publishes an Asset, a Job, a
lease or a public `ready` fact.

The design is [资产检查容器](../docs/components/asset-inspection-container.md);
deployment assembly (Compose profile, `internal` network, resource limits) is a
separate card. This member supplies the service and its image only.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | process liveness |
| `GET` | `/readyz` | **unfrozen operational endpoint** (not in the private contract): self-check state, contract/recipe versions, limits and `in_flight`; `503` until the startup self-check decodes its built-in 5 ms WAV. C3 does not consume it; capability probing is a separate decision. |
| `POST` | `/internal/v1/inspection/audio` | the frozen private contract |

The runtime image keeps `prlimit` and removes `unshare`, `mount`, `umount`,
`nsenter` and `setpriv`; no code path creates a namespace, mounts a filesystem
or changes privilege.

`POST` requires `Authorization: Bearer`, `Content-Type:
application/octet-stream`, an exact `Content-Length`, `X-Nexus-Deadline-Ms` and
`X-Nexus-Max-Duration-Ms`. Optional `X-Request-ID` is echoed on `200` only.
Every budget is clamped to the server value in this member's configuration.

A `Content-Type` mismatch is a protocol/configuration fault and returns
`400 bad_request`, not a content verdict. `415 unsupported_media` is reserved
for a payload whose media type the recipe rejects. While the startup self-check
is still running, requests are refused with the retryable `429 busy` (a
temporary capacity state), never the terminal `503 unavailable` that means the
service is misconfigured.

## Configuration

Only `SPEECHNEXUS_INSPECTION_*` variables are read; the bearer token is required
and never logged or written into a receipt.

| Key | Default | Bounds |
| --- | --- | --- |
| `TOKEN` | required | ASCII graphic characters, 1–4096 |
| `HOST` | `0.0.0.0` | printable, ≤255 |
| `PORT` | `8080` | 1–65535 |
| `CONCURRENCY` | `2` | 1–4; no queue, excess requests get `429 busy` |
| `MAX_ENCODED_BYTES` | `4194304` | 1–1 GiB |
| `MAX_DURATION_MS` | `120000` | 1–86400000 |
| `MAX_WALL_SECONDS` | `20` | 0.1–60 |
| `MEMORY_BYTES` | `268435456` | 64 MiB–1 GiB; `prlimit --as` per child |
| `CPU_SECONDS` | `5` | 1–30; `prlimit --cpu` per child |
| `OUTPUT_BYTES` | `4096` | 1–65536; child stdout cap |
| `MAX_RECEIPT_BYTES` | `4096` | 1–65536; `200` body cap |

The service refuses to start when `/run/inspection` is not a private directory
or `/usr/bin/prlimit` is missing. It binds its port first and then runs the
self-check in the background: `/readyz` returns `503 unavailable` until the real
decoder child produces the expected receipt, and the process exits non-zero if
the check fails so the deployment unit restarts it.

## Image and runtime baseline

```sh
docker build -f asset-inspector/Dockerfile -t speechnexus-asset-inspector .

docker run --rm --name asset-inspector \
  --read-only \
  --tmpfs /run/inspection:rw,noexec,nosuid,nodev,size=64m,mode=0700,uid=10001,gid=10001 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 64 \
  --memory 768m --cpus 2.0 \
  --init \
  -p 127.0.0.1:18080:8080 \
  -e SPEECHNEXUS_INSPECTION_TOKEN=synthetic-private-inspection-token \
  speechnexus-asset-inspector
```

The deployment form attaches the unit to a dedicated `internal: true` network
and publishes no host port. To prove the service needs no outbound interface,
run it with `--network none` (omit `-p`) and call it from inside the container:

```sh
docker exec <container> /app/.venv/bin/python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read())"
```

The container needs no volume, no bind mount and no `docker.sock`.

Per request the service creates one `TemporaryFile` in `/run/inspection`, starts
one `prlimit`-wrapped child (`--as`, `--cpu`, `--fsize`, `--nofile`, `--core`),
caps stdout, discards stderr, passes a minimal environment without the token,
and kills the child's whole session when the wall clock expires. The slot is
released only after the child has been reaped. If a killed child cannot be
reaped within one second the service exits so the container runtime destroys
the PID namespace.

## Image dependencies

The runtime dependency set is built from the committed lock, in three steps that
share one BuildKit cache mount on `/root/.cache/uv`:

1. `uv export --frozen --package speechnexus-asset-inspector --no-dev \
   --no-emit-workspace` writes the package's transitive runtime requirements at
   the versions and hashes recorded in `uv.lock`.
2. `uv pip sync --require-hashes` creates `/app/.venv` from that set, so every
   wheel is checked against the lock before it enters the image.
3. `uv pip install --no-deps` installs `speechnexus-media` and
   `speechnexus-asset-inspector` from source, which cannot disturb the verified
   set because nothing is resolved.

The cache mount never enters the image, and `UV_LINK_MODE=copy` keeps
`/app/.venv` self-contained.

Measured on one aarch64 host, `uv pip sync --require-hashes` over nine packages
including `av` (31.2 MiB) and `numpy` (15.0 MiB):

| | Empty cache mount | Warm cache mount |
| --- | --- | --- |
| Dependency step | `Prepared 9 packages in 4.96s` | `Installed 9 packages in 62ms`, no download |
| Whole build | 11.8s | 4.8s |

`--no-cache-filter` does not preserve cache mounts, so it measures a cold cache
rather than a rebuild; invalidate the builder with a real source change instead.

### Hash verification

`--require-hashes` is load-bearing, not decorative. Replacing `numpy`'s hash
block with a single bogus digest fails the build on an empty cache with
`Hash mismatch for numpy==2.5.4`; the same requirement set without that flag
would install whatever the index returned. Two further properties follow from
the layout: `uv export --frozen` fails if the lock and the working tree
disagree, and the export excludes workspace packages so no local source path is
ever resolved from an index.

### Index selection

`UV_DEFAULT_INDEX_URL` selects the package index for the dependency step and
defaults to empty, which keeps `pypi.org`. With
`https://pypi.tuna.tsinghua.edu.cn/simple` set on an empty cache, uv fetched
`av-19.0.2-cp312-abi3-manylinux_2_28_aarch64.whl` and
`numpy-2.5.4-cp314-cp314-manylinux_2_27_aarch64...whl` with a fresh GET from
`https://pypi.tuna.tsinghua.edu.cn`, and no request went to
`files.pythonhosted.org`; with the argument unset the same build fetched them
from `files.pythonhosted.org`. `uv.lock` is read, never written: its sha256 is
identical before and after either build.

Download time on this host is dominated by the network, not the mirror: the same
nine packages took 6.9s from `pypi.org` and 5.0s from the mirror when measured
minutes apart, while an earlier run against `pypi.org` took 2m 12s during a slow
window. The argument exists for hosts where the public index is slow or
unreachable, not because the mirror is inherently faster.

### Debian archive selection

`DEBIAN_MIRROR` rewrites only the main archive `URIs:` line of
`/etc/apt/sources.list.d/debian.sources`. The `bookworm-security` suite keeps
`http://deb.debian.org/debian-security`, the `Suites` and `Components` fields are
untouched, and every entry keeps
`Signed-By=/usr/share/keyrings/debian-archive-keyring.gpg`, so a mirror serving
unsigned or tampered metadata still fails the build.

```sh
# Local builds only; CI keeps the default sources.
docker build -f asset-inspector/Dockerfile \
  --build-arg UV_DEFAULT_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian \
  -t speechnexus-asset-inspector .
```

## Verification

```sh
uv run pytest tests/unit/inspector -q
uv run ruff check .
uv run ruff format --check .
uv run mypy --platform linux
uv run lint-imports
```

The service process does not import PyAV or NumPy; only the child module does.
Unit tests use synthetic WAV bytes and never read business audio.
