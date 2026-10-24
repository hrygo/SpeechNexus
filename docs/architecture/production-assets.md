# Production asset publication unit

A02's first storage/application unit provides real UTF-8 `text_input`
publication on PostgreSQL and versioned MinIO. `AssetService` exposes upload,
original-identity reconciliation, metadata, bounded content/Range reads and
bounded retention maintenance. The backend now exposes `POST /v1/nexus/assets`, `GET /v1/nexus/assets/{asset_id}`
and its `/content` path when explicit storage credentials are configured. A
standalone maintenance entry point replays originals and retention ledgers.
Artifact/Operation/receipt and immutable resource families remain subsequent
A02 units. Operational endpoints retain their existing behavior.

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

Upload TTL is 60–31,536,000 seconds. The process default is **7 days (604800
seconds)**, selected by the product owner; deployments can override
`SPEECHNEXUS_ASSET_DEFAULT_TTL_SECONDS`. An explicit upload `ttl_seconds` takes
precedence. The low-level `AssetPolicy(None)` still requires a request TTL.
Effective expiry is fixed with the original record using the database clock;
changing process defaults never changes an existing Asset or its replay identity.
Content/metadata reads reject expired content even
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
this unit adds no public delete shortcut. The configured API performs a bounded maintenance pass before accepting traffic;
every read independently rejects persisted expiry. A separate worker continues
polling. Full backup restoration and user-confirmed deletion remain A07 work.

## Immutable storage namespace

`0004_asset_storage` adds a singleton immutable namespace declaration. The
fingerprint identifies the normalized configured S3 origin and bucket, excludes
credentials (which may rotate), and is checked by every metadata transaction.
The API/worker composition derives the metadata fingerprint from its exact
object adapter. A changed endpoint/bucket fails before any original object is
read or deleted; a different store's missing version cannot complete the original
ledger. Runtime roles may insert the initial declaration, but cannot update or
delete it. This is a persisted identity fact, not mutable topology configuration.

An empty authority declares its first namespace under the default-workspace
lock before any new Asset row is committed. Configured startup checks an existing
declaration before sending even S3 credentials; initial versioning/credential
checks happen before declaration. Existing assets without a declaration (for
example an old `0003_assets` authority) require explicit verified A07 adoption.
Startup refuses to infer their old bucket or silently replace their placement.
Do not rewrite the declaration to bypass recovery. Stop/fence all older asset
writers and cleanup processes before this protocol upgrade; old binaries do not
check the new declaration. A07 qualifies the full rolling-upgrade/restore path.

All processes must use the same canonical storage origin/bucket. This guard
qualifies configuration identity, not physical-provider attestation, endpoint
alias migration or multi-host storage/backup failover. Loopback endpoints are
for the same isolated host in these tests; cluster deployment needs a shared
approved canonical endpoint. Those deployment/recovery qualifications remain A07.

## HTTP and worker configuration

The API uses the explicitly configured `SPEECHNEXUS_DATABASE_URL` and
`SPEECHNEXUS_DATABASE_SCHEMA` (default `speech`). Apply migrations with the
separate migration credential and install `asset_runtime_grants` as an
administrator. API/worker startup rejects a database/schema/table owner or a
role able to create persistent objects in the authority schema. It does not
install migrations, grants, a bucket, versioning or an object policy. This
startup guard is not the complete deployment privilege audit.

Supply both `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY` and
`SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` for the configured private, versioned
bucket. With neither key, business endpoints return a stable 503 and operational
health remains available. A partial credential pair is a configuration error;
validation excludes input values from error messages. Use dedicated object
credentials; synthetic test root keys do not qualify production IAM.

`SPEECHNEXUS_ASSET_DEFAULT_TTL_SECONDS` is optional and defaults to **604800
seconds (7 days)**. Set a value in 60–31,536,000 seconds to override it; invalid
values fail configuration instead of silently falling back. Uploads may omit
`ttl_seconds`, or explicitly override the deployment default. Omission remains
absent in the request digest; replays use the original persisted expiry, even
after a configuration change. This setting does not rewrite existing content,
enable object credentials, or replace the retention worker. `asset_max_bytes` is 1–4 MiB
(default 4 MiB); ingress allows another 16 KiB for multipart framing. A process
accepts at most `asset_upload_concurrency` concurrent private upload buffers
(default/hard maximum 4), with no waiting queue. The complete receive deadline is
`asset_receive_timeout_seconds` (default 30 s, maximum 120 s). This is a transport
buffer bound, not a distributed execution capacity claim. File parsing accepts
one binary part and four bounded metadata fields; duplicate or unapproved fields
and client paths/keys/URLs are rejected. Private buffers close on errors and
receive disconnects. Filename and client Content-Type do not determine storage
identity or validated MIME facts.

Asset endpoints accept only configured `asset_allowed_hosts` (default
`localhost`, `127.0.0.1`, `::1`). Requests carrying Origin require an exact
`asset_allowed_origins` entry (default empty). These are ingress guards, not a
login, user identity, tenant boundary or CORS service. Browser clients use the
same-origin deployment proxy; the existing Vite `/v1` proxy supports this during
development. Configure its actual Host and frontend Origin explicitly. Deployment
network access and outgoing-content restrictions still require A07 evidence.

