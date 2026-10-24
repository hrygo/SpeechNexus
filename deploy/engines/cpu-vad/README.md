# CPU VAD model runtime

This directory pins optional vendor dependencies independently of the application
workspace. It is not an enabled Compose service or a qualified deployment template.
The gateway loads neither these libraries nor model files during ordinary startup.

The adapter is `speechnexus_gateway.infrastructure.silero_onnx.SileroOnnxCpuModel`.
It requires an explicitly configured local model and expected SHA-256.
The consumer API and remaining integration boundaries are documented in
[CPU VAD](../../../docs/architecture/cpu-vad.md).

Recompile the complete dependency lock after an intentional version change:

```bash
uv pip compile deploy/engines/cpu-vad/requirements.in --generate-hashes \
  --output-file deploy/engines/cpu-vad/requirements.txt
```

An isolated model environment can install the generated requirements with
`uv pip install --require-hashes -r deploy/engines/cpu-vad/requirements.txt`.
The measured host supports Python 3.14 / Linux ARM64; other targets need their
own runtime and model qualification. Application dependencies remain separate.

The optional local synthetic probe uses the repository's existing media tools:

```bash
uv run --no-sync --with-requirements deploy/engines/cpu-vad/requirements.txt \
  python scripts/dev-env/vad-probe.py \
  --model "$SPEECHNEXUS_VAD_MODEL_FILE" \
  --model-sha256 1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3 \
  --report .runtime/probes/b06-vad/report.json
```

`--with-requirements` keeps vendor packages in an ephemeral environment; it does not
install them in the project's normal environment or modify `uv.lock`. For deployment
installation use the hash-enforcing command above, not this development probe shortcut.

Model provenance: `snakers4/silero-vad` commit
`bfdc0193023f121ea5b3cc7b176dbed570a68a59`,
`src/silero_vad/data/silero_vad.onnx`. Its MIT license is retained in
[SILERO_LICENSE](SILERO_LICENSE). The upstream `src/silero_vad/utils_vad.py`
documents the 16 kHz / 512 sample input and recurrent/context tensor protocol.
The SpeechNexus adapter and segmentation are independent implementations; no
SpeechRail platform code is copied. Runtime libraries retain their own licenses;
this notice does not replace the project's overall model/runtime license decision.
