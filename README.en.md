# SpeechNexus

[简体中文](README.md)

SpeechNexus is an enterprise speech service foundation, primarily using vLLM / vLLM-Omni on Linux. Its backend provides declared OpenAI-compatible subsets for real-time and offline ASR/TTS, plus enhanced protocols such as voice creation. The WebUI has two surfaces: an **AdminUI** for deployment maintenance and a **personal speech workbench** for different speech tasks.

Current stage: **engineering bootstrap**. The repository now has buildable
FastAPI service and execution-gateway processes, two Web entry shells, and
PostgreSQL / object-storage readiness checks. Speech, voice, job, realtime, and
workbench business contracts remain pending P0; documented performance and
capacity targets have not been implemented or validated.

## Quick start

```bash
make dev-bootstrap
make verify
```

`make dev-bootstrap` initializes the isolated database, object store, locked
Python / Node dependencies, and browser. On a machine whose development services
already exist, run `make install && make verify`. Start the foundation and Web
entries in separate terminals with `make api-dev`, `make gateway-dev`, and
`make web-dev`.

`make stack-config` validates the Compose development topology; see the
[deployment bootstrap](deploy/README.md). The available endpoints are
`/healthz`, `/readyz`, and the two interface shells, not speech business APIs.

## Engineering baseline

- Python 3.14 uses a `uv` workspace with separate `src/` packages, strict typing,
  enforced dependency direction, and independent entry points for the backend
  and execution gateway.
- React 19, TypeScript 6, and Vite 8 use a `pnpm` workspace. AdminUI, workbench,
  and shared components build separately with centralized dependency versions.
- Public contracts live in `contracts/`. Only operational health is currently
  frozen; unreviewed speech protocols are not invented ahead of P0.
- Unit, contract, fake, and integration test boundaries are separated. CI checks
  Python, Web, and Compose configuration.
- The initial scope has no login, user directory, RBAC, or multi-tenancy. The UI
  and documentation state that data is shared within the deployment.

## Product direction

- Deploy the foundation independently for SDKs, MCP and external applications, without requiring a WebUI or LLM.
- Add voice design, cloning, previews, validation and version publication, alongside presets, durable jobs, resource scheduling and diagnostics.
- Use the AdminUI for model services, presets, resource pools, jobs and operations; use the workbench for transcription, dubbing, voice creation, meetings, assistants, teleprompters, captions and audiobooks.
- **Do not build a user identity or authentication system in the initial scope.** Login, user directories, roles and multi-tenancy are not prerequisites. Deployment data is shared; a personal workbench does not imply private per-user storage.
- Define hardware-independent capabilities and capacity manifests, and qualify quality, concurrency, recovery and service targets on actual deployments.

## Design and roadmap

- [Product and architecture design (Chinese)](docs/speechnexus-product-architecture.md): compatible and enhanced protocols, voice creation, both WebUI surfaces, scenario presets, initial access boundaries and scheduling.
- [Repository layout and dependency boundaries (Chinese)](docs/architecture/repository-layout.md): workspace layout, dependency direction, contract workflow, test layers and validation commands.
- [Confirmed product boundaries and identity scope (Chinese)](docs/decisions/0001-product-boundaries-and-authentication-scope.md).
- [Phased implementation plan (Chinese)](docs/plans/2026-10-22-speechnexus-implementation-plan.md): P0 contracts, P1 independent foundation, P2 both WebUI surfaces, P3 real-time applications, P4 audiobooks and P5 production qualification. Identity is a separate future initiative.
- [Design review record (Chinese)](docs/audits/2026-10-22-speechnexus-design-review.md): issue decisions, upstream evidence, documentation checks and remaining runtime validation.
- [Development environment (Chinese)](docs/development-environment.md): local toolchain, isolated services, media and headless browser checks.
- [Development environment readiness review (Chinese)](docs/audits/2026-10-22-development-environment-readiness.md): verified capabilities, isolation boundary and remaining inference gaps.
- [Deployment bootstrap](deploy/README.md): Compose development topology, ports, configuration authority and boundaries.
- [Documentation index](docs/README.md).

SpeechNexus is an independent project inspired by the product and architecture experience of [SpeechRail](https://github.com/hrygo/SpeechRail). Any source reuse must be reviewed against the [reuse boundaries (Chinese)](docs/architecture/speechrail-reuse.md) and the original license.
