# Private deployment inputs

These files are syntactically complete examples, not approved deployment inputs.
Copy them to the ignored `deploy/.runtime/private/` directory, replace every
synthetic identity, digest, endpoint, bucket and model, then make each file
owner-only (`0600`, owned by `SPEECHNEXUS_RUNTIME_UID`).

The gateway examples use the fixed bootstrap MinIO address `172.28.0.10:9000`
because the file-speech S3 adapter rejects DNS names. If `172.28.0.0/24` is not
available on the host, change the Compose subnet and all private object
endpoints together.

The ASR `vad_model_file` must point to the approved Silero ONNX artifact, and
`vad_model_sha256` must be its real SHA-256. The zeros below are intentionally
invalid for a real execution.

`transcription-recipe.json` must use the exact approved runtime plan and
binding/manifest identities. `recipe_digest` is the RFC 8785 canonical JSON
SHA-256 with `recipe_digest` removed, as described in
[synchronous transcription](../../docs/architecture/synchronous-transcription.md).

| Source example | Required runtime name |
| --- | --- |
| `transcription-recipe.example.json` | `transcription-recipe.json` |
| `gateway-file-asr.example.json` | `gateway-file-asr.json` |
| `gateway-file-asr-endpoint.example.json` | `gateway-file-asr-endpoint.json` |
| `gateway-file-tts.example.json` | `gateway-file-tts.json` |
| `gateway-file-tts-endpoint.example.json` | `gateway-file-tts-endpoint.json` |
