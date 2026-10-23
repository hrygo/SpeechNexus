# SpeechNexus backend

The backend package owns the speech foundation, its administrative protocol
projection, and the optional workbench domain. The current bootstrap exposes
only operational health endpoints. Speech, voice, job, and workbench business
machine contracts are frozen under `contracts/`, but their routes and runtime
behavior are not implemented and must not be inferred from schema files.

Run from the repository root:

```bash
source scripts/dev-env/activate.sh
uv run speechnexus-api
```

See [`docs/architecture/repository-layout.md`](../docs/architecture/repository-layout.md)
for module boundaries and [`contracts/README.md`](../contracts/README.md) for the
contract inventory and validation gates.
