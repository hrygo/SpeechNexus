# SpeechNexus

[简体中文](README.md)

SpeechNexus targets an independently deployable enterprise speech service on Linux, primarily using vLLM / vLLM-Omni. It aims to provide declared OpenAI-compatible subsets for real-time and offline ASR/TTS, plus enhanced protocols such as voice creation. The WebUI has two surfaces: an **AdminUI** for deployment maintenance and a **personal speech workbench** for different speech tasks.

Current stage: **engineering bootstrap established; P0 business contracts pending**.

| Scope                               | Current status                                                                                                                                                                                                      |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backend and execution gateway       | Independent FastAPI entry points, validated configuration, request correlation IDs, `/healthz`, and `/readyz` are implemented. The API probes PostgreSQL / object storage; the gateway probes its control database. |
| AdminUI / workbench                 | Separately buildable and deployable interface shells are implemented; business workflows remain pending.                                                                                                            |
| Machine contracts                   | Only [operational health](contracts/operations.openapi.yaml) is frozen. Speech, voice, Job, Realtime, workbench, and internal execution / runtime-control contracts remain P0 deliverables.                         |
| Models and deployment qualification | Preferred models and loading policies are recorded in an ADR. Real inference, speech quality, mixed load, cancellation, long-running stability, and production qualification remain unverified.                     |

Health checks and toolchain smoke tests establish only their stated scope.
They do not establish speech capability, performance, or capacity.

## Quick start

Run commands from the repository root. The current `dev-bootstrap` is pinned
to Linux ARM64 with APT and user-level systemd. It installs the locked
toolchain and Chromium, then initializes and starts isolated PostgreSQL / MinIO.
For other hosts, consult the [deployment bootstrap](deploy/README.md) for the
Compose development topology; the native bootstrap is specific to this host.

```bash
make dev-bootstrap
make verify
```

The toolchain uses Python 3.14 ([.python-version](.python-version)),
Node 26 ([.nvmrc](.nvmrc)), and pnpm 12 ([package.json](package.json)).
For an initialized native environment, run `make install` to install locked
dependencies, `make dev-up` to start services, and `make dev-status` to inspect
them. `make verify` also requires the Docker Compose CLI for configuration
validation.

Start each application command in a separate terminal:

| Command            | Native development entry                                             |
| ------------------ | -------------------------------------------------------------------- |
| `make api-dev`     | Speech API shell: `http://127.0.0.1:8000`                            |
| `make gateway-dev` | Execution gateway shell: `http://127.0.0.1:8010`                     |
| `make web-dev`     | AdminUI: `http://localhost:5173`; workbench: `http://localhost:5174` |

Once the services are running, check the API's operational endpoints.
The gateway uses the same paths on port `8010`:

