# SpeechNexus · 声枢

![SpeechNexus · 声枢 — A self-hosted speech service and creation hub](web/shared/src/assets/brand-banner.svg)

**Turn recordings into text and text into speech, in your own environment.**

[简体中文](README.md) · [Use cases](#use-cases) · [Current availability](#current-availability) · [Quick start](#quick-start) · [Documentation](#documentation) · [Feedback](https://github.com/hrygo/SpeechNexus/issues)

[![CI](https://github.com/hrygo/SpeechNexus/actions/workflows/ci.yml/badge.svg)](https://github.com/hrygo/SpeechNexus/actions/workflows/ci.yml)
[![Inference: vLLM](https://img.shields.io/badge/Inference-vLLM-FF6B35)](https://github.com/vllm-project/vllm)
[![Inference: vLLM-Omni](https://img.shields.io/badge/Inference-vLLM--Omni-FF8F35)](https://github.com/vllm-project/vllm-omni)
[![ASR: Qwen3-ASR](https://img.shields.io/badge/ASR-Qwen3--ASR-615CED)](https://github.com/QwenLM/Qwen3-ASR)
[![TTS: Qwen3-TTS](https://img.shields.io/badge/TTS-Qwen3--TTS-8B5CF6)](https://github.com/QwenLM/Qwen3-TTS)
[![Docker Compose](https://img.shields.io/badge/deploy-Docker_Compose-2496ED?logo=docker&logoColor=white)](deploy/README.md)

SpeechNexus is a speech service and creation hub for your own Linux environment. It connects recognition, synthesis, and voice creation to everyday work: users transcribe, narrate, and organize content in a browser; developers integrate through APIs; operators manage models, resources, and tasks.

> **Early development: suitable for interface previews and development evaluation.** The default startup loads no speech models and cannot yet transcribe real recordings or generate speech. Some pages and service modules are connected and independently verified; complete speech workflows and production deployment still require integration and acceptance. The use cases below describe product goals. See [current availability](#current-availability) for delivered scope.

## Why SpeechNexus

- **Process content in your own environment.** Designed for local speech inference and intranet deployment, with audio, transcripts, manuscripts, and voice references kept within the deployment network boundary.
- **Create around a task.** The workbench brings capture, editing, previews, continuation, and export into one workflow.
- **Integrate a speech foundation into your applications.** The API is designed for independent use; speech services can run without the WebUI, workbench business logic, or an LLM.

Three entry points serve these needs: the **speech workbench** for creation, **AdminUI** for deployment operations, and the **speech API** for application integration. Their scope and delivery status are described below.

## Use cases

These are target workflows and are not all available yet.

| What you want to do | Target experience and output |
| --- | --- |
| Turn recordings into text | Upload audio, review recognized text, and export transcripts or captions, with speaker labels where supported by the model |
| Narrate a script | Enter text, choose a voice, generate and preview speech, then refine and export audio |
| Create a reusable voice | Design from a description or clone from reference samples; preview, validate, and explicitly publish before reuse |
| Capture meetings and live captions | View live text, mark key points, save records, and optionally generate minutes with an intranet LLM |
| Use a voice assistant or teleprompter | Interact through speech or follow your speaking position in a script |
| Produce an audiobook | Organize chapters and characters, assign voices, generate by chapter, redo sections, and export |

For example, the target transcription flow is **upload → choose language and preset → track progress → review text → export**. Narration starts with text and voice selection, followed by previews and targeted revisions.

See the [product design (Chinese)](docs/speechnexus-product-architecture.md) for the complete scope.

## Models and inference engines

The selected **Qwen3 speech model family** covers recognition, narration, and voice creation, with **vLLM / vLLM-Omni** serving inference.

| Selected model and engine | Intended experience |
| --- | --- |
| **Qwen3-ASR · vLLM** | 0.6B for the realtime recognition path; 1.7B for file transcription and full-segment review |
| **Qwen3-TTS · vLLM-Omni** | CustomVoice for preset voices, VoiceDesign for voices described in text, and Base for sample cloning and reuse of created voices |
| **Silero VAD · ONNX Runtime CPU** | Detect and segment speech to prepare useful recognition input |

These are selected integration paths: ASR, VAD, and CustomVoice TTS have partial runtime evidence; complete business integration, quality, recovery, and cluster qualification still need validation. Header badges identify technology choices and do not imply availability at default startup. See the [model and engine decision (Chinese)](docs/decisions/0002-speech-models-engines-and-loading.md) for responsibilities and acceptance requirements, and [file TTS (Chinese)](docs/architecture/file-tts.md) for component evidence.

## Current availability

Repository delivery summary as of **2026-10-25**. Independently verified modules do not imply complete availability in the default deployment.

| Area | Current state | Before you try it |
| --- | --- | --- |
| Default Compose startup | Two Web entry points, speech service, and infrastructure health checks | Interface preview; no model loading or complete speech workflow |
| File transcription, narration, and task pages | Public API consumer flows connected | Real speech execution, result generation, and complete deployment still need integration |
| Gateway file-ASR composition | Private configuration connects the durable worker to a fixed-generation endpoint, CPU VAD, and object storage | Candidate default entrypoint verified with real 1.7B/CPU VAD, isolated PG/MinIO, synthetic speech/silence, replay and restart; production admission, trusted budget release, publication and public workflow acceptance remain pending; see [configuration (Chinese)](docs/architecture/file-asr-runtime.md) |
| Projects, manuscripts, and meeting records | Pages and an independent workbench service have verification evidence | Separate configuration and database preparation required; not fully assembled in default Compose |
| Scenario preset drafts | Admin supports complete draft creation, fixed revision reads, explicit confirmation, and original-key recovery, verified through HTTP/PostgreSQL/browser tests | Explicit profile writer configuration required; publication, activation, and retirement are not connected |
| Live captions, live speech, and voice creation | Page consumers tested with synthetic services | Real services and models pending; creating a task does not publish a reusable voice |
| Asset upload and download | Available with explicit configuration; UTF-8 text handling verified | Object credentials disabled by default; audio inspection integration pending |
| Real models and production operation | Partial ASR, VAD, and CustomVoice TTS probes and modules verified | Complete recognition/synthesis, quality, capacity, recovery, and cluster operation still need acceptance |

Implementation details and evidence are linked from the [architecture documents (Chinese)](docs/architecture/README.md), [implementation tracking (Chinese)](docs/plans/2026-10-23-implementation-tracking.md), and [audit records (Chinese)](docs/audits/README.md).

## Quick start

Preview the current version with Docker Compose. You need Git, Docker, and the Compose plugin. These steps start a single-node preview environment.

### 1. Get the source and prepare configuration

```bash
git clone https://github.com/hrygo/SpeechNexus.git
cd SpeechNexus
cp deploy/compose.env.example deploy/compose.env
```

Edit `deploy/compose.env` to replace the example PostgreSQL and MinIO passwords. If this project's native development services are running, run `make dev-down` first to avoid port conflicts.

### 2. Build and start

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml up --build -d
```

Once services have started, open:

| Entry | Address | Current purpose |
| --- | --- | --- |
| Speech workbench | <http://127.0.0.1:8081> | Preview transcription, narration, and other task pages with capability status |
| AdminUI | <http://127.0.0.1:8080> | Preview deployment management and capability information |
| Speech service | <http://127.0.0.1:8000/healthz> | Check process liveness |

Check infrastructure readiness:

```bash
curl --fail http://127.0.0.1:8000/readyz
```

A successful `readyz` check means infrastructure dependencies are ready. It does not mean models are loaded or transcription and synthesis are available. If a page reports missing configuration or pending capabilities, consult [current availability](#current-availability); these startup steps alone do not enable real speech processing.

If startup fails, inspect service status and logs. Remove credentials and business content before sharing logs:

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml ps
docker compose --env-file deploy/compose.env -f deploy/compose.yaml logs --tail=100 api
```

### 3. Stop services

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml down
```

This preserves database and object-storage volumes. Published ports bind only to loopback by default. See the [deployment guide](deploy/README.md) for configuration, all ports, and requirements for additional modules.

## Integrate speech into an application

The interface design uses an **OpenAI Audio compatibility subset plus `nexus` extensions**: the standard subset covers recognition and synthesis, while extensions cover voices, durable tasks, and presets. Compatibility is limited to explicitly declared endpoints and events.

Once the relevant services are implemented and enabled, standard OpenAI SDKs can point `base_url` at the deployment's `/v1`. Public speech workflows remain in development; contracts and examples support integration work and do not imply that the default deployment implements every endpoint.

The configured independent workbench provides public downloads of committed copies and completed exports, with single byte ranges and integrity checks. The UI verifies content before handing it to the browser to save. See [workbench content downloads (Chinese)](docs/architecture/workbench-content-downloads.md) for limits and evidence scope. Complete export manifests, continuation, and real model quality acceptance remain in progress.

- [Speech API design (Chinese)](docs/architecture/speech-api.md): endpoints, events, and usage.
- [OpenAI compatibility boundaries (Chinese)](docs/architecture/openai-compatibility.md): supported subsets and extension rules.
- [Machine contracts](contracts/README.md): protocol definitions shared by implementations and consumers.

## Deployment and data boundaries

**The initial scope is a shared deployment within a trusted network, without login, role permissions, or tenant isolation.** Users of a deployment share its data. “Personal speech workbench” describes the experience and does not provide private user spaces. Workbench and AdminUI entry points do not establish permission boundaries; the operator controls access.

The product requires local speech inference, keeping business audio, transcripts, manuscripts, voice references, and derived content within the deployment network. LLM integration for assistants and minutes is optional, disabled by default, and restricted to deployment-approved local or intranet services, with no public-cloud fallback. Complete LLM integration and egress restrictions still require acceptance; see the [network boundary decision (Chinese)](docs/decisions/0003-workbench-llm-network-boundary.md).

When the asset service is explicitly enabled, new assets default to **7 days** of retention, overridable by deployment configuration or upload settings. Changes to defaults do not affect existing expiry times. See the [asset service guide (Chinese)](docs/architecture/production-assets.md) for storage, backup, and cleanup configuration.

## What's next

- Connect real file transcription and text synthesis, from task submission to result download.
- Complete voice design, cloning, previews, validation, and publication.
- Connect live captions, live speech, and meeting persistence, then expand assistants, teleprompters, and audiobooks.
- Complete AdminUI business features, resource governance, and real-model quality, recovery, and production deployment acceptance.

See the [implementation plan (Chinese)](docs/plans/2026-10-22-speechnexus-implementation-plan.md) for stages and dependencies, and the [model selection decision (Chinese)](docs/decisions/0002-speech-models-engines-and-loading.md) for model and engine validation plans.

## Documentation

| I want to learn about | Start here |
| --- | --- |
| Product scope and workflows | [Product design (Chinese)](docs/speechnexus-product-architecture.md) |
| Deployment, ports, and configuration | [Deployment guide](deploy/README.md) |
| API integration and compatibility | [Speech API (Chinese)](docs/architecture/speech-api.md) |
| Development and verification | [Development guide (Chinese)](docs/development-environment.md) |
| Architecture and module boundaries | [Technical blueprint (Chinese)](docs/architecture/technical-blueprint.md) |
| All designs, plans, and evidence | [Documentation index (Chinese)](docs/README.md) |

## Feedback and contributions

Use [Issues](https://github.com/hrygo/SpeechNexus/issues) to share use cases, request features, or report problems. Include your environment, reproduction steps, expected behavior, and actual results. Leave out credentials, private audio, and complete business transcripts.

See the [contribution and issue guide (Chinese)](CONTRIBUTING.md) for feedback templates, implementation tasks, and closure rules. For development, read the [development guide (Chinese)](docs/development-environment.md) and [AGENTS.md (Chinese)](AGENTS.md), then run checks appropriate to your changes. Full native verification uses `make verify`; first satisfy the guide's host and isolated-service requirements.

## License and acknowledgments

The repository does not currently include a project `LICENSE` file. Model weights, third-party dependencies, and voice materials require separate license review.

SpeechNexus is an independent project inspired by the product and architecture experience of [SpeechRail](https://github.com/hrygo/SpeechRail). See the [SpeechRail reuse boundaries (Chinese)](docs/architecture/speechrail-reuse.md) for attribution and reuse requirements.
