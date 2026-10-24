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
stop recording does not return scheduler budgets. The default gateway process does
not yet wire this source or run a periodic recovery loop.
`abort_in_flight=true` is rejected until a qualified abort adapter exists.
See [the implementation and operating boundaries](../docs/architecture/execution-gateway.md).
