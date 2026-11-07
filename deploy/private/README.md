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
[synchronous transcription](../../docs/operations/synchronous-transcription.md).

The TTS gateway requires `file_tts_v2`. Its `builtin_voice_binding` must pin the
public voice ID/revision, internal revision ID and native speaker from the same
authoritative immutable revision; the speaker must equal `tts_policy.voice`.
The mapping restricts execution and does not grant publication or qualification.
Stop the old worker before activating the replacement snapshot; v1 is rejected.
See [TTS deployment](../../docs/operations/file-tts-runtime.md).

The VoiceDesign gateway requires `file_voice_design_v1`, a `voice-creation` pool
target and the `VoiceDesign` engine task; it delivers 24 kHz mono WAV only. A
gateway serves at most one TTS variant: configuring both `gateway-file-tts.json`
and `gateway-file-voice-design.json` fails startup, and switching variants is a
stop-then-activate action. The engine sleep endpoint is not release evidence.
Readiness requires one real warmup synthesis, cached and run in the background
with its own `readiness` budget, so `/readyz` never blocks a health probe; only
languages with a verified vendor value are forwarded. See
[VoiceDesign template](../../engines/file-voice-design/README.md).

### 升级顺序（常驻 TTS 变体）

1. 给既有常驻 TTS 模板补 `variant_slot`（CustomVoice 与 VoiceDesign 取同一值，如
   `tts-variant`）；缺失时控制器在加载模板阶段拒绝，错误信息给出 `template_id`。
2. 核对 `tts_policy.language` 是否为有实测厂商值的 `zh/en/fr/de/it/ja/ko/pt/ru/es`
   （区域子标签按主语言归并）；`auto` 或其他取值在启动时拒绝。
3. 再升级控制器与网关；切换变体仍按「旧实例取得已核验 stop → 新变体 prepare」。

| Source example | Required runtime name |
| --- | --- |
| `transcription-recipe.example.json` | `transcription-recipe.json` |
| `gateway-file-asr.example.json` | `gateway-file-asr.json` |
| `gateway-file-asr-endpoint.example.json` | `gateway-file-asr-endpoint.json` |
| `gateway-file-tts.example.json` | `gateway-file-tts.json` |
| `gateway-file-tts-endpoint.example.json` | `gateway-file-tts-endpoint.json` |
| `gateway-file-voice-design.example.json` | `gateway-file-voice-design.json` |
| `gateway-file-voice-design-endpoint.example.json` | `gateway-file-voice-design-endpoint.json` |
