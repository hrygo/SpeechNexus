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
