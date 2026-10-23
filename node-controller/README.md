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

The current unit implements the domain rules, the application service, and the
deterministic fakes for a node-local container runtime. The HTTP shell, the
persistent intent store, and the container adapter follow in later units.
