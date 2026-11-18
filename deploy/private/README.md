# Private deployment inputs

These files are syntactically complete examples, not approved deployment inputs.
Copy them to the ignored `deploy/.runtime/private/` directory, replace every
synthetic identity, digest, endpoint, bucket and model, then make each file
owner-only (`0600`, owned by `SPEECHNEXUS_RUNTIME_UID`).

The gateway examples pin the bootstrap MinIO address `172.28.0.10:9000` because
the file-speech S3 adapter rejects DNS names. That literal is the default of
`SPEECHNEXUS_MINIO_STATIC_IP`, the 10th host of the default Compose subnet
`SPEECHNEXUS_COMPOSE_SUBNET=172.28.0.0/24`; the committed examples always keep the
default. A host that already uses `172.28.0.0/24` sets both variables in
`deploy/compose.env`, and rewrites `objects.endpoint` in every copied JSON to the
address `python3 deploy/compose-network.py <subnet>` prints. The runbook step is
`docs/operations/compose-minimal.md` §1.1.

The bucket keys play different roles, and one equality is mandatory:
`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` (the synchronous upload bucket) must name
the same bucket as `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` (the Job input asset
bucket); the staging/result bucket may name a different one. A lane that declares
`objects.input_bucket` (`file_asr_v1`, `file_voice_clone_v1`) reads the **Job
input asset bucket**, so it must equal `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` — the
bucket `/v1/nexus/assets` writes to and admission copies into the command's
`storage.storage_bucket`.
`objects.result_bucket` must equal `SPEECHNEXUS_EXECUTION_RESULT_BUCKET`.
`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` is the synchronous-transcription upload
bucket; because the gateway exposes one `objects.input_bucket` per lane and
serves both paths from it, this key must also equal
`SPEECHNEXUS_OBJECT_STORAGE_BUCKET`. Neither mismatch fails startup: the gateway
rejects before any engine call with `execution_input_scope_mismatch` — the first
Job's input is quarantined, and every synchronous request answers
`503 backend_unavailable` (#972/DEL-539). `tests/ci/test_reference_deploy_job_prereqs.py`
and `tests/ci/test_sync_transcription_bucket_scope.py` keep the committed
examples and the Compose defaults on these values.

The ASR `vad_model_file` must point to the approved Silero ONNX artifact, and
`vad_model_sha256` must be its real SHA-256. The zeros below are intentionally
invalid for a real execution.

`speech-recipe.json` is the synchronous-TTS allocation input behind
`SPEECHNEXUS_SPEECH_RECIPE_FILE`. It has no `asr` block and no
`binding_publication_id`: TTS publication facts are read inside the admission
transaction, so the recipe carries only the binding revision, manifest, policy
revision and runtime plan. `recipe_digest` is the same RFC 8785 canonical JSON
SHA-256 with `recipe_digest` removed. The replacement list is the same as for the
transcription recipe, plus these TTS-specific ones: `model_id` is the public
model — the binding id a caller sends as `model`, which admission compares with
the plan's `model_id` — so it must be the published binding's public name and
never the gateway's `served_model`; `served_model` is the engine's own model
name, sent to the engine as the request's `model`, and
`check-tts-assembly.py` requires it to be a non-empty identifier that differs
from the public model name (the engine name in `model_id` is DEL-538's
first-request `503` `stage=recipe_mismatch`; the public name in `served_model`
passes admission and is rejected later by the engine as an unknown model). The
rule is deliberate: a deployment that sets `--served-model-name` to the public
model name is red too. `runtime_plan.binding` must
match the controlled endpoint's `target` (node/gateway/pool/instance/binding
revision), and `admission_expires_at_us` must still be in the future — an expired
recipe leaves synchronous TTS switched off rather than starting it. The committed
example holds no credential at all: the engine token lives in
`gateway-file-tts-endpoint.json`, and
`scripts/dev-env/check-tts-assembly.py` puts all three files through their own
production loaders and lists every field as「仍是样例值 / 不一致 / 本地检查通过 /
无法从文件判定」, plus a named list of the leaves only their loader checks. It rejects
an assembly that still carries the example's synthetic identities or its all-zero
token, names the gateway policy fields its loader refuses (`task`, `sample_rate_hz`,
`speed`, …), and keeps values with no local oracle (budgets, the real engine
address, the registration generation) undecidable instead of a pass. `exit 0` only
means the files were replaced and agree with each other. The preconditions a
replaced assembly still has to satisfy — a non-blank settlement DSN, TTS instance
registration, plan digests equal to the registered instance, `policy_revision_id`
matching the voice qualification, a published non-retired voice — are in
[deploy/README.md](../README.md) "Synchronous TTS assembly".

