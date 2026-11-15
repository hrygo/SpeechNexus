# Deployment bootstrap

This directory defines a local, single-node Compose topology for the **minimal
synchronous speech surface**. Following
[ADR 0004](../docs/decisions/0004-cluster-ready-architecture-and-single-node-mvp.md),
the architecture remains cluster-ready, but this bootstrap is not a production
capacity, multi-node high-availability or model-qualification claim. Release
images must still be pinned by digest before publication; the bootstrap uses
explicit version tags for readability.

Start with the executable, command-by-command guide:
[Minimal synchronous ASR/TTS Compose deployment](../docs/operations/compose-minimal.md).
It covers environment generation, private-config preparation, migrations,
non-owner roles, readiness probes and one-time engine registration.

## Services and profiles

The base `docker compose --env-file deploy/compose.env.example -f deploy/compose.yaml config --services`
set is intentionally limited to:

- `postgres` and `minio`: private state and object storage;
- `minio-init`: creates and versions the input/result buckets;
- `api`: public SpeechNexus API;
- `gateway`: execution gateway with private file-ASR/file-TTS configuration.

The following production units are deliberately **not** part of this file:
`node-controller`, `runtime-delivery-worker`, `asset-maintain` and
`voice-evidence-worker`. Their absence is a scope statement, not an assertion that
those capabilities work elsewhere. They are excluded because controller lifecycle,
runtime delivery, asset retention maintenance and automatic evidence recording
(which needs a recorder DSN plus report object storage) are not on the synchronous
request path or are not assembled here. Artifact retention is separate: it is
assembled by the opt-in `artifact-retention` profile described below.

Additional profiles are opt-in:

