# SpeechNexus · Acoustic Hub (声枢)

![SpeechNexus · Acoustic Hub — Self-Hosted Speech Engine & Studio](web/shared/src/assets/brand-banner.svg)

**Self-hosted speech engine and voice creation studio**

[简体中文](README.md) · [Features](#features-and-use-cases) · [Quick start](#quick-start) · [Documentation](docs/README.md) · [Feedback](https://github.com/hrygo/SpeechNexus/issues)

[![CI](https://github.com/hrygo/SpeechNexus/actions/workflows/ci.yml/badge.svg)](https://github.com/hrygo/SpeechNexus/actions/workflows/ci.yml)

SpeechNexus is for teams that want to deploy speech capabilities in their own Linux environment. The product brings together a **speech API, an AdminUI, and a personal speech workbench**. Its goal is to connect speech recognition, synthesis, voice creation, and everyday speech tasks: developers integrate through APIs, users work in the browser, and maintainers manage services and resources.

> **Early development.** You can currently launch AdminUI and workbench previews and call service health endpoints. Transcription, synthesis, voice creation, and workbench business workflows are not yet available. The features below describe the product roadmap; real models and production deployments remain unverified.

## Who it is for

SpeechNexus is designed for three needs:

- **Add speech to applications**: use a common API for file and realtime recognition, synthesis, and voice resources. The speech foundation can run independently of the WebUI and LLM integration.
- **Complete speech tasks in the browser**: capture, edit, preview, resume, and export work across transcription, dubbing, meetings, captions, and audiobooks.
- **Operate services in your own environment**: manage model services, scenario presets, shared voices, tasks, and resources, with local speech inference and optional intranet LLM integration.

## Features and use cases

| Feature                            | What you can use it for                                                                                           |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Speech recognition                 | Upload recordings or stream audio to produce editable transcripts, captions, and optional speaker labels          |
| Speech synthesis                   | Turn text into speech using preset or published voices, with file generation and realtime output                  |
| Voice creation                     | Design a voice from a description or clone a reference sample, then preview, validate, publish, and reuse it      |
| Meetings and live captions         | Follow transcripts, mark key points, save records, and optionally generate meeting notes with an intranet LLM     |
| Voice assistants and teleprompters | Combine recognition, text generation, and synthesis for voice interactions, or follow a script as you speak       |
| Audiobook production               | Import manuscripts, organize chapters and characters, assign voices, generate chapters, redo sections, and export |

Long-running tasks are planned to support progress tracking, cancellation, recovery, and downloads. Scenario presets will let users reuse recognition, review, and synthesis settings. See the [product design (Chinese)](docs/speechnexus-product-architecture.md) for the full scope.

## Ways to use SpeechNexus

### Complete tasks in the workbench

The workbench organizes actions around tasks. Once business features are available, typical workflows will be:

- **Transcribe a recording**: upload audio → choose a language and preset → follow progress → edit the text → export a transcript or captions.
- **Create a voiceover**: enter text → choose a voice → generate and preview → revise the content → export audio.
- **Create a voice**: submit a description or reference sample → generate candidates → preview and validate reuse → confirm publication → use it for future voiceovers.

### Integrate through the API

The P0 contracts freeze an OpenAI Audio-compatible subset plus `nexus` interfaces for voices, durable tasks, and scenario presets for applications, SDKs, and MCP callers. A standard OpenAI SDK can point `base_url` at the deployment's `/v1`. Realtime supports a transcription-only `model` connection mode and pre-created nexus sessions; both cover only the declared event subset.

Runtime endpoints include `/healthz`, `/readyz`, and configured Asset upload, metadata and content download. The initial path validates actual UTF-8 text; audio inspection depends on B03, and other speech/workbench routes remain in development. Protocol calls, compatibility scope, and examples are documented in the [speech API design (Chinese)](docs/architecture/speech-api.md) and [OpenAI compatibility boundaries (Chinese)](docs/architecture/openai-compatibility.md). Machine contracts are indexed in [contracts](contracts/README.md).

Contract repairs cover canonical execution commands, synchronous idempotency recovery, strict receipts and data deletion. See [ADR 0005 (Chinese)](docs/decisions/0005-complete-execution-and-data-lifecycle-contracts.md) for scope and upgrade boundaries. These business protocols are not implemented as runtime routes.

Full cluster support remains required. [ADR 0006 (Chinese)](docs/decisions/0006-cluster-coordination-and-storage-portability.md) excludes etcd and moves cross-table rules into controlled application transactions to reduce database coupling. PostgreSQL is the only database targeted for official product support; MySQL/InnoDB is limited to validating key coordination behavior. [Simplified DDL, application transaction design and contracts (Chinese)](docs/architecture/coordination-transactions.md) are delivered, with a shared test prototype on real PG/InnoDB. Production integration and full cluster fault qualification remain pending; see the [refactoring plan (Chinese)](docs/plans/2026-10-23-cluster-coordination-refactor.md).

The [first production coordination unit (Chinese)](docs/architecture/production-jobs.md) implements atomic Job/outbox creation, full-identity replay and read-only recovery of unknown commits, with a PG adapter, minimum runtime grants and versioned migration. It is not connected to public business routes and does not implement the complete public Job or file-speech workflow.

The [`0002_coordination` production storage foundation (Chinese)](docs/architecture/production-coordination-storage.md) completes the 15 coordination tables and minimum A/B role grants while preserving existing Job/outbox data. Remaining scheduling, admission and gateway transactions, and real runtime qualification, still require their owners' implementation and evidence.

[Production owner epoch transactions (Chinese)](docs/architecture/production-owners.md) provide acquire, renew, activate and current-authority queries, with database time after lock waits, complete snapshots and unknown-commit recovery. Scheduler loops and runtime admission remain separate delivery units.

[Runtime intent transactions (Chinese)](docs/architecture/production-runtime-intents.md) provide independent create/query/observe ports: reserve the complete peak atomically, require trusted warmup/qualification before moving to steady, and return the base only after verified stopping and completed cleanup. Synthetic PG/InnoDB facts do not prove GPU qualification. Approved-plan/readiness providers, durable dispatch and control loops remain unconnected; there are no new public routes.

The [execution gateway (Chinese)](docs/architecture/execution-gateway.md) provides persistent submit/stop/quarantine, guarded execution observation, trusted command-body redaction and output interval append/original-fact queries, an authenticated HTTP ingress barrier and read-only execution queries enabled by explicit control configuration. Actual loopback HTTP/PostgreSQL integration is verified, with synthetic recovery, observation, command redaction, interval deduplication and unsent-cleanup guards checked on PG/InnoDB. Real metering producers/consumers, deletion authorization, submit/abort routes, real engines and cross-host qualification remain pending. Default Compose does not yet enable these control endpoints.

The [restricted node controller (Chinese)](docs/architecture/node-controller.md) includes HTTP, SQLite and Docker adapters, with approved container memory limits and HTTP health probes. Actual Qwen3-ASR 0.6B/1.7B loading, synthetic speech and verified stopping have [local evidence](docs/audits/2026-10-24-b02-real-asr-controller-probe.md). Silence hallucinations, slow long non-speech responses, TTS, the scheduler budget control loop and cluster qualification remain unresolved; this does not make the public speech workflow available.

[Job claims (Chinese)](docs/architecture/production-jobs.md#worker-认领与当前权威查询) provide version/expiry-guarded worker acquisition and read-only current-authority queries. Reclaiming does not authorize engine redispatch; worker loops and artifact publication fencing remain separate work.

The [asset publication storage unit](docs/architecture/production-assets.md) provides actual text validation, PG/versioned MinIO publication, original-identity recovery and retention deletion ledgers. Public upload/download routes and an independent maintenance entry point are connected; audio inspection depends on B03, while Artifact and Operation/receipt remain pending.

### Maintain a deployment through AdminUI

AdminUI is for deployment maintainers, with planned views for service health, models and voices, presets, resource budgets, global tasks, and diagnostics. The workbench and AdminUI organize user tasks and maintenance tasks separately.

## Quick start

Preview the current version with Docker Compose. You will need Git, Docker, and the Compose plugin.

### 1. Get the source and prepare configuration

```bash
git clone https://github.com/hrygo/SpeechNexus.git
cd SpeechNexus
cp deploy/compose.env.example deploy/compose.env
```

Edit `deploy/compose.env` to replace the example PostgreSQL and MinIO passwords. If you have enabled the native development environment, run `make dev-down` first to avoid database and object-storage port conflicts.

### 2. Build and start

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml up --build -d
```

Once the services are running, open:

| Entry                     | Address                         | Currently available                                 |
| ------------------------- | ------------------------------- | --------------------------------------------------- |
| AdminUI                   | <http://127.0.0.1:8080>         | Admin interface preview and capability status notes |
| Personal speech workbench | <http://127.0.0.1:8081>         | Workbench preview and task scope notes              |
| Speech service            | <http://127.0.0.1:8000/healthz> | Process liveness check                              |

This startup does not load speech models. Check infrastructure readiness with:

```bash
curl --fail http://127.0.0.1:8000/readyz
```

### 3. Stop services

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml down
```

This preserves the database and object-storage volumes. See the [deployment guide](deploy/README.md) for configuration, the full port list, and deployment boundaries.

## Deployment and data

The initial scope is a shared deployment within a trusted network, without login, role permissions, or tenant isolation. **Users of a deployment share its data.** “Personal workbench” describes the task experience, not a private user space. The operator controls access through the deployment network and entry point.

The product design requires local speech inference and keeps business audio, transcripts, manuscripts, voice references, and derived content within the deployment network. LLM integration for assistants and meeting notes is optional, disabled by default, and restricted to deployment-approved local / intranet services, with no public-cloud fallback. LLM integration and full egress restrictions remain unimplemented; see the [network boundary decision (Chinese)](docs/decisions/0003-workbench-llm-network-boundary.md).

## Roadmap

- [x] Runnable services, health checks, and two Web entry previews.
- [ ] File and realtime recognition, synthesis, and model integration.
- [ ] Voice design, cloning, validation, publication, and durable tasks.
- [ ] Workbench transcription, dubbing, and AdminUI business features.
- [ ] Meetings, captions, assistants, teleprompters, and audiobook workflows.
- [ ] Real-model quality, capacity, recovery, and production deployment validation.

See the [implementation plan (Chinese)](docs/plans/2026-10-22-speechnexus-implementation-plan.md) for stages and dependencies, and the [model selection decision (Chinese)](docs/decisions/0002-speech-models-engines-and-loading.md) for the preferred model and engine validation path.

## Documentation

| I want to learn about                | Start here                                                                |
| ------------------------------------ | ------------------------------------------------------------------------- |
| Product scope and complete workflows | [Product design (Chinese)](docs/speechnexus-product-architecture.md)      |
| API compatibility and planned usage  | [Speech API design (Chinese)](docs/architecture/speech-api.md)            |
| Deployment, ports, and configuration | [Deployment guide](deploy/README.md)                                      |
| Native development and testing       | [Development environment (Chinese)](docs/development-environment.md)      |
| Architecture and implementation      | [Technical blueprint (Chinese)](docs/architecture/technical-blueprint.md) |
| All designs and decisions            | [Documentation index (Chinese)](docs/README.md)                           |
| Cluster architecture and single-node MVP | [Cluster-ready deployment decision (Chinese)](docs/decisions/0004-cluster-ready-architecture-and-single-node-mvp.md) |

## Contributing

Use [Issues](https://github.com/hrygo/SpeechNexus/issues) to report problems, share use cases, or discuss features. Pull requests are welcome. Include your environment, reproduction steps, and expected behavior when reporting a problem; leave out credentials and private audio.

Native development uses Python 3.14 / uv and Node 26 / pnpm 12. Follow the [development environment guide (Chinese)](docs/development-environment.md) to prepare your setup. Run checks appropriate to your changes before submitting; the full native verification command is:

```bash
make verify
```

See [AGENTS.md (Chinese)](AGENTS.md) and the [repository layout guide (Chinese)](docs/architecture/repository-layout.md) for engineering rules and validation requirements.

## License and acknowledgments

The repository does not currently include a project `LICENSE` file. Model weights, third-party dependencies, and voice materials require separate license review.

SpeechNexus is an independent project inspired by the product and architecture experience of [SpeechRail](https://github.com/hrygo/SpeechRail). See the [SpeechRail reuse boundaries (Chinese)](docs/architecture/speechrail-reuse.md) for attribution and reuse requirements.