The TTS endpoint descriptor next to it is what makes those preconditions reachable:
`address` and `port` must be the real engine address reachable from the Compose
network, and `bearer_token` must be that engine's `VLLM_API_KEY`. The 64 zeros in
`gateway-file-tts-endpoint.example.json` are a shape placeholder only — used as
committed, the engine answers `401` on the first request. Nothing in the file
reaches the engine, so that `401` is not a startup error and no local checker can
decide it; only a real request proves the address, the port and the token.

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
gateway serves at most one TTS variant: configuring more than one of
`gateway-file-tts.json`, `gateway-file-voice-design.json` and
`gateway-file-voice-clone.json` fails startup, and switching variants is a
stop-then-activate action. The engine sleep endpoint is not release evidence.
Readiness requires one real warmup synthesis, cached and run in the background
with its own `readiness` budget, so `/readyz` never blocks a health probe; only
languages with a verified vendor value are forwarded. See
[VoiceDesign template](../../engines/file-voice-design/README.md).

The Base clone gateway requires `file_voice_clone_v1`, the same `voice-creation`
pool and the `Base` engine task. Reference audio is read from `input_bucket` under
the command's version-pinned object reference; `spool_directory` must already
exist, be owned by the runtime user and mode `0700`. The reference clip and its
transcript are validated before any engine call (mono 16-bit WAV at 16/24 kHz,
1–60 s, ≤8 MiB); ICL is the default and x-vector-only copying needs
`allow_x_vector_only: true`. `warmup.reference_audio_file` is a private `0600`
file holding an approved synthetic clip whose `reference_text` matches it word
for word; a mismatch fails startup. Copy the template's synthetic
`warmup-reference.wav` there, or replace both the clip and its transcript and
recompute the node template's warmup body digest. See
[Base clone template](../../engines/file-voice-clone/README.md).

### 升级顺序（常驻 TTS 变体）

1. 给既有常驻 TTS 模板补 `variant_slot`（CustomVoice、VoiceDesign 与 Base 取同一值，如
   `tts-variant`）；缺失时控制器在加载模板阶段拒绝，错误信息给出 `template_id`。
2. 核对 `tts_policy.language` 是否为有实测厂商值的 `zh/en/fr/de/it/ja/ko/pt/ru/es`
   （区域子标签按主语言归并）；`auto` 或其他取值在启动时拒绝。
3. 再升级控制器与网关；切换变体仍按「旧实例取得已核验 stop → 新变体 prepare」。

| Source example | Required runtime name |
| --- | --- |
| `transcription-recipe.example.json` | `transcription-recipe.json` |
| `speech-recipe.example.json` | `speech-recipe.json` |
| `gateway-file-asr.example.json` | `gateway-file-asr.json` |
| `gateway-file-asr-endpoint.example.json` | `gateway-file-asr-endpoint.json` |
| `gateway-file-tts.example.json` | `gateway-file-tts.json` |
| `gateway-file-tts-endpoint.example.json` | `gateway-file-tts-endpoint.json` |
| `gateway-file-voice-design.example.json` | `gateway-file-voice-design.json` |
| `gateway-file-voice-design-endpoint.example.json` | `gateway-file-voice-design-endpoint.json` |
| `gateway-file-voice-clone.example.json` | `gateway-file-voice-clone.json` |
| `gateway-file-voice-clone-endpoint.example.json` | `gateway-file-voice-clone-endpoint.json` |
