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
HTTP submit/abort/drain, real ASR/TTS dispatch, public VAD integration and physical
cluster qualification remain pending.
`abort_in_flight=true` is rejected until a qualified abort adapter exists.
See [the implementation and operating boundaries](../docs/architecture/execution-gateway.md).
