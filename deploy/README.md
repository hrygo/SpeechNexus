# Deployment bootstrap

This directory defines a local, single-node Compose topology for validating
the repository bootstrap. Following
[ADR 0004](../docs/decisions/0004-cluster-ready-architecture-and-single-node-mvp.md),
the architecture is cluster-ready while the MVP deployment runs as a
single-node homomorphic topology (`Node=1, Replica=1`). It is not a production
capacity or multi-node high-availability qualification. Release images must be
locked by digest before publication; the bootstrap uses explicit version tags
for readability.

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
contain no hidden fallback service endpoints. Every process declares its node,
role, and instance identity so leases, fencing tokens, and session owners remain
cluster-ready from day one.

| Variable | Process | Meaning |
| --- | --- | --- |
| `SPEECHNEXUS_PROCESS_ROLE` | Backend | Explicit process role (`api`, `realtime-gateway`, `scheduler`, `job-worker`, `workbench-api`, `workbench-export-worker`) |
| `SPEECHNEXUS_NODE_ID` | Backend | Physical / virtual host node identity for parent physical budget and controller affinity |
| `SPEECHNEXUS_INSTANCE_ID` | Backend | Unique process instance identity for leases, fencing, and session ownership |
| `SPEECHNEXUS_DATABASE_URL` | Backend | Authoritative PostgreSQL DSN |
| `SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT` | Backend | S3-compatible private object endpoint |
| `SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL` | Backend | Dependency probe URL |
| `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` | Backend | Deployment object bucket |
| `SPEECHNEXUS_GATEWAY_NODE_ID` | Gateway | Host node identity associated with the execution gateway |
| `SPEECHNEXUS_GATEWAY_GATEWAY_ID` | Gateway | Unique execution gateway identity for submit-intent fencing |
| `SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL` | Gateway | PostgreSQL control-plane DSN |

Production entry points, Host / Origin policy, shared service credentials,
backup, retention and capacity qualification remain deployment decisions. The
initial product has no login, user directory, RBAC, or tenant isolation.
