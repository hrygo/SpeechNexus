# SpeechNexus execution gateway

This independent deployment owns submission intents, ingress fencing and stop evidence.
Its default HTTP surface is `/healthz` and `/readyz`. Explicit control configuration
enables the authenticated gateway fencing-barrier route, backed by the production
PostgreSQL ledger and an exclusive host-local process lease.

Set `SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN`, `SPEECHNEXUS_GATEWAY_INGRESS_LOCK_DIRECTORY`,
and explicit `SPEECHNEXUS_GATEWAY_NODE_ID` / `SPEECHNEXUS_GATEWAY_GATEWAY_ID` together.
Use the migration-provisioned gateway database role. Candidates for the same identity
must share the same host-mounted lock directory; it must not carry business content.
Compose does not yet provision this control configuration.

The submit application/ledger and atomic stop facts are implemented. The
[CPU VAD native API](../docs/architecture/cpu-vad.md) now provides bounded
segmentation and an isolated-state ONNX CPU adapter, checked with synthetic audio.
The [file audio preparation API](../docs/architecture/file-asr-audio.md) adds bounded
codec/resampling and source-clock mapping into VAD; local checks include two
authorized recording excerpts without ASR dispatch or quality acceptance.
The [native file recognition API](../docs/architecture/file-asr-recognition.md) now
consumes VAD candidates through a fixed vLLM endpoint and verifies terminal identity,
finish reason and token usage. Both ASR models were exercised on authorized local inputs.
The authenticated submit, abort and instance-drain HTTP routes are served: without an engine
adapter the two engine routes refuse, and the drain reports the recorded in-flight work of one
instance generation without claiming a stop. Durable ASR/TTS dispatch, public VAD integration
and physical cluster qualification remain pending.
The [checkpoint-backed node source](../docs/architecture/trusted-node-stop.md)
can now supply the existing recovery sweep with whole-instance stop facts over
fixed, verified TLS. Missing or conflicting proof keeps execution unknown; gateway
stop recording does not return scheduler budgets. Set
`SPEECHNEXUS_GATEWAY_RECOVERY_CONFIG_FILE` (absolute private JSON path) and the independent
`SPEECHNEXUS_GATEWAY_RECOVERY_NODE_COMPONENT_TOKEN` together to start periodic recovery
automatically with the existing control configuration. Both absent preserves existing behavior;
partial or invalid configuration refuses startup. The
[configuration contract](../contracts/internal/gateway-recovery.schema.json) requires every
field explicitly: one private IP/TLS CA, approved immutable bindings, interval, batch size and
request/pass budgets. Credentials stay outside JSON. No DNS/proxy/redirect or execution dispatch
is introduced. The default driver uses the original read-only ledger query; engine observation
still requires an engine adapter.
Recovery passes now retain a process-local cursor over immutable execution IDs,
so an unresolved first page does not starve later work. Reuse one recovery service
across passes; an empty page wraps the next pass to the beginning. Reports classify
each execution once, including when a concurrent generated-usage write changes
its version while it remains unknown. No scheduling-only database write is made.
Shutdown stops starting rows, joins the active bounded call, then closes the database pool
and releases process ownership. The same cleanup job preserves that order under repeated
lifespan cancellation; cancellation propagates after cleanup. A pass budget stops starting
rows, not an already active call.
An alive configured driver participates in readiness; it does not prove node/model readiness.
`abort_in_flight=true` is rejected until a qualified abort adapter exists.
See [the implementation and operating boundaries](../docs/architecture/execution-gateway.md).