Responses validate against packaged authoritative schemas, not weaker inferred
response models. `/openapi.json` projects only these three implemented business
paths alongside health. Public metadata exposes no object key/version, writer
or request digest. Downloads use octet-stream attachments, no-store, nosniff and
one pinned byte range; malformed/multiple/unsatisfiable ranges return 416 with
`Content-Range: bytes */<actual-size>`. Prefetched storage readers close even if
response start/body sends fail. A same-key expired replay remains unavailable.

Unknown/pending responses provide Location when the original Asset identity is
known, `retryable: false` and `X-Should-Retry: false`. The latter suppresses the
OpenAI SDK's default transport retries. `asset_upload_in_progress` represents an
Asset; it never invents an Operation ID. Query the original metadata or retry
only its original body/key for reconciliation. Stable error envelopes omit
parser/SDK diagnostics, raw content and raw idempotency keys. Without a key,
a new request remains a new logical upload.

The `speechnexus-asset-maintain` process requires explicit `process_role=job-worker`,
node ID and instance ID, plus the same authority/bucket configuration. It defaults
to a 10-resource batch every 5 seconds (`asset_maintenance_batch_size` 1–100,
`asset_maintenance_interval_seconds` >0–300 s). `--once` runs one bounded pass;
a zero exit does not assert that blocked/unknown ledgers are complete. SIGINT or
SIGTERM unwinds the current owned transaction/reader and stops the process;
interrupted or forced operations retain original identities/ledgers for replay. This worker does not start a GPU process
or release compute budgets.

```bash
# A non-owner runtime credential, pre-created versioned bucket and paired
# object keys must already be configured. Do not use a migration/DB-owner DSN.
export SPEECHNEXUS_PROCESS_ROLE=job-worker
export SPEECHNEXUS_NODE_ID=node-local-1
export SPEECHNEXUS_INSTANCE_ID=asset-maintenance-1
uv run speechnexus-asset-maintain
```

`@speechnexus/contracts` exports `uploadAsset(createSpeechClient(baseUrl), input)`:
its Blob/File transport replaces only the generated binary placeholder, validates
metadata/replies and supplies actual FormData without adding mutable defaults or
retrying unknown replies. Use an explicit Idempotency-Key and request TTL, e.g.
`{ purpose: "text_input", file, ttl_seconds: 3600 }`. The static generator uses
`--default-non-nullable false`: a schema's default annotation must not make an
optional request field mandatory in TypeScript or insert a value at runtime.

## Storage, grants and local evidence

The packaged Alembic head is `0007_admission_commands`. Its admission command
storage and the runtime/outbox/private readiness extensions preserve the frozen
`0006_runtime_observation`, `0005_runtime_intents`,
`0004_asset_storage`, `0003_assets` and
`0002_coordination` migrations. `0003_assets` creates
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


## Shared workspace reads

The backend implements `GET /v1/nexus/workspaces` and
`GET /v1/nexus/workspaces/{workspace_id}` using the existing `0003_assets`
`speech_workspaces` authority. The default group is the actual migration row:
missing records are never synthesized. Both active and disabled groups remain
readable; existing asset upload rules continue to reject disabled groups.
Workspace grouping does not establish a user, ACL, tenant or private boundary.

The pure application read port exposes get and bounded keyset scan. Its PG
adapter uses the existing runtime pool, original SELECT grants, explicit columns
and read-only transactions, with bounded pool waits and SQL deadlines. It adds
no migrations, grants or writes. Invalid stored values fail closed with a fixed
503 error. Numeric metadata revisions stay within exact JSON integer range;
policy revisions retain the complete stored BIGINT as a decimal string. UTC
creation times preserve microseconds. No policy object or budget is invented.

List limits come from the generated source contract: default 50, range 1–100.
The adapter reads at most limit + 1 ordered keys. The opaque, canonical cursor
identifies the workspace-list version and original last key; invalid, cross
resource or noncanonical cursors return 400. Cursors are shared-group navigation,
not authorization tokens. Each page uses current committed records; concurrent
inserts behind the cursor do not rewind the list, and this is not a snapshot
pagination guarantee. Unknown/repeated query parameters, invalid paths, repeated
request IDs and GET bodies are rejected before database access.

The composition root shares the configured asset runtime and database pool.
Missing configuration or unavailable storage returns 503. Existing
`asset_allowed_hosts`/`asset_allowed_origins` apply through the same extracted
policy as asset routes. Host authorities reject userinfo, path/query/fragment
separators even when the parsed component is empty; no new identity system or client-selected endpoint is
introduced. Fixed public errors never include SQL, credentials or private paths.
The runtime OpenAPI includes only the two implemented GETs with namespaced admin
components; planned workspace POST/PATCH and policy-management routes remain
unregistered. The optional Python service injection is for explicit composition
and isolated tests, and never creates a fallback record.

Unit, actual non-owner PG/MinIO ASGI and native Uvicorn TCP HTTP tests cover
malformed and repeated
parameters, canonical cursor paging, disabled/missing/corrupt records, locked
storage deadlines, read-only enforcement, service reconstruction, configured
lifetimes and the original asset upload/read path. These tests use synthetic
content and do not qualify a speech engine, GPU capacity or production network.
