# SpeechNexus node controller

This independently deployed process owns the node-scoped lifecycle of approved
runtime instances: it claims the `runtime-control` prepare/stop intents whose
`target_node_id` is its own node, refuses anything outside an approved template,
keeps instance generations and fencing tokens monotonic, retains peak
reservation until readiness is observed, and reports stop evidence only when a
container exit and its resource release were both verified.

The controller never receives business audio, transcript text, arbitrary images,
host mounts, or shell commands, and it does not own the scheduler's budget
authority: it reports durable intent and observation facts through
`contracts/internal/runtime-control.openapi.yaml` and leaves admission
accounting to the scheduler.

The controller includes the HTTP shell, a durable local SQLite receipt store,
and a restricted Docker adapter. Approved templates pin the image, arguments,
read-only artifacts, GPU request, container memory limit and readiness probe.
HTTP readiness accepts only 200 from a fixed container IP/path, without
environment proxies or redirects. A declared custom network is supplied by
deployment configuration; its name alone does not prove egress isolation.

The process starts one local observation loop by default. It pages unresolved
receipts in ID order and persists observations. Prepare uses the node's own
inspector; an already accepted stop resumes its pinned container through durable
target/exit/release checkpoints. Reads remain read-only. Lifecycle actions and
warmup inspections are serialized within the controller; shutdown joins the
active bounded inspection before closing its store and Docker client.
`SPEECHNEXUS_CONTROLLER_LOCAL_OBSERVER_ENABLED` defaults to true,
`SPEECHNEXUS_CONTROLLER_OBSERVATION_INTERVAL_SECONDS` to 2, and
`SPEECHNEXUS_CONTROLLER_OBSERVATION_BATCH_SIZE` to 50
(1–500). Container absence without stop proof keeps authoritative capacity held;
HTTP ready does not create the scheduler's qualification receipt.

`POST reconcile` compares caller assertions with persisted node facts for
diagnostics only. It never writes receipts/checkpoints or invokes the daemon,
even with complete-looking stop evidence. Its compatibility fencing token does
not fence resource-pool intents; the quarantine list is a diagnostic response,
not a persisted state change. Upgrade every serving node before relying on this
boundary, and query the original intent for authoritative observations.

The read-only `getVerifiedInstanceStop` endpoint returns an original successful
stop only when its full request and evidence match a local released checkpoint.
Legacy success receipts without a checkpoint are not certified. Missing proof
returns 404; contradictory or ambiguous proof returns 503, preserving unknown
occupancy. This query neither touches the daemon nor advances recovery. The
[gateway consumer](../docs/architecture/trusted-node-stop.md) verifies the complete
immutable binding and execution scope over configured TLS.

Production requires the same durable SQLite store for stop checkpoints. Schema 1
upgrades atomically to 2 without rewriting existing receipts; older controllers
reject schema 2. Restore a matching stopped-controller snapshot for code rollback,
never reset the version or discard evidence. The private format and failure
boundaries are specified in [Stop recovery](../docs/architecture/node-stop-recovery.md).

Template files must declare `container_memory_limit_bytes`; old files fail
startup rather than retaining an unlimited container. Docker Memory and
MemorySwap are set to the same fixed value, covered by the intent's host or
unified peak reservation. This is not proof of GPU allocation isolation or
qualification for a lower steady budget. Discovered containers whose resource
settings differ are quarantined and remain stoppable.

Native `vllm_api_key_v1` templates require explicit argv and bounded authenticated
warmup on `/v1/audio/transcriptions` or `/v1/audio/speech`. Both use the same
generation-scoped credential and actual image/argv/environment/identity checks.
Upgrade the controller before installing a TTS warmup template; older controllers
reject that path. Health-only readiness cannot enable this authentication mode.

Synthetic container/HTTP probes verify only the paths recorded in the
[controller design](../docs/architecture/node-controller.md). Real engine,
scheduler-budget, warmup, recovery and physical-cluster qualification require
their own evidence.
