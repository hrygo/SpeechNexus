# Production asset publication unit

A02's first storage/application unit provides real UTF-8 `text_input`
publication on PostgreSQL and versioned MinIO. `AssetService` exposes upload,
original-identity reconciliation, metadata, bounded content/Range reads and
bounded retention maintenance. These are process-local ports; public HTTP routes,
a scheduled maintenance process and the complete A02 resource families remain
subsequent units. Operational endpoints retain their existing behavior.

## Publication and recovery

The application privately spools at most 4 MiB, hashes actual bytes and checks
UTF-8 content with at most one million characters. Empty, invalid or mismatched
content creates a durable `rejected` fact and no object. ASR/voice inputs require
a real `AssetInspector`; the default inspector reports unavailable and creates
neither fake audio facts nor a validation task without a consumer.

An optional idempotency key is scoped by the authoritative deployment/schema,
asset operation and workspace. Its stored SHA-256 identifies the key without
persisting the raw value. The request fingerprint fixes purpose, actual byte
count/hash, optional expected hash and **requested** TTL, including omission.
A same-key replay resolves the original before consulting changed defaults;
a different fingerprint conflicts without a new object. Without a key a new
upload is a new logical request; no cross-request exactly-once guarantee exists.

Short transactions reserve an Asset/command/event, then persist a single writer
identity before any PUT. Object versioning must already be enabled. One original
PUT, verification of storage metadata, and read-back SHA/length verification
happen outside metadata transactions. Publication atomically binds the verified
immutable storage version and changes the Asset state. Runtime credentials do
not create buckets or enable versioning. The 4 MiB ceiling keeps this unit below
S3's multipart threshold; multipart part/abort accounting is not implemented.

A lost object or DB reply does not authorize a second PUT. A `reserved` row
proves no external write was authorized; `writing` can only recover the original
writer's positive verified object. Missing content cannot prove that an
uncertain writer has stopped. Its state and retention ledger stay unresolved
until positive recovery or a later explicit reconciliation protocol can provide
that evidence. This is safety coverage, not complete crash-liveness qualification.

## Retention and references

Upload TTL is 60–31,536,000 seconds. Deployment default retention is an explicit
`AssetPolicy.default_ttl_seconds`; `None` requires a request TTL. No production
default is selected by tests. Effective expiry is fixed with the original record
using the database clock. Content/metadata reads reject expired content even
before a maintenance poll changes its stored state.

`maintain(limit)` reconciles at most `limit` originals and handles at most
`limit` expired assets, with a limit of 1–100. It closes the Asset, persists a
hash/version-bound retention deletion ledger, and blocks live references or an
unresolved writer. Deletion occurs outside the transaction, addresses only the
pinned object version, and is recorded complete only after that version is
confirmed absent. Unknown deletion replies retain the queued ledger for recovery.
Independent persisted poll hints rotate both bounded scans, so permanently blocked originals do not starve later recovery or deletion. They confer no publication or execution authority. Unrelated later storage versions are never swept. History and coordination
proofs remain in the database.

The reference table is present from the first resource. No Job/workbench
reference writer is delivered here. Such consumers must implement their narrow
transaction port and serialize new references with the same workspace → Asset
lock prelude, reject expired/closed assets and retain references until their own
release evidence is satisfied. The asset runtime role cannot insert references.
User-requested deletion still requires A07's preflight/confirmation protocol;
this unit adds no public delete shortcut. Maintenance must run before exposing
content after recovery, as part of the subsequent deployment/API integration.

## Storage, grants and local evidence

The packaged Alembic head is `0003_assets`, after `0002_coordination`. It creates
the shared `workspace-default`, Asset/command/object/reference/event tables and
content deletion ledger in the same authority schema. Existing coordination
rows and grants are preserved. Destructive downgrade is refused. Use the
existing migration CLI with a separate migration credential; it now upgrades
through this head. `asset_runtime_grants(schema, role)` generates minimum DML for
a fresh non-owner role. It grants no persistent DDL, DELETE, object/history/key
rewrites, workspace configuration updates or reference creation. Database-level
PUBLIC TEMP privileges need a separate deployment audit, as for owner roles.

The MinIO image is built from the official
[fixed release](https://github.com/minio/minio/releases/tag/RELEASE.2025-09-07T16-13-09Z),
using architecture-specific DEB SHA-256 values and a checked license file. The
community Docker Hub tag returned unavailable during implementation. The
Dockerfile supports amd64/arm64 packages; the executable image is validated on
this environment's amd64 host. The optional BuildKit CA secret is scoped to
network RUN steps and is not copied into the runtime image. Production object
IAM and deployment-network outbound restriction remain A07 qualification work;
synthetic test root keys do not qualify minimum production object privileges.

```bash
# Local synthetic service only; configure an explicit Docker endpoint as needed.
./scripts/dev-env/minio-test-service.sh
export SPEECHNEXUS_TEST_MINIO_ENDPOINT=http://127.0.0.1:59000
export SPEECHNEXUS_TEST_MINIO_ACCESS_KEY=speechnexus_test
export SPEECHNEXUS_TEST_MINIO_SECRET_KEY=synthetic_asset_test_secret
# Also configure both isolated database URLs described in tests/integration/README.md.
make integration-test
```

The helper builds checked release bytes, uses an owned, loopback-only container
and bounded private tmpfs for newly created services, and refuses to replace an
unowned or mismatched build. Tests create unique versioned buckets, use synthetic
bytes and remove only their own versions/buckets. PG uses packaged migrations
and an actual fresh non-owner runtime role. Tests cover actual contention,
object confirmation loss, real PG session termination, simulated lost replies
after real commits, durable rejection, process-service reconstruction, Range,
blocked references, uncertain cleanup and pinned-version preservation. The
InnoDB gate remains mandatory for coordination; this product asset path is PG
only. CI starts MinIO in the existing integration job and retains the protected
aggregate and 80% unit/contract coverage requirement.
