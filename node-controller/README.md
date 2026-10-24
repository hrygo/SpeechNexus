# SpeechNexus node controller

This independently deployed process owns the node-scoped lifecycle of approved
runtime instances: it claims the `runtime-control` prepare/stop intents whose
`target_node_id` is its own node, refuses anything outside an approved template,
keeps instance generations and fencing tokens monotonic, retains peak
reservation until readiness is observed, and reports stop evidence only when a
container exit and its resource release were both verified.

The controller never receives business audio, transcript text, arbitrary images,
host mounts, or shell commands, and it does not own the scheduler's budget
authority: it reports durable intent and observation facts through
`contracts/internal/runtime-control.openapi.yaml` and leaves admission
accounting to the scheduler.

The controller includes the HTTP shell, a durable local SQLite receipt store,
and a restricted Docker adapter. Approved templates pin the image, arguments,
read-only artifacts, GPU request, container memory limit and readiness probe.
HTTP readiness accepts only 200 from a fixed container IP/path, without
environment proxies or redirects. A declared custom network is supplied by
deployment configuration; its name alone does not prove egress isolation.

Template files must declare `container_memory_limit_bytes`; old files fail
startup rather than retaining an unlimited container. Docker Memory and
MemorySwap are set to the same fixed value, covered by the intent's host or
unified peak reservation. This is not proof of GPU allocation isolation or
qualification for a lower steady budget. Discovered containers whose resource
settings differ are quarantined and remain stoppable.

Synthetic container/HTTP probes verify only the paths recorded in the
[controller design](../docs/architecture/node-controller.md). Real engine,
scheduler-budget, warmup, recovery and physical-cluster qualification require
their own evidence.
