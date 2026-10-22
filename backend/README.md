# SpeechNexus backend

The backend package owns the speech foundation, its administrative protocol
projection, and the optional workbench domain. The current bootstrap exposes
only operational health endpoints. Speech, voice, job, and workbench business
contracts remain pending P0 and must not be inferred from empty package
directories.

Run from the repository root:

```bash
source scripts/dev-env/activate.sh
uv run speechnexus-api
```

See [`docs/architecture/repository-layout.md`](../docs/architecture/repository-layout.md)
for module boundaries and [`contracts/operations.openapi.yaml`](../contracts/operations.openapi.yaml)
for the operational contract.
