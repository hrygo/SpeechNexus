# SpeechNexus backend

The backend package owns the speech foundation, its administrative protocol
projection, and the optional workbench domain. It exposes operational health and
configured Asset upload/metadata/content routes with actual UTF-8 inspection.
The independent `speechnexus-asset-maintain` worker replays originals and
retention ledgers. Audio inspection, Artifact/Operation/receipt and other speech,
voice, job and workbench business behavior continue in their owned units.
Machine contracts alone do not prove those runtime capabilities.

The ModelBinding draft registry provides immutable create, original-key recovery
and fixed revision reads through PostgreSQL and an opt-in HTTP writer. Its
[storage and handoff](../docs/architecture/model-binding-drafts.md) describe migration
0010, explicit minimum-role provisioning and `SPEECHNEXUS_BINDING_*` settings.
Only POST and fixed GET are exposed; publication, activation/default selection
and model qualification remain separate work.

Run from the repository root:

```bash
source scripts/dev-env/activate.sh
uv run speechnexus-api
```

See [`docs/architecture/repository-layout.md`](../docs/architecture/repository-layout.md)
for module boundaries and [`contracts/README.md`](../contracts/README.md) for the
contract inventory and validation gates.

Asset configuration requires the current migration head, a non-owner runtime role, an immutable origin/bucket declaration and paired object keys; see [production assets](../docs/architecture/production-assets.md). No keys leaves business endpoints unavailable. Browser callers use explicit Host/Origin configuration and the same-origin proxy.
