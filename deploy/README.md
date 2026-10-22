# Deployment bootstrap

This directory defines a local, single-node Compose topology for validating
the repository bootstrap. It is not a production capacity or release
qualification. Release images must be locked by digest before publication; the
bootstrap uses explicit version tags for readability.

## Local Compose

```bash
cp deploy/compose.env.example deploy/compose.env
docker compose \
  --env-file deploy/compose.env \
  -f deploy/compose.yaml \
  up --build
```

The Compose development topology binds all published ports to loopback:

- AdminUI: `http://127.0.0.1:8080`
- Workbench: `http://127.0.0.1:8081`
- Speech API: `http://127.0.0.1:8000`
- Execution gateway: `http://127.0.0.1:8010`
- PostgreSQL: `127.0.0.1:55432`
- MinIO: `127.0.0.1:59000`

The repository's systemd development services use the same PostgreSQL and
MinIO host ports. Stop one topology before starting the other.

## Configuration authority

Runtime configuration is validated by `pydantic-settings` in each Python
package. Environment variables are explicit, prefixed with `SPEECHNEXUS_` for
the speech backend or `SPEECHNEXUS_GATEWAY_` for the execution gateway, and
contain no hidden fallback service endpoints.

| Variable | Process | Meaning |
| --- | --- | --- |
| `SPEECHNEXUS_DATABASE_URL` | API | Authoritative PostgreSQL DSN |
| `SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT` | API | S3-compatible private object endpoint |
| `SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL` | API | Dependency probe URL |
| `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` | API | Deployment object bucket |
| `SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL` | Gateway | PostgreSQL control-plane DSN |

Production entry points, Host / Origin policy, shared service credentials,
backup, retention and capacity qualification remain deployment decisions. The
initial product has no login, user directory, RBAC, or tenant isolation.