```bash
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

Native PostgreSQL / MinIO use `127.0.0.1:55432` / `127.0.0.1:59000`.
Data and credentials stay under the Git-ignored `.runtime/` directory.
Compose uses the same database and object-storage host ports, so run only
one topology at a time. Its Web ports are `8080` / `8081`.
`make stack-config` validates configuration only. See the
[development environment (Chinese)](docs/development-environment.md) and
[deployment bootstrap](deploy/README.md) for details.

## Engineering baseline

- Python 3.14 uses a `uv` workspace with separate `src/` packages, strict typing,
  enforced dependency direction, and independent entry points for the backend
  and execution gateway. `uv.lock` and `pnpm-lock.yaml` pin dependencies.
- React 19, TypeScript 6, and Vite 8 use a `pnpm` workspace. AdminUI, workbench,
  and shared components build separately with centralized dependency versions.
- Public contracts live in `contracts/`. Only operational health is currently
  frozen; unreviewed speech protocols are not invented ahead of P0.
- Unit, contract, fake, and integration test boundaries are separated; a directory
  does not establish business test coverage. `make verify` runs the complete native
  gate; CI checks Python, Web, and Compose configuration separately. See the
  [verification script](scripts/dev-env/verify.sh) and
  [CI configuration](.github/workflows/ci.yml) for the actual steps.
- The initial scope has no login, user directory, RBAC, or multi-tenancy. The UI
  and documentation state that data is shared within the deployment.

```text
backend/             Speech foundation and optional workbench modules
execution-gateway/   Independent execution gateway
web/                 admin, workbench, and shared workspace members
contracts/           Public and internal machine-contract entry
deploy/              Development Compose, images, and configuration templates
tests/               Unit, contract, fake, and integration tests
scripts/dev-env/     Isolated environment bootstrap, launch, and verification
docs/                Product, architecture, ADRs, plans, and reviews
```

See [AGENTS.md (Chinese)](AGENTS.md) for collaboration rules and
[repository layout and dependency boundaries (Chinese)](docs/architecture/repository-layout.md)
for module boundaries and minimum validation requirements.

## Product direction

- Deploy the foundation independently for SDKs, MCP and external applications, without requiring a WebUI or LLM.
- Add voice design, cloning, previews, validation and version publication, alongside presets, durable jobs, resource scheduling and diagnostics.
- Use the AdminUI for model services, presets, resource pools, jobs and operations; use the workbench for transcription, dubbing, voice creation, meetings, assistants, teleprompters, captions and audiobooks.
- **Do not build a user identity or authentication system in the initial scope.** Login, user directories, roles and multi-tenancy are not prerequisites. Deployment data is shared; a personal workbench does not imply private per-user storage.
- Run speech inference locally. Optional workbench LLM integration is disabled by default and accepts only deployment-approved local / intranet services. Audio, transcripts, manuscripts, voice references, and derived content must stay within the deployment network, with no public-cloud fallback. Complete egress restrictions and LLM integration remain unimplemented.
- Define hardware-independent capabilities and capacity manifests, and qualify quality, concurrency, recovery and service targets on actual deployments.

The preferred validation matrix includes Qwen3-ASR, Qwen3-TTS
CustomVoice / VoiceDesign / Base, Silero VAD, Qwen3-ForcedAligner, and
Nemotron-3-Diarization. Enabled realtime bindings are preloaded and resident;
VoiceDesign loads on demand. Designed voices require Base reuse validation
before explicit publication. See
[ADR 0002 (Chinese)](docs/decisions/0002-speech-models-engines-and-loading.md)
for model roles, auxiliary runtimes, resource budgets, and acceptance requirements.
These are accepted design choices; adapters and real inference validation remain pending.

## Design and roadmap

- [Product and architecture design (Chinese)](docs/speechnexus-product-architecture.md): compatible and enhanced protocols, voice creation, both WebUI surfaces, scenario presets, initial access boundaries and scheduling.
- [Technical blueprint (Chinese)](docs/architecture/technical-blueprint.md): internal execution / runtime-control protocols, transactions and migrations, contract type generation, Web state, observability, release, and implementation handoff.
- [Repository layout and dependency boundaries (Chinese)](docs/architecture/repository-layout.md): workspace layout, dependency direction, contract workflow, test layers and validation commands.
- [Confirmed product boundaries and identity scope (Chinese)](docs/decisions/0001-product-boundaries-and-authentication-scope.md).
- [Speech models, inference engines, and loading policies (Chinese)](docs/decisions/0002-speech-models-engines-and-loading.md).
- [Workbench LLM and content network boundaries (Chinese)](docs/decisions/0003-workbench-llm-network-boundary.md).
- [Phased implementation plan (Chinese)](docs/plans/2026-10-22-speechnexus-implementation-plan.md): P0 contracts, P1 independent foundation, P2 both WebUI surfaces, P3 real-time applications, P4 audiobooks and P5 production qualification. Identity is a separate future initiative.
- [Design review record (Chinese)](docs/audits/2026-10-22-speechnexus-design-review.md): issue decisions, upstream evidence, documentation checks and remaining runtime validation.
- [Development environment (Chinese)](docs/development-environment.md): local toolchain, isolated services, media and headless browser checks.
- [Development environment readiness review (Chinese)](docs/audits/2026-10-22-development-environment-readiness.md): verified capabilities, isolation boundary and remaining inference gaps.
- [Deployment bootstrap](deploy/README.md): Compose development topology, ports, configuration authority and boundaries.
- [Documentation index](docs/README.md).

SpeechNexus is an independent project inspired by the product and architecture experience of [SpeechRail](https://github.com/hrygo/SpeechRail). Any source reuse must be reviewed against the [reuse boundaries (Chinese)](docs/architecture/speechrail-reuse.md) and the original license.
