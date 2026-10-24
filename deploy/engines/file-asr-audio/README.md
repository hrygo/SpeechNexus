# File ASR audio preparation runtime

This optional environment composes PyAV 19.0.2 with the pinned
[CPU VAD dependencies](../cpu-vad/README.md). NumPy and ONNX Runtime share one
resolved lock. It is not an enabled Compose service or a qualified runtime template.
Ordinary gateway startup loads neither the media libraries nor the CPU model.

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
