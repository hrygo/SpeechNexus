# File ASR audio preparation runtime

This optional environment composes PyAV 19.0.2 and HTTPX 0.28.1 with the pinned
[CPU VAD dependencies](../cpu-vad/README.md). NumPy and ONNX Runtime share one
resolved lock. It is not an enabled Compose service or a qualified runtime template.
Ordinary gateway startup loads neither the media libraries nor the CPU model.

The file-ASR 1.7B model, CPU VAD and repository runtime locks are enumerated in
[artifact-pin.json](artifact-pin.json). Verify local files without network access,
Docker, GPU use or model loading:

```bash
uv run python scripts/dev-env/verify-file-asr-artifacts.py \
  --model-root "$SPEECHNEXUS_ASR_MODEL_ROOT" \
  --vad-file "$SPEECHNEXUS_VAD_MODEL_FILE"
```

Its JSON `artifact_set` and `engine` reuse the existing ModelBinding contract.
The result verifies a local snapshot against a trusted pin; it does not register
or publish a binding, inspect a running image, grant capacity qualification, or
prove an instance/generation or stop. See the
[binding and parameter handoff](../../../docs/architecture/file-asr-binding-handoff.md)
for provenance, supported parameters, limits and remaining integration work.

To emit the existing registry's complete `BindingDefaultsCreateRequest`, add
`--emit-binding-request --binding-id "$SPEECHNEXUS_ASR_BINDING_ID"
--logical-model "$SPEECHNEXUS_ASR_LOGICAL_MODEL"
--capacity-manifest-id "$SPEECHNEXUS_ASR_CAPACITY_MANIFEST_ID"`.
The caller must supply all three references; the tool verifies artifacts and
validates the request through the actual producer's pure preparation boundary,
without HTTP or database writes. The versioned
[binding-definition.json](binding-definition.json) supplies supported parameters,
the existing `asr_batch` recipe and explicit snapshot defaults. CPU decoding and
VAD run inside that file consumer; the complete capacity manifest must still
cover their resources. Its `production` tier is a requested target, not a
publication or capacity qualification. Revision/snapshot identities are assigned
by the registry. Prompt/temperature and deployment limits remain governed by
their existing complete-command/configuration contracts.

Recompile after an intentional dependency change:

```bash
uv pip compile deploy/engines/file-asr-audio/requirements.in --generate-hashes \
  --output-file deploy/engines/file-asr-audio/requirements.txt
```

Install in a dedicated environment with hash enforcement:

```bash
uv pip install --require-hashes -r deploy/engines/file-asr-audio/requirements.txt
```

The repository's application packages must also be installed in that environment.
The development shortcut below provides them from the existing workspace and adds
vendor packages in an ephemeral environment; it does not change `uv.lock` and is
not a substitute for hash-enforced deployment installation.

```bash
uv run --no-sync --with-requirements deploy/engines/file-asr-audio/requirements.txt \
  python scripts/dev-env/audio-preparation-probe.py \
  --model "$SPEECHNEXUS_VAD_MODEL_FILE" \
  --model-sha256 1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3 \
  --corpus-root "$SPEECHNEXUS_AUDIO_CORPUS_ROOT" \
  --report .runtime/probes/b03-audio/report.json
```

The optional `--corpus-root` must reference the authorized local corpus directory
with `corpus-files.tsv` (`path`, `bytes`, `sha256`). The probe verifies one complete
source WAV from each of the indexed AISHELL-4 and AliMeeting-derived groups, then
creates a temporary prefix of at most 20 seconds / 8 MiB for each. It fails if either
group is absent or verification fails. Omit this option for synthetic-only checks.
It also requires `espeak-ng` to generate a fixed synthetic speech sample.

Only aggregate timings, formats, sample counts and digests enter the report.
The source selection paths stay in `private-corpus-selection.json` beside the ignored
local report. Temporary audio is deleted when the process exits normally or raises.
No transcript/annotation is read, no ASR request is sent, and no GPU is used.
These checks do not establish recognition quality or a production memory budget.

See the [native API and limits](../../../docs/architecture/file-asr-audio.md).
The CPU model's MIT notice is retained in the VAD directory. This directory pins
runtime packages, not the project's overall model/runtime redistribution decision;
wheel codec/library configuration and licenses remain part of deployment review.

The [native recognition adapter](../../../docs/architecture/file-asr-recognition.md)
uses this same optional environment. The controller probe can exercise it with
explicitly authorized local recordings:

```bash
uv run --no-sync --with-requirements deploy/engines/file-asr-audio/requirements.txt \
  python scripts/dev-env/asr-controller-probe.py \
  --templates-file "$SPEECHNEXUS_APPROVED_ASR_TEMPLATES" \
  --template-id "$SPEECHNEXUS_ASR_TEMPLATE_ID" \
  --served-model-name "$SPEECHNEXUS_ASR_MODEL_ALIAS" \
  --file-asr-vad-model "$SPEECHNEXUS_VAD_MODEL_FILE" \
  --file-asr-vad-sha256 1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3 \
  --corpus-root "$SPEECHNEXUS_AUDIO_CORPUS_ROOT" \
  --file-asr-repeat-corpus \
  --evidence-directory .runtime/probes/file-asr
```

The approved template must use a pre-created `Internal=true` Docker bridge with
no host ports, a fixed local image/model and container memory limits. This mode
requires `--no-enable-log-requests`, `--no-enable-log-outputs` and
`--disable-uvicorn-access-log` in the template command. The measured vLLM version
supports these flags; other images require their own verification.
The optional repeat flag runs the exact same excerpts again, marking `repeat_of`;
it does not add independent samples. Remove only the empty network owned by this run
after both model probes exit. Controller stop evidence remains distinct from request
completion and Scheduler budget release.

The warmup readiness gate (`http_warmup_ok`) can be exercised against the same
image with a fixed 20-second synthetic window:

```bash
uv run python scripts/dev-env/asr-warmup-probe.py \
  --templates-file "$SPEECHNEXUS_APPROVED_ASR_TEMPLATES" \
  --template-id "$SPEECHNEXUS_ASR_TEMPLATE_ID" \
  --served-model-name "$SPEECHNEXUS_ASR_MODEL_ALIAS" \
  --evidence-directory .runtime/probes/b02-warmup \
  --window-seconds 20 --attempts 2
```

It creates and removes its own `--internal` network, records the
`warming_up → ready` sequence and each attempt's duration, and never stores the
audio or a transcript in the repository. Use `--attempts 2` to expose the first
long-window cost next to the immediate repeat.
