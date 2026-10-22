# SpeechNexus

[简体中文](README.md)

SpeechNexus is a speech platform for private enterprise deployments. It is designed to provide real-time and offline ASR/TTS through a WebUI, REST, WebSocket and MCP, supporting voice assistants, meetings, teleprompters, live captions and audiobook production.

Current stage: **design and repository initialization**. This repository contains a product and architecture proposal; it does not yet provide a runnable service or WebUI. Proposed APIs, performance targets and capacity figures have not been implemented or validated.

## Product direction

- Unify execution semantics, results and errors across real-time and offline recognition and synthesis.
- Adapt workflows through scenario presets bounded by model capabilities and enterprise policies.
- Provide enterprise identity, workspace permissions, data isolation, quotas, resource scheduling and audit.
- Use vLLM / vLLM-Omni on Linux as primary inference backends, with adapters separating model differences.
- Define hardware-independent capabilities and capacity manifests; qualify concurrency and service targets on each deployment.

## Design and roadmap

- [Product and architecture design (Chinese)](docs/specs/2026-10-22-speechnexus-product-architecture-design.md): unified APIs, real-time events, eight scenario presets, permissions, scheduling, durable jobs and workflows.
- [Phased implementation plan (Chinese)](docs/specs/2026-10-22-speechnexus-product-architecture-design.md#15-分阶段实施计划): P0 contract validation, P1 platform foundation, P2 core workbench, P3 real-time applications, P4 audiobooks and P5 production qualification.
- [Documentation index](docs/README.md).

SpeechNexus is an independent project inspired by the product and architecture experience of [SpeechRail](https://github.com/hrygo/SpeechRail). Any source reuse must be reviewed against the design's reuse matrix and the original license.
