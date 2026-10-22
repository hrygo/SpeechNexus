# SpeechNexus

[简体中文](README.md)

SpeechNexus is an enterprise speech service foundation, primarily using vLLM / vLLM-Omni on Linux. Its backend provides declared OpenAI-compatible subsets for real-time and offline ASR/TTS, plus enhanced protocols such as voice creation. The WebUI has two surfaces: an **AdminUI** for deployment maintenance and a **personal speech workbench** for different speech tasks.

Current stage: **design and repository initialization**. No runnable service or WebUI is available yet; documented APIs, performance and capacity targets have not been implemented or validated.

## Product direction

- Deploy the foundation independently for SDKs, MCP and external applications, without requiring a WebUI or LLM.
- Add voice design, cloning, previews, validation and version publication, alongside presets, durable jobs, resource scheduling and diagnostics.
- Use the AdminUI for model services, presets, resource pools, jobs and operations; use the workbench for transcription, dubbing, voice creation, meetings, assistants, teleprompters, captions and audiobooks.
- **Do not build a user identity or authentication system in the initial scope.** Login, user directories, roles and multi-tenancy are not prerequisites. Deployment data is shared; a personal workbench does not imply private per-user storage.
- Define hardware-independent capabilities and capacity manifests, and qualify quality, concurrency, recovery and service targets on actual deployments.

## Design and roadmap

- [Product and architecture design (Chinese)](docs/speechnexus-product-architecture.md): compatible and enhanced protocols, voice creation, both WebUI surfaces, scenario presets, initial access boundaries and scheduling.
- [Confirmed product boundaries and identity scope (Chinese)](docs/decisions/0001-product-boundaries-and-authentication-scope.md).
- [Phased implementation plan (Chinese)](docs/plans/2026-10-22-speechnexus-implementation-plan.md): P0 contracts, P1 independent foundation, P2 both WebUI surfaces, P3 real-time applications, P4 audiobooks and P5 production qualification. Identity is a separate future initiative.
- [Design review record (Chinese)](docs/audits/2026-10-22-speechnexus-design-review.md): issue decisions, upstream evidence, documentation checks and remaining runtime validation.
- [Documentation index](docs/README.md).

SpeechNexus is an independent project inspired by the product and architecture experience of [SpeechRail](https://github.com/hrygo/SpeechRail). Any source reuse must be reviewed against the [reuse boundaries (Chinese)](docs/architecture/speechrail-reuse.md) and the original license.