- `migration`: one-shot Alembic migration with the separate migration DSN;
- `engine-registration`: one-shot `speechnexus-register-engine` invocation;
- `engine-readiness`: receipt renewal (`engine-readiness-renew`, the same
  registration with `--renew-readiness`) and a read-only report
  (`engine-readiness`). Every create re-checks the pinned instance's readiness
  receipt, whose TTL is 24 h by default, so this profile is the periodic path that
  keeps it valid. See
  [Engine readiness renewal](#engine-readiness-renewal-engine-readiness-profile);
- `web`: the static Admin and Workbench frontends;
- `binding-disable`: explicitly starts the disable worker, which is required to
  process persisted disable intents; it is absent from the base topology.
- `job-dispatch`: explicitly starts the public Job dispatcher
  `job-dispatch-worker`; it is absent from the base topology and the base `up`
  never starts it.
- `artifact-retention`: explicitly starts `artifact-retention`, the bounded sweep
  that acts on the `expires_at_us` every published Result Artifact records. It is
  absent from the base topology: without it, publication records a deadline but
  nothing removes content. See [Artifact retention](#artifact-retention-artifact-retention-profile).
- `asset-inspection`: explicitly starts the private audio inspection container
  `asset-inspector`; it is absent from the base topology, publishes no host port
  and joins only a dedicated `internal: true` network. Enabling it requires
  `SPEECHNEXUS_ASSET_INSPECTOR=audio`, `SPEECHNEXUS_ASSET_INSPECTION_URL` and a
  non-empty `SPEECHNEXUS_ASSET_INSPECTION_TOKEN` in `compose.env`. The design and
  the verified assembly facts are in the
  [asset inspection container](../docs/components/asset-inspection-container.md#44-compose-装配).
- `workbench-api`: explicitly starts the independent Workbench API unit:
  `workbench-migrate` (one-shot owner migration of the workbench namespace plus
  the grants for its two dedicated roles) and `workbench-api` (the service
  `WEB_WORKBENCH_UPSTREAM` names), behind
  `deploy/workbench-startup-gate.py`. See
  [Workbench API](#independent-workbench-api-workbench-api-profile).

The `workbench` container serves only the frontend; the independent Workbench API
is the separate `workbench-api` profile described below. Setting
`SPEECHNEXUS_PROCESS_ROLE` identifies a process; it does not switch the backend
command to another service factory.

Both Web images require `WEB_SPEECH_UPSTREAM`, `WEB_WORKBENCH_UPSTREAM` and
`WEB_DNS_RESOLVER`. Origins must be `http://` or `https://` followed by a DNS
name or IPv4 address and an explicit port, without a path or credentials. DNS
resolver is an IPv4 address. Invalid or missing values stop startup; there is no
Workbench fallback. The example Workbench hostname is the `workbench-api` service
this file assembles when its profile is enabled, so the frontend has a real
upstream address; without that profile the name does not resolve and the requests
fail visibly. DNS lookup occurs on requests so an unavailable Workbench does not
prevent Speech startup.
HTTPS verifies the upstream certificate and hostname against the image system CA
bundle; install a trusted deployment CA in that bundle when required. Use HTTPS
for cross-host connections, and separately protect the browser-facing ingress.
Paths and queries pass through unchanged. This proxy configuration alone does not
qualify a real service, cross-host TLS, or a physical topology.

## Current capability limits

`/healthz` and `/readyz` are startup/dependency probes, not business acceptance:

- public synchronous ASR is still blocked by the missing production terminal-state
  writer; a live request can return `504 operation_timeout` even when the gateway
  staged a result;
- public synchronous TTS is assembled here — recipe sample, language, settlement
  role and the API identity's speech grants are all part of this file (see
  "Synchronous TTS assembly" below) — but it is not exercised: this bootstrap
  provides no real engine, no published and qualified builtin voice, and no
  public `200` has been run against it;
- public asynchronous Job execution is not qualified here: the opt-in
  `job-dispatch` profile only runs the dispatcher, this bootstrap provides no real
  engine, and submission still needs the Job writer and admission roles below;
- both Web images render a blank document in a real browser under their own
  Content-Security-Policy: `deploy/web/nginx.conf` sends `script-src 'self'` with no
  `'unsafe-eval'`, while the generated contract validators compile through `ajv`,
  which builds its functions with `new Function`. Verified 2026-11-15 against
  same-version `speechnexus/web-workbench` and `speechnexus/web-admin` images (empty
  body, `EvalError` on the CSP directive), with the same bundle rendering fully when
  served without that header. The CI browser gates drive `vite preview`, which sends
  no CSP header, so they cannot observe this; tracked as #840.
- real model quality, CUDA capacity, physical multi-node recovery, content-network
  isolation and final Gate approval require separate evidence.

Do not use a green Compose health check to close any of those items.

## Synchronous TTS assembly

`synchronous TTS` is assembled by this file, and that is all it means: the four
inputs exist in the topology, not that a synthesis succeeds. The inputs are

- `SPEECHNEXUS_SPEECH_RECIPE_FILE`, fixed to
  `/run/speechnexus-private/speech-recipe.json` and copied from
  [private/speech-recipe.example.json](private/speech-recipe.example.json). It is
  set unconditionally for the same reason as the transcription recipe: an
  operator who forgets to copy it gets an explicit startup failure from the loader
  (`Invalid private speech admission recipe /run/speechnexus-private/speech-recipe.json
  (FileNotFoundError)`), not a silent "TTS off";
- `SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE`, forwarded with the reference value `en`
  because the recipe does not carry a language;
- `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL`, the independent
  `sn_speech_settlement` role the runbook's §6 creates. It is forwarded with an
  empty default so the base topology renders before that role exists; a blank
  value keeps synchronous TTS off, and the API logs every missing TTS key by name
  instead of guessing;
- the existing gateway URL, component credential and object-storage keys, which
  synchronous ASR already requires.

The API identity also needs `speech_synthesis_reader_grants(schema, "sn_api")`;
both provisioning paths in the runbook replay it. Without it the factory starts,
reports no qualified voice, and keeps TTS off even with every key present.

### Checking a copied assembly

`uv run --all-packages python scripts/dev-env/check-tts-assembly.py --private-dir deploy/.runtime/private`
puts **all three documents through their own production loaders** — `load_tts_recipe`,
`load_controlled_tts_endpoint` and the gateway's `load_file_tts_configuration` — so a
value a loader refuses is red, and the gateway fields its own rules can name
(`tts_policy.task`, `sample_rate_hz`, `speed`, …) are named one by one. Above that it
reports every field in four states: still the committed example's value, inconsistent
(with another file, with a domain constant, or expired), decided by a local check
(a domain constant, a legal set, a mutual relation, the clock), or **undecidable from
the files**. A fifth, explicitly listed group holds the leaves only their loader
checks, so a field never disappears from the output. Every leaf in the three
documents lands in exactly one of those groups.

There is no "ready" verdict. `exit 0` means "nothing is still the example's value and
the files agree" — it does **not** mean the deployment can accept a request, and the
tool prints that sentence itself. Values with no local oracle (plan budgets, the real
engine address, the registration generation, whether a policy revision matches the
voice qualification) stay undecidable no matter how the file is edited.

### What acceptance still needs

Replacing every field is necessary and not sufficient. A first request additionally
requires, in order, all of it verifiable only against a running deployment:

0. **A settlement DSN that actually composes the factory.** With
   `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` blank, `build_speech` logs every
   missing key and returns `None` (`speech_runtime.py:49-66`), so `/v1/audio/speech`
   answers `503 backend_unavailable` no matter how perfect the three files are. The
   `sn_speech_settlement` role and its two grants are in runbook §6.
1. **A registered TTS instance.** `runtime_instance_heads` must hold
   `recipe.stage.instance_id` at the generation the endpoint descriptor pins
   (`qualified_speech_facts_postgres.py:76` selects `current_generation` and raises
   `missing instance head` otherwise). Registration is
   `speechnexus-register-engine`; this Compose's `engine-register` service only
   loads the **transcription** recipe (`engine_registration.py:1136` calls
   `load_transcription_recipe`), so a TTS instance is registered in manual mode
   (`_spec_from_manual`, `engine_registration.py:1072`) with `--model --node --pool
   --gateway --instance --template-id --binding-revision-id --manifest-id
   --config-digest --topology-digest --qualification-digest --architecture
   --service-class --loading-policy --max-slots --policy-revision --peak --steady
   --per-slot`. The async lane and the TTS lane register different instances.
2. **Registration values equal to the recipe.** Admission reads
   `engine_instances` joined with `runtime_instance_heads` and refuses unless
   `current_generation == instance_generation`, `(host_node_id, pool_id,
   gateway_id)` equals the recipe's plan binding, and `binding_revision_id`,
   `manifest_id`, `approved_template_id`, `config_digest` all equal the recipe's
   values (`transcription_admission.py:88-123`, surfaced as
   `TranscriptionUnavailableError … disagree with the private recipe`). Registration
   also refuses a plan whose budget does not fit the host's declared capacity
   (`_require_capacity`, `host_capacity_insufficient`, `engine_registration.py:494`),
   so the recipe's budget must be the one registration accepted.
3. **`policy_revision_id` matching two independent facts.** The workspace quota row's
   `policy_revision` must equal it (`transcription_admission.py:116`), and the
   builtin voice approval's `gateway_policy_revision_id` must equal it
   (`backend/src/speechnexus_backend/speech/infrastructure/builtin_voice_qualification.py:54`). A mismatch is
   `AdmissionRejectedError("policy_or_resource_disabled")`.
4. **A published, non-retired builtin voice under that binding**, with a live source
   approval (`has_qualified_voice`, `qualified_speech_facts_postgres.py:56-62`);
   otherwise the factory logs `no current qualified voice` and keeps TTS off.
5. **A live engine behind the descriptor.** `address`/`port`/`served_model` and the
   64-hex token are only proven by a request: nothing in the files reaches the
   engine.

The narrow-scope real `200` was observed on MegaCube in the 2026-11-03 window; this
reference deployment has not re-run it.

### An ASR-only deployment

`SPEECHNEXUS_SPEECH_ENABLED` is the one supported way to run without synchronous
TTS. It is a `Literal["true", "false"]` with the default `true`, so it is
fail-closed in both directions:

- **unset, blank or `true`** — enabled. `compose.yaml` always forwards the fixed
  recipe path, so a deployment that never touches the switch still refuses to start
  when the private recipe is missing, and "TTS off" cannot happen by accident. (A
  deployment that also deletes `SPEECHNEXUS_SPEECH_RECIPE_FILE` itself falls back to
  the pre-existing "missing configuration" warn-and-off path; this switch neither
  adds nor widens that path.)
- **`false`** — disabled on purpose. `build_speech` logs
  `Synchronous speech disabled by SPEECHNEXUS_SPEECH_ENABLED=false` and returns
  `None`; the rest of the process composes normally and ASR, Jobs and the UIs are
  unaffected;
- **anything else** (`ture`, `FALSE`, `1`, `off`) — the settings refuse startup with
  `SPEECHNEXUS_SPEECH_ENABLED must be 'true' or 'false'; got 'ture'`. A typo can
  never be read as "off", which is exactly what makes an explicit switch safe here.

Set it in `deploy/compose.env`; do not edit `deploy/compose.yaml`. The scripted
golden path is only reachable by adding the key, so no fork of the repository file
has to be maintained across upgrades.

## Local endpoints

Published ports bind to loopback only:

- Speech API: `http://127.0.0.1:8000`
- Execution gateway: `http://127.0.0.1:8010`
- PostgreSQL: `127.0.0.1:55432`
- MinIO S3: `127.0.0.1:59000`; console: `127.0.0.1:59001`
- Admin UI (`web` profile): `http://127.0.0.1:8080`
- Workbench UI (`web` profile): `http://127.0.0.1:8081`

**`SPEECHNEXUS_ASSET_INSPECTOR=text` is the shipped default and it makes audio
uploads fail.** With `text`, `TextAssetInspector` returns `available=False` for
every purpose other than `text_input`, so an `asr_input` or `voice_reference`
upload answers `503 asset_inspection_unavailable`; only `text_input` validates.
This does not affect synchronous TTS (text in, audio out) or text ASR — it blocks
audio *uploads* until `audio` is selected together with the inspection URL and
token. The default stays `text` deliberately: `audio` needs a separate container
(the opt-in `asset-inspection` profile), a shared token generated outside the
repository, and it refuses startup when either is missing, so flipping the
default would ship a topology that cannot start without those inputs and would
turn a documented fail-closed 503 into a runtime surprise. A deployment that
needs audio uploads selects `audio` explicitly; the failure mode is named in the
runbook and in
[asset inspection](../docs/components/asset-inspection-container.md#44-compose-装配).

The `asset-inspection` profile publishes nothing: `asset-inspector` is reachable
only as `http://asset-inspector:8080` from containers on its `internal: true`
network, so the host has no inspection endpoint to probe. The API deliberately
does not depend on the container being healthy: with `audio` selected, a stopped
or misconfigured inspector answers `503 asset_inspection_unavailable` per request
instead of blocking API startup. A blank token makes the inspector container exit
at startup naming `SPEECHNEXUS_INSPECTION_TOKEN`; the resulting restart loop is a
configuration error, not a ready service.

The gateway's private file-ASR/file-TTS object adapter requires a private IP
literal rather than a DNS name, so MinIO receives the fixed Compose-network
address `172.28.0.10`. If `172.28.0.0/24` conflicts with the host, change the
subnet and both private JSON object endpoints together. The examples and
private-file rules are in [deploy/private](private/README.md).

`postgres-data` and `minio-data` are named volumes. `minio-init` creates and
verifies versioning for the transcription input bucket, the Artifact bucket and
`SPEECHNEXUS_EXECUTION_RESULT_BUCKET` (the gateway staging bucket the dispatcher
reads back). It uses the local MinIO root identity only; a real deployment must
provision separately scoped object credentials and reviewed buckets outside this
bootstrap.

## Build and dependency boundary

The Python image is the shared composition for API, gateway, bucket init,
migration, registration and the Job dispatcher. It now includes:

- the backend migration extra so the opt-in `migrate` profile can run;
- the hash-pinned CPU VAD requirements from
  [engines/cpu-vad/requirements.txt](engines/cpu-vad/requirements.txt), because
  configured file-ASR validates and opens the Silero ONNX model at gateway
  startup.

The build contexts include the shared contracts workspace. `.dockerignore`
excludes runtime data, credentials, local dependencies and caches. When a build
runs behind a deployment/session CA, supply its trusted combined CA bundle as
BuildKit secret `proxy_ca`. The optional secret is mounted only in networked
dependency-install steps and is not copied into the final image. Do not disable
TLS verification.

### Web images

`deploy/images/web.Dockerfile` builds one application per image
(`--build-arg APP_NAME=admin` or `workbench`). Its `COPY` set is the complete input
set of `pnpm --filter "@speechnexus/${APP_NAME}" build`: the locked install, the
application's production TypeScript program (`web/<app>/tsconfig.build.json`), the
workspace packages it imports, and the root configuration `pnpm`/`tsc` read
(including `.npmrc`, so the image enforces the same engine policy as CI). Test
material under `tests/**` is deliberately absent — the image must not contain test
data — so product sources may not import it. The dev scenario pages are reached
through a Vite glob behind `import.meta.env.DEV`, and the test-only fixture helpers
sit outside the production program, which is what keeps `tests/fixtures/contracts`
out of the image.

The `COPY` set is therefore a build input contract, not an incidental list. CI
builds with the whole repository present, so a source or config outside the set
fails only when someone builds the image (#834: both Web images were unbuildable
while every CI lane was green). `pnpm verify:web-image` — also a step of the CI Web
job — materialises exactly the `COPY` set, installs and builds both applications
inside it, and fails when anything outside the set is needed. Extend the set
deliberately, and only with files the build reads.

Build one application per invocation and record the source commit in the image, so a
running deployment can be traced back to the tree that produced it:

```bash
sha="$(git rev-parse HEAD)"
docker build -f deploy/images/web.Dockerfile \
  --build-arg APP_NAME=workbench \
  --label "org.speechnexus.source.commit=${sha}" \
  --label "org.speechnexus.source.dirty=$(git status --porcelain | wc -l | tr -d ' ')" \
  --label org.speechnexus.app=workbench \
  -t "speechnexus/web-workbench:${sha}" .
```

Builds complete without a mirror change and without an extended window: on
2026-11-15 `docker pull node:26.12.1-bookworm-slim` took 4.24 s, an uncached
(`--no-cache`) build of the Workbench image took 53.44 s with exit code 0, and the
per-tarball `registry.npmjs.org` warnings the install prints (1–45 KiB/s on
individual packages) do not set the pace because `pnpm` fetches in parallel. A warm
`pnpm` store only shortens repeat builds. The earlier "stuck on the node layer at
about 50 KB/s" report (#572) does not reproduce on this host.

The build is reproducible at the bundle level: a cached build and a `--no-cache`
build produce the same 24-file `web-dist` tree
(`80ada7a38a4e08dce8c3009cd196482e26d9854b8930c7e44ef9136f2b7d7f9d`), which is also
the tree a host `pnpm --filter @speechnexus/workbench build` produces, so that
digest identifies source bytes rather than build cache. The nginx base image adds
one further file, `50x.html`, that is not part of the application bundle.

To deploy an image built this way instead of letting Compose build once more, pin it
in a deployment-side override file and pass it alongside the repository file:

```yaml
services:
  workbench:
    image: speechnexus/web-workbench:<commit>
```

The `web` profile still selects the service; adding `--no-deps` starts the frontend
without pulling the base API topology in, which is how a frontend-only check runs.

## Configuration authority

Every Python process validates its own `pydantic-settings` configuration and
declares explicit node, role and instance identity. There is no hidden fallback
service endpoint or implicit migration-owner runtime identity:

- `SPEECHNEXUS_DATABASE_URL` comes from `SPEECHNEXUS_API_DATABASE_URL`;
- `SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL` must name the non-owner gateway role;
- `SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL` must name a separately provisioned,
  non-owner reader with only `gateway_query_grants` column privileges. The default
  control lifespan refuses missing or unsafe authority; it never reuses the writer
  credential. Each original-execution GET uses one repeatable-read, read-only snapshot;
- registration and migration use separate one-shot DSNs;
- file-ASR/file-TTS credentials and configuration are always supplied together;
- private files must be owner-only regular files under the mounted private path.

Production entry points, Host/Origin policy, shared service credentials, backup,
object IAM and capacity qualification remain deployment decisions. The initial
product has no login, user directory, RBAC or tenant isolation.

## Optional writers and background units

Asset HTTP routes can use the local object credentials, but production must
replace them with narrowly scoped credentials. The
[asset configuration and recovery guide](../docs/components/production-assets.md)
describes paired object keys, pre-created versioned buckets, non-owner runtime
grants, the 7-day default/explicit TTL policy and Host/Origin guards. The
`speechnexus-asset-maintain` entry point is not started here and must be
provisioned separately before retention/unknown-original polling is claimed.

The public Job dispatcher is registered separately as
`speechnexus-job-dispatch-worker` and is assembled by the opt-in `job-dispatch`
profile (see below). It requires explicit worker identity, a non-owner PostgreSQL
role, the execution-gateway origin/credential and every batch/lease/poll/timeout
bound in
[production jobs](../docs/components/production-jobs.md#worker-入口与运行配置del-98).
It is not part of this minimal surface.

## Job dispatcher (`job-dispatch` profile)

`job-dispatch-worker` runs the public Job dispatcher entry point
`speechnexus-job-dispatch-worker`. The base `up` never starts it. Prepare the
worker DSN and its grants first; only then start the profile:

```bash
docker compose --env-file deploy/compose.env \
  -f deploy/compose.yaml --profile job-dispatch up -d
```

The service has no healthcheck, and a blank DSN makes it exit before claiming any
lease. With `restart: unless-stopped` that shows up as a restart loop, while
`docker compose ... up -d` (and `--wait`, which has no probe to wait on) still
returns success. A successful `up` is therefore not evidence that the dispatcher
is ready: confirm the worker is running without restarts, for example with
`docker compose --env-file deploy/compose.env -f deploy/compose.yaml ps` and
`logs job-dispatch-worker`, and fix the DSN/grants before retrying.

The container entrypoint is not the worker: it is `deploy/job-startup-gate.py`,
mounted read-only and given the worker's own environment. Before it `exec`s
`speechnexus-job-dispatch-worker`, the gate verifies the prerequisites the worker
could only discover _after_ claiming a lease and names each missing one:

- the dispatch DSN is present and its role is not a superuser, does not own schema
  `CREATE`, the database or any schema relation (the same guard the worker runs);
- `speech.alembic_version` exists, the dispatch role can read it, and its single
  row equals the packaged migration head. An unreadable head is a startup failure,
  not a `pending` line: the gate cannot tell a missing read-only grant from a
  database that is genuinely behind, so it refuses to start and names
  `GRANT SELECT ON speech.alembic_version TO <role>` — the upgrade prerequisite in
  the minimal runbook §6. The eight startup `SELECT` probes are not a substitute:
  they cover the columns this build reads, not views, tables, indexes or other
  columns a migration adds;
- the worker's own `SELECT` probes pass;
- every column the dispatcher writes after claiming is granted, across the 10
  relations it writes: the Job transition (`speech_jobs`, `coordination_jobs`,
  `speech_job_events`, `speech_job_cancellation_not_submitted`), the committed
  result Artifact (`speech_artifacts` INSERT) and the capacity release of a
  terminal Job (`admission_leases`, `admission_attempts`, `workspace_quotas`,
  `host_nodes`, `engine_instances` UPDATE) — 67 columns in total. A role missing
  `job_dispatch_grants`, `artifact_publisher_grants` or
  `coordination_runtime_grants` stops at container start instead of failing after
  the first claim or the first result publication;
- every requested lane is one this bootstrap assembles:
  `SPEECHNEXUS_JOB_DISPATCH_KINDS` must stay inside `asr`/`tts`, and any other
  kind is named and rejected before a claim (see the lane rules below);
- both `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` (Artifact) and
  `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` (gateway staging read-back) exist and have
  versioning enabled; the gateway's private `result_bucket` in
  `gateway-file-asr.json`/`gateway-file-tts.json` equals the latter. This covers
  the staging bucket that `check_ready` alone never probed, so a missing or
  unversioned result bucket no longer fails only after the first publication.

A failure exits non-zero with `FAIL: <named prerequisite>` and the worker never
starts. Exactly two inputs are printed as `pending` lines because they are
deployment inputs this container cannot verify: the real engine and a registered
`ready` instance, and the API-side writer/admission DSNs — the API already answers
`503 job_unavailable` at creation for a blank Job writer/admission DSN, which is
before any claim. The registered instance's readiness receipt also carries a TTL
that only a periodic renewal keeps fresh: an elapsed receipt answers
`503 engine_not_ready` (retryable) instead of the storage `job_unavailable`, and
the `engine-readiness` profile is that renewal. Every other prerequisite above is a
hard failure. The gate reads the gateway configs through
`SPEECHNEXUS_JOB_GATE_PRIVATE_DIR` (`/run/speechnexus-private` in Compose); it never
reads credentials from them.

Provision the three Job roles with
`uv run --all-packages python scripts/dev-env/provision-job-roles.py` after the
migration profile; it reads the role names and passwords from the three Job DSNs
and installs the five dispatch grant groups, the writer baseline and the read-only
`speech.alembic_version` `SELECT`. When the selected kinds include
`voice_validation` it also installs that lane's two extra `SELECT`s, which only
matter once the lane itself is assembled (see below). The step-by-step path
is [the minimal deployment runbook §6](../docs/operations/compose-minimal.md#异步-job-运行角色与授权).

## Engine readiness renewal (`engine-readiness` profile)

Registration signs a readiness receipt for the one instance it makes `ready`, and
admission re-reads that receipt's window on every create
(`VerifiedRuntimeReadiness.require_valid`): `verified_at_us <= now < expires_at_us`.
`--readiness-ttl-seconds` sets the window and defaults to `86400` (24 h), so a
deployment that registers once and never renews starts answering
`503 engine_not_ready` exactly one TTL later. That answer is deliberately not
`503 job_unavailable` ("Job storage is unavailable"): the store is healthy and
re-signing the receipt admits the same body under the same Idempotency-Key. Both
are `retryable: false` -- `retryable` is true only when the same request succeeds
without an out-of-band change, and re-signing a receipt is an operator step -- while
the message names the renewal command. It is also not `429
job_capacity_exhausted`, which a released attempt can free, nor 503
`job_admission_contended`, the one admission refusal that does retry.

The other engine-side refusals keep their own codes for the same reason: 503
`engine_state_mismatch` (the pinned runtime state or receipt identity disagrees),
503 `engine_resource_disabled` (instance, pool or recipe approval not active) and
503 `recipe_not_admitted` (the request came from another recipe). None of them means
the Job store is down, and each message names the step that restores it.

The receipt cannot be re-signed from the database alone — it records a real
`/health` probe — so renewal re-runs the registration with an explicit switch:

```bash
# one renewal: same identity, same generation, fresh receipt from a new probe
docker compose --env-file deploy/compose.env \
  -f deploy/compose.yaml --profile engine-readiness run --rm engine-readiness-renew

# read-only report: one JSON line per ready instance, then a summary
docker compose --env-file deploy/compose.env \
  -f deploy/compose.yaml --profile engine-readiness run --rm engine-readiness
```

`engine-readiness-renew` runs the installed `speechnexus-register-engine` with
`--renew-readiness`, which re-signs the receipt even while it is still valid; that
is what makes a timer shorter than the TTL work. Without the flag a registration run
only refreshes an already-elapsed receipt. The renewal updates the receipt and
`engine_instances.ready_at_us` in one transaction, increments both row versions, and
never starts a new instance generation, so it is safe to run repeatedly. Compose
does not schedule it: call it from the host timer, e.g. hourly with cron or a
systemd timer, at an interval shorter than the TTL and the warning window.

`engine-readiness` runs the installed `speechnexus-engine-readiness`, the surface
that makes the window observable before a request fails. It writes nothing, probes nothing and opens a read-only session; for
each `ready` instance it prints `readiness_state` (`valid`, `expiring`, `expired`,
`missing` or `invalid`), `expires_at_us`, `remaining_seconds` against the database
clock admission reads, and the command that repairs it, followed by a summary line:

```text
{"instance_id":"instance-asr-1","instance_generation":1,"target_node_id":"node-local-1","readiness_state":"expiring","expires_at_us":1794664410587133,"remaining_seconds":1800,"remediation":"run speechnexus-register-engine --renew-readiness before expires_at_us"}
{"summary":{"ready_instances":1,"valid":0,"expiring":1,"expired":0,"missing":0,"invalid":0,"unusable":0}}
```

`--warn-within-seconds` (`SPEECHNEXUS_READINESS_WARNING_SECONDS`, default `21600`,
a quarter of the 24 h TTL) sets the `expiring` threshold; it is a reporting choice,
never an admission rule. The exit code is non-zero when any receipt is `expired`,
`missing` or `invalid`, so a supervisor can alert on it; `--fail-on-expiring` also
fails on `expiring`, which lets a probe alert before creates start failing. The
renewal command writes its own signal: `--renew-readiness` reports the window it
found and the fact it re-signed in `readiness.state` / `readiness.renewed` on
stdout, and warns on stderr when that window was `expiring` or `expired`.

The same fact is stated where an operator meets it first: the `job-dispatch`
startup gate's `pending` line for the real engine names this renewal, and the Job
create contract declares `engine_not_ready` with its recovery. `deploy/README.md`
does not claim the renewal is scheduled; a deployment that skips the timer trades a
24 h grace period for the failure this profile exists to prevent.

Known gap, tracked as [#845](https://github.com/hrygo/SpeechNexus/issues/845): this
deployment has no automatic _within-cluster_ signal for the state itself. `api`'s
healthcheck is `/readyz`, which deliberately keeps answering 200 because reads and
cancellations must stay available; the readiness report is a one-shot that compose
does not schedule; and the startup gate prints its `pending` line once. So a
deployment that stops renewing answers every create with `503 engine_not_ready`
while `/readyz` and the container health stay green. The timer above narrows that
window and `engine-readiness` makes it observable to whatever supervises it, but
until #845 lands an independent, pollable "can this deployment admit a create?"
surface, an operator must rely on the externally scheduled report.

## Artifact retention (`artifact-retention` profile)

Publication writes `expires_at_us = created_at_us + SPEECHNEXUS_ARTIFACT_RETENTION_SECONDS`
on every committed Result Artifact, so the window is a recorded fact rather than a
process default. `artifact-retention` is what acts on it:

```bash
docker compose --env-file deploy/compose.env \
  -f deploy/compose.yaml --profile artifact-retention up -d
```

One pass has two bounded phases. It first moves committed `ready` rows whose
`expires_at_us` the database clock has passed to `expired`, then removes the exact
object version each `expired` row pins and records `deleted` only after a
follow-up stat confirms the version is gone. A row inside its window is never
selected by either phase, and both the selection and the state transition repeat
the deadline predicate, so a widened query cannot remove content early. An
unconfirmed removal leaves the row `expired`: the download answer stays
`artifact_expired` and the next pass retries. The row keeps `created_at_us` and
`expires_at_us`, and a confirmed removal also writes `deleted_at_us` in the same
transition, so each decision — including when content went away — remains
reviewable in the database after the fact, not only in the process log.

The service runs as its own identity, `sn_artifact_retention`
(`SPEECHNEXUS_ARTIFACT_RETENTION_DATABASE_URL`, forwarded into the container as
`SPEECHNEXUS_DATABASE_URL`), with `artifact_retention_grants`: `USAGE` on the
schema, `SELECT` on `speech_artifacts` and `UPDATE (artifact_state, deleted_at_us)`
only. Startup refuses a role that holds `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`,
`REFERENCES` or `TRIGGER`, or `UPDATE` on any other column, because that would
widen the sweep into a content-deletion authority; it also refuses the API runtime
identity for the same reason, which is why the two DSNs are separate. `SPEECHNEXUS_ARTIFACT_RETENTION_INTERVAL_SECONDS` and
`SPEECHNEXUS_ARTIFACT_RETENTION_BATCH_SIZE` bound a pass. Each decision logs one
JSON line with the Artifact id, the decision, the recorded deadline and the
database instant, followed by a per-pass summary:

```text
{"artifact_id":"artifact-…","decision":"expired","event":"artifact_retention","expires_at_us":1767225600000000,"observed_at_us":1767312000000000,"reason":""}
{"artifact_id":"artifact-…","decision":"deleted","deleted_at_us":1767312000000000,"event":"artifact_retention","expires_at_us":1767225600000000,"observed_at_us":1767312000000000,"reason":""}
{"batch_size":10,"blocked":0,"decisions":2,"deleted":1,"event":"artifact_retention_pass","expired":1}
```

Migration `0054_artifact_deleted_ledger` adds the column; rows a deployment had
already marked `deleted` before it keep an honest `NULL` (nothing in the schema
records when a state changed), and the consistency check is installed `NOT VALID`
so only writes from now on are constrained. Count them with
`SELECT count(*) FROM speech_artifacts WHERE artifact_state = 'deleted' AND deleted_at_us IS NULL`
and treat that number as "deleted before the ledger, not traceable" rather than
backfilling a computed instant.

The deadline is written from the application clock but judged against the
database clock, so the observed window is the configured one minus the skew between
the two (application clock ahead ⇒ earlier expiry, behind ⇒ later). Keep node
clocks within their normal synchronisation error; raising the window is not the
fix for a skewed host. Rows without a recorded deadline are never selected.

A single pass runs on demand as
`docker compose … run --rm artifact-retention speechnexus-artifact-retention --once`
(or `uv run speechnexus-artifact-retention --once` against a prepared environment).

The download route keeps the three outcomes apart: `410 artifact_expired` when the
recorded window ended, `410 artifact_deleted` after retention removed the content,
and `404 artifact_not_published` for a minted result identity with no committed
row (staging content is not addressable). A `404 artifact_not_found` identifier was
never issued by this API. `GET /v1/nexus/jobs/{job_id}` still requires the
committed Artifact to be `ready`, so reading a succeeded Job whose content has
already been retired answers `503 artifact_service_unavailable`: retention moves
the content, and the Job row itself is never deleted. Settlement is unaffected —
a dispatcher replay of the same publication still reaches `succeeded` after the
retention window elapsed, because the replay asks whose publication it is rather
than whether content is still servable.

The command-by-command path from a blank environment to one submitted Job,
including the six prerequisites above and a read-only preflight, is
[the asynchronous Job runbook](../docs/operations/async-job-runbook.md).
Its self-check is `scripts/dev-env/check-async-job-prereqs.py`.

The service shares the gateway network namespace and calls
`http://127.0.0.1:8010`, because `JobDispatchSettings` rejects plain HTTP outside
loopback. That is a single-host Compose property, not a multi-node transport
design. `SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` defaults to blank and must name a
non-owner worker role; a blank value fails closed before any lease is claimed.
[production jobs](../docs/components/production-jobs.md#worker-入口与运行配置del-98)
lists the complete variable set and its bounds; `compose.yaml` supplies explicit
reference values for every one, and the worker itself has no in-process default
for them. `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` (Artifact output) and
`SPEECHNEXUS_EXECUTION_RESULT_BUCKET` (gateway staging read-back) must be buckets
`minio-init` created and versioned, and the latter must match the private gateway
configs' `result_bucket`. The reference environment uses `speechnexus-results` for
both. Because `minio-init` receives the same variable, a non-default staging
bucket is created and versioned before the worker starts; if `minio-init` fails,
`depends_on: service_completed_successfully` keeps the worker from starting at
all. The local bootstrap still gives the worker the MinIO root identity
(`MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`), as it does for the API and gateway. That
is a local convenience, not object-layer least privilege: `read_only`, `cap_drop`
and `no-new-privileges` constrain the container process only, not S3
authorization, so production must supply narrowly scoped object credentials.

`SPEECHNEXUS_JOB_DISPATCH_KINDS` selects the lanes this process claims and
defaults to `asr,tts`. It is comma-separated and de-duplicated, and it must stay
inside the lanes this bootstrap assembles: `deploy/compose.yaml` forwards exactly
the private ASR and TTS gateway configs, so `asr` and `tts` are the only kinds the
gate accepts. Any other kind — `voice_design`, `voice_clone`, `voice_validation`
or a value outside the dispatchable union — is named and rejected at container
start, before a claim. `voice_design`/`voice_clone` are non-dispatchable generation
lanes, and `voice_validation` submits through the voice-clone command lane, which
this file never configures for the gateway: the worker would claim such a Job and
fail after the claim, or never be served an engine for it.

Serving another lane is a topology change, not a `kinds` change: add its private
gateway config and `SPEECHNEXUS_GATEWAY_FILE_*_CONFIG_FILE` entry to
`compose.yaml`, add the kind to the gate's `GATEWAY_CONFIGS`, install that lane's
extra grants, and only then select it. For `voice_validation` the extra grants are
`voice_validation_worker_grants(schema, worker_role)`: beyond `USAGE` on the schema
it grants only two `SELECT`s — on `speech_voice_draft_revisions` and
`speech_assets` — the frozen draft revision and the ready reference Asset the
validation reader replays (#553). It grants no draft writes, publication tables or
object-storage rights, and the worker probes both tables when that lane is
selected.

The startup check runs before the dispatcher exists, so a missing grant stops the
process (non-zero exit, a restart loop under Compose) instead of claiming a Job
and failing at publication. The same role check rejects superuser, schema
`CREATE`, database owner and membership in any table owner of the configured
schema; it does not audit extra DML grants, so a broader role is not rejected and
minimum privilege remains the deployment's responsibility. Do not treat the API
role's superset as worker evidence. A running dispatcher is still not a real
engine, GPU or end-to-end asynchronous acceptance.

预设草稿 HTTP writer 默认关闭。仅在操作者已应用 `0009_profile_drafts` 并独立配置最小非 owner 角色后，设置 `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`；不得复用 Compose bootstrap owner。启用 writer 必须同时设置 `SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE`，指向部署方评审过的发布资格文档：该文档按 revision 绑定已存定义摘要与评审过的校验报告摘要，仓库不提供任何默认资格。缺任一输入、文档不可读或格式非法时 API 在启动阶段点名变量退出，不会把发布留到运行时返回 `503 profile_result_unknown`。角色预置与资格文档生成步骤见 [最小部署 §6](../docs/operations/compose-minimal.md#预设发布资格输入与-sn_profile_writer-角色)。启动仅验证权限与资格文档并管理独立连接池，不执行迁移或 grant。原请求/原键重放、固定读取和限额见 [profile drafts](../docs/components/profile-drafts.md#http-runtime-and-controlled-writer)。

绑定草稿选择一个显式模式：`SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` 使用旧三表角色；
`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` 使用预先迁移到 0011 的四表最小角色，
启用原子默认值声明与固定快照 GET。两者互斥、默认均为空；没有通用数据库身份回退，
也不会自动扩旧角色权限。固定 binding GET 另需
`SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL`：一个只读非 owner 角色，可 SELECT
binding 历史、`speech_publications` 与证据表。未配置时 GET 返回
`503 binding_unavailable`，不会把已发布 revision 谎报为 draft。绑定列表另需
`SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL`：只读非 owner 角色，可 SELECT
`speech_binding_families`、`speech_binding_revisions`、`speech_publications`、
`speech_publication_heads` 与 `speech_publication_retirements`；启动逐表校验
SELECT 且拒绝任何写权限，未配置时列表返回 `503 model_binding_service_unavailable`。

公开读取面另需三个只读非 owner 身份，`compose.yaml` 的 api 环境均以空默认透传，
未配置的既有部署仍可渲染并启动；配置了与声明权限面不符的角色则 API 启动失败。
`SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL` 使用 `model_catalog_reader_grants`：
`speech_publications`、仅已发布的 `speech_published_binding_revisions` 视图、
`speech_catalog_state`、`speech_publication_retirements` 与
`speech_publication_heads` 的 SELECT，不读草稿表，供 `GET /v1/models` 与
`GET /v1/models/{model}`；未配置或读取失败时返回 `503 model_catalogue_unavailable`。
`SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL` 使用 `artifact_reader_grants`：
`speech_artifacts` 与 `speech_workspaces` 的 SELECT，供
`GET /v1/nexus/artifacts/{artifact_id}` 及其内容下载，并继续使用上文的成对对象存储
键；未配置时返回 `503 artifact_service_unavailable`。
`SPEECHNEXUS_AUDIT_READER_DATABASE_URL` 使用 `audit_reader_grants`：仅
`speech_publication_lifecycle_events` 的 SELECT，供 `GET /v1/nexus/audit-events`；
该身份不得复用生命周期写入角色，否则读取路径会获得追加或重放证据的能力。未配置时
返回 `503 audit_service_unavailable`。
`SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL` 只授予 `engine_instances` 与
`speech_binding_revisions` 的 SELECT，供 `GET /v1/nexus/admin/model-instances` 与
`GET /v1/nexus/admin/model-instances/{instance_id}/generations/{generation}`；实例意图、
租约、命令、提交意图与停止证据不在该身份可读范围内，不得复用 owner 或任何写入角色。
该投影目前没有对应的仓库授权函数，按
[最小部署手册 §6](../docs/operations/compose-minimal.md#6-创建非-owner-运行角色与最小授权)
中的显式 `GRANT` 预置。未配置时两个端点返回
`503 model_instance_service_unavailable`。

隔离租约的运维释放另需独立写入身份
`SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL`：由 owner 运行
`quarantine_release_grants(schema, role)` 预置的非 owner 角色，持有 schema `USAGE`
（无 `CREATE`）与九张关系的列级 `SELECT`/`UPDATE`/`INSERT`。它在同一个事务里写
`execution_stop_evidence` 运维证据、关闭唯一提交意图、归还 `workspace_quotas`、
`host_nodes`、`engine_instances`、`admission_leases` 的容量，并追加
`admission_quarantine_releases` 账本；其中只有后两张表有 `INSERT`，账本表没有
`UPDATE`，因为 append-only 触发器负责不可改写。该身份不得复用迁移 owner、`sn_api`
运行时角色或任何 gateway/worker 写入身份。启动时逐列比对实际 ACL 与声明面：多一列、
少一列、schema 内任何序列的 `USAGE`/`SELECT`/`UPDATE`、append-only 触发器缺失或未启用
都会让 API 启动失败并报 `Quarantine release startup rejected`。`compose.yaml` 的 api
环境以空默认透传本变量，未配置的既有部署仍可渲染并启动，此时
`POST /v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases` 返回
`503 quarantine_release_unavailable`（`retryable: true`，须先确认该键是否配置，不要盲目
轮询）。已配置但运行期授权被回收时返回 `503 quarantine_release_privilege_missing`，迁移
未到位时返回 `503 quarantine_release_schema_not_migrated`，其余存储拒绝返回
`503 quarantine_release_failed`；这四种 5xx 都不表示证据冲突，只有真实的状态不匹配才返回
`409 quarantine_release_conflict`。

公开 Job 提交与读取默认关闭。应用 `0017_job_submissions` 并为非 owner 角色授予
`speech_jobs` / `speech_job_events` / `speech_job_submissions` 的最小写入权限及发布事实
只读权限后，设置 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`；启动只验证权限，不执行迁移
或 grant。未配置时 `/v1/nexus/jobs` 返回既有错误包封的 `503 job_unavailable`，不会
伪造 pending Job。

应用 `0053_voice_draft_publication` 后，Job writer 还必须只读
`speech_voice_draft_publications`：这是发布后草稿读投影的事实来源（`draft_id`、草稿
revision、`voice_id`、voice revision、`validation_job_id`）。视图由迁移 owner 持有并以
`security_invoker = false` 执行，因此该身份只读视图，仍不持有
`speech_voice_capability_evidence` 或 `speech_voice_revisions` 的任何权限；
`voice_draft_grants` 已包含这条授权。缺少它时已发布的草稿无法投影出契约要求的字段，
启动按声明读面直接失败，不会带着半个读模型上线。

工作区写操作默认关闭。执行 `0049_workspace_commands` 后，由 owner 运行
`workspace_writer_grants(schema, role)` 并为非 owner 角色设置
`SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL`；不得复用 Compose bootstrap owner 或资产
运行时角色。读取继续使用资产运行时角色，该角色对 `speech_workspaces` 只被授予
`UPDATE (metadata_revision)`，因而无法改写显示名或状态；写操作使用本配置的独立身份。
启动按 schema 校验其权限面：仅 `speech_workspaces` 与 `speech_workspace_commands`
两张声明表，`speech_workspaces` 只接受列级 `INSERT (workspace_id, display_name,
workspace_state, created_at_us)` 与 `UPDATE (display_name, workspace_state,
metadata_revision)`，命令表只接受列级 `INSERT`，表级 `INSERT`/`UPDATE` 与任何
`DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER` 一律拒绝；校验不通过时进程启动失败，
不执行迁移或 grant。同一 `Idempotency-Key` 的相同请求返回首次接受的身份，不同请求体、
目标或操作返回 `409 idempotency_conflict`，记录的接受结果已被删除时返回 503 而不重建；
`expected_metadata_revision` 与存储值不一致返回 `409 configuration_changed`。
`policy_id` 在尚无策略注册表的部署中返回 `422 workspace_policy_unsupported`。
未配置本变量时两个写操作返回 `503 workspace_service_unavailable`，读操作不受影响。
`compose.yaml` 的 api 环境以空默认透传本变量，未配置的既有部署仍可渲染并启动；
配置了与声明权限面不符的角色则 API 启动失败，请求不会带着错误身份上线。
进程内还要求资产运行时已装配（成对的 `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY` /
`_SECRET_KEY`，Compose 固定提供），只设置本变量而关闭资产运行时不会装配写端口。

声音发布还需要独立的 publisher 角色：应用 0047 后由 owner 运行
`voice_publication_grants(schema, role)`，设置 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL`；
它必须与 Job writer 指向同一数据库和服务器，但不得复用 Job writer 身份。启动按 schema
校验其完整权限面：只允许 `voice_publication_grants` 声明的 14 张表，白名单之外的表级或
列级权限与 schema `CREATE` 一律拒绝；表级与列级分开判定，列级授权不会因表级检查为 false
而漏检。新增同类校验时必须同时补真实角色拒绝用例。启动校验不通过或未配置时发布返回 503。

Artifact reader、binding writer、binding admin reader、public Job writer 与 workbench
runtime 的启动校验遵循同一条权限面规则：配置 schema 内声明表之外不得持有任何表级或列级
权限；可写表拒绝 `DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER`，表级 `INSERT` 与列级
`INSERT`/`UPDATE`/`REFERENCES` 分开判定；schema 需要 `USAGE` 且拒绝 `CREATE`。
列级判定一律使用 `has_column_privilege`/`has_any_column_privilege`，不以表级结果为代理。

public Job writer 的 15 张可写表只接受列级 `INSERT`：表级 `INSERT` 在任何声明表上都被
拒绝，列集必须与 `job_writer_columns.py` 中的声明完全一致，少一列、多一列或新增列都不
会自动扩权。拒绝信息点名权限类型与关系，并给出撤销授权或扩声明表的修复动作。

从旧版本升级时，若 Job writer 角色还持有表级 `INSERT`，必须先撤销再重新授予列级权限，
否则启动会按预期拒绝。对同一 schema 和角色执行：

```sql
REVOKE INSERT ON
  <schema>.coordination_jobs,
  <schema>.coordination_outbox,
  <schema>.admission_operations,
  <schema>.speech_job_intents,
  <schema>.speech_artifacts,
  <schema>.speech_job_cancellation_not_submitted,
  <schema>.speech_jobs,
  <schema>.speech_job_events,
  <schema>.speech_job_submissions,
  <schema>.speech_job_cancellations,
  <schema>.speech_job_cancellation_fences,
  <schema>.speech_job_cancellation_reconciliations,
  <schema>.speech_voice_drafts,
  <schema>.speech_voice_draft_revisions,
  <schema>.speech_voice_metadata_commands
FROM <job-writer-role>;

-- 然后对同一 Job writer 角色重跑以下 grant 函数（逐个）：
-- job_runtime_grants
-- job_dispatch_grants
-- public_job_grants
-- voice_draft_grants
-- public_job_submission_grants
-- job_cancellation_fence_grants
-- cancellation_not_submitted_grants
-- cancellation_reconciliation_grants
-- artifact_publisher_grants
--
-- public_job_admission_grants 属于独立的 admission 角色，不要应用到 Job writer：
-- 它只给 speech_job_events 4/8 列，并额外授予 admission_attempts / requests / leases。
```

独立记录角色使用 `voice_evidence_recorder_grants` 和
`SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL`，通过
`speechnexus-record-voice-evidence` 显式写入证据，或由
`speechnexus-voice-evidence-worker` 周期消费已提交的校验报告并在中断后自愈。该 worker
不在默认 Compose 面内，需要显式提供 recorder DSN、`SPEECHNEXUS_DATABASE_SCHEMA`（无默认
值，缺失即 exit 2）、报告 Artifact 所在对象存储的读取凭据（只需 GET，不需要写入或删除）
与轮询参数；recorder DSN 必须与 Job/Artifact 同一可写主库（指向 logical replica 时，
瞬时不一致可能被 `validation_report_mismatch` 判成永久）。启动会按
`voice_evidence_recorder_grants` 的列级形状拒绝更宽的身份，包括配置 schema 之外的表权限。退休角色需在
0047 后重跑 `binding_lifecycle_grants`，否则生命周期启动校验会拒绝旧授权。

发音集合使用三个独立的非 owner 身份。读取身份设置
`SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL`，仅授予
`speech_pronunciation_families`、`speech_pronunciation_revisions` 与
`speech_publications` 的 SELECT；草稿写入身份设置
`SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL`，由
`pronunciation_draft_grants(schema, role)` 授予草稿最小权限；原子发布身份设置
`SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL`，由
`pronunciation_publication_grants(schema, role)` 授予草稿表、发布账本、选中 head
和 `speech_catalog_state` 的列级权限。发布身份不得复用带 DELETE 的
`publication_grants`；启动会校验列级权限并拒绝更宽的角色。未配置发布身份时
新的 `publish: true` 请求（无已提交回执）返回 422
`pronunciation_publication_not_supported` 且不写入；已提交发布的重放仍以
200 从命令回执恢复，`publish: false` 仍可写草稿。只配置发布身份时草稿写入也可
用，因为其授权是草稿 writer 的超集。

asr/tts 的原子创建还需要独立的 admission 角色：先应用当前迁移，再用
`public_job_admission_grants(schema, role)` 授予列级读写（无 DELETE），设置
`SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`，并确保它与
`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 指向同一数据库和服务器；启动会比对
`current_database()` 与服务器地址，不同则拒绝启动。只配 writer 不配 admission 时，
创建返回 `503 job_unavailable`，读取/取消仍可用。

升级前必须确认已发布 binding 带有 `execution_defaults`。不要自动回填；先运行只读预检
`psql "$SPEECHNEXUS_DATABASE_URL" -f scripts/dev-env/check-public-job-execution-defaults.sql`，
对列出的 binding 重新发布并补齐对应任务的执行默认值；旧 binding 会让 asr/tts 创建
返回 `422 job_binding_incomplete`。

自定义声音 TTS 的内部命令在 `TtsEffectiveParameters` 追加可选 `reference_text`。该字段是
前向兼容：**只有 custom 命令包含它**，builtin 命令与旧版逐字一致，旧网关仍可处理 builtin。
未升级的网关对 custom 命令抛基类 `InvalidExecutionRequestError`；worker 只把
`FileCommandInvalidError` 当作确定性未外发拒绝，因此基类异常走未知路径并落
`ENGINE_TRANSPORT_UNKNOWN`，不会静默回退 builtin 或合成错误音频。部署必须先升级所有
execution gateway，再升级 backend/worker；回退时先停用自定义声音 TTS 入口或回退 backend，
不得保留“新 backend + 旧 gateway”的组合。

Release preflight C9 checks only the parsed Compose service set and the presence
of the gateway's private ASR/TTS configuration keys. It does not start these
containers; runtime and business evidence remain separate.

## Independent Workbench API (`workbench-api` profile)

`WEB_WORKBENCH_UPSTREAM` used to name a host this file never assembled. The
`workbench-api` profile is that unit:

```bash
docker compose --env-file deploy/compose.env \
  -f deploy/compose.yaml --profile workbench-api run --rm workbench-migrate
docker compose --env-file deploy/compose.env \
  -f deploy/compose.yaml --profile web --profile workbench-api up -d
```

`workbench-migrate` is `speechnexus-workbench-migrate`, one owner transaction that
either bootstraps a fresh workbench namespace (its fragments are fresh-only, so
they never modify one that exists) or verifies the recorded revision of an
existing one, then replays the two grant declarations and probes the resulting
surface. `workbench-api` is the real service behind the Web tier's
`/v1`/`/v2/workbench/*` proxy; its entrypoint is `deploy/workbench-startup-gate.py`
and it `exec`s
`python -m uvicorn speechnexus_backend.workbench.runtime:create_app_from_env --factory`.

The workbench owns its own PostgreSQL namespace
(`SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA`, default `workbench`), which must not be
`speech`: the unit never shares or writes the speech product schema. It holds two
dedicated non-owner roles and no third:

| Identity                                                              | Declaration          | Surface                                                                                                                                                                          |
| --------------------------------------------------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sn_workbench_runtime` (`SPEECHNEXUS_WORKBENCH_DATABASE_URL`)         | `runtime_grants_sql` | schema `USAGE`, `SELECT`/`INSERT` on the declared tables, column-level `UPDATE` on the declared columns; the ledger, the erasure receipts and the revision marker stay read-only |
| `sn_workbench_cleanup` (`SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL`) | `cleanup_grants_sql` | `DELETE` on business rows, `INSERT` on the ledger and erasure receipts, `UPDATE` on the two outbox columns                                                                       |

Neither identity may be the owner, and they may not be one identity: the cleanup
role carries the erasing authority, so folding it into the serving role would hand
deletion to the request path. At startup the gate runs the runtime's own
`check_database` and `check_cleanup_identity`, and both checks judge their identity
by the same two boundaries:

- no privileges on relations or sequences _outside_ the workbench schema, named
  relation by relation;
- no privileges reachable by `SET ROLE`, at any membership depth. An ordinary grant
  is visible through inheritance, but `GRANT wider TO role WITH INHERIT FALSE`
  confers nothing any privilege predicate over the login can see while
  `SET ROLE wider` still reaches it, so the scan judges every role the login can
  switch to and names the one the foreign grant is actually held by. A membership
  that reaches no relation outside the workbench schema stays legal — a group role is
  a normal deployment shape, and both checks settle any DSN `role=` switch back to
  the login identity, so the boundary is reachable _authority_, not membership.

Pointing either DSN at `sn_api` therefore fails closed instead of sharing the API's
wide identity — the serving DSN fails in `check_database`, the cleanup DSN in
`check_cleanup_identity`, and each names the foreign relation it found. An identity
that is the schema owner or otherwise privileged is refused by the same checks with
the generic "unavailable or unsafe" line, because "which flag" is not an operator
action there: the fix is to stop using that role. Both cases are exercised in
`tests/integration/test_startup_authority_561.py`.
Every gate line is `OK: <what is now true>` or `FAIL: <the named prerequisite>`;
a failure exits 1 and never starts the server.

The container publishes one loopback host port
(`SPEECHNEXUS_WORKBENCH_API_HOST_PORT`, default `8082`) and its healthcheck probes
`GET /v1/workbench/status`, which answers only after the real lifespan preflight
passed — the workbench has no unauthenticated `/healthz`. Browser requests arrive
through the Web tier, so `SPEECHNEXUS_WORKBENCH_ALLOWED_ORIGINS` is the published
Web origin (`http://127.0.0.1:<SPEECHNEXUS_WORKBENCH_HOST_PORT>`); the runtime
rejects a request whose `Host` or `Origin` is not allowed rather than serving it.

This unit assembles the workbench business backend: projects, manuscripts,
meetings, the teleprompter, books and chapters, and the deletion
preflight→confirmation→ledger path. It intentionally configures no speech base
URL, no object zone and no LLM, so `/v1/workbench/status` reports those
capabilities as `disabled` rather than guessing endpoints. Enabling them is a
deployment input: `SPEECHNEXUS_WORKBENCH_SPEECH_BASE_URL`, the four object-zone
keys and `SPEECHNEXUS_WORKBENCH_LLM_*`.

## Binding disable worker

See [the optional worker runbook](../docs/operations/compose-minimal.md#12-按需启用-binding-停用-worker)
for separate scanner and settlement identities, migration, grants and rollout.
The worker shares the gateway network namespace and calls its loopback address;
this is a single-host Compose topology, not a multi-node transport design. A
`disabled` response means selection is blocked and cleanup remains pending or
unknown; it does not establish that the worker has run, an engine stopped, or
capacity was released. The base topology does not start this worker.
