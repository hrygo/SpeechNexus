"""Probe the restricted node controller against a synthetic inert container.

The probe uses the real container adapter against the local daemon and a
synthetic approved template that runs ``python -m http.server`` inside a stock
image. It loads no model, touches no GPU service, publishes no host port, and
removes every container it creates. Its JSON output is the reproducible evidence
recorded in the B02 audit note.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from speechnexus_node_controller.application.runtime_control import (
    NodeRuntimeControlService,
)
from speechnexus_node_controller.domain.intents import (
    MemoryArchitecture,
    RuntimeActionKind,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeIntentState,
    RuntimeMemoryReservation,
    RuntimeMemoryVector,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    ApprovedTemplateRegistry,
    ReadinessKind,
    ReadinessRule,
)
from speechnexus_node_controller.infrastructure.docker_api import DockerApi
from speechnexus_node_controller.infrastructure.docker_runtime import (
    INSTANCE_ID_LABEL,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    DockerNodeRuntime,
    DockerRuntimeSettings,
)

_NODE_ID = "node-local-1"
_INSTANCE_ID = "instance-probe-inert"
_TEMPLATE_ID = "template-probe-inert-tcp"
_CONFIG_DIGEST = "sha256:" + "1" * 64
_READINESS_PORT = 8080
_DEADLINE_SECONDS = 300.0


class ProbeIntentStore:
    """Intent store for one probe run; the durable store ships with the deployment unit."""

    def __init__(self) -> None:
        self._intents: dict[str, RuntimeInstanceIntent] = {}

    def get(self, intent_id: str) -> RuntimeInstanceIntent | None:
        stored = self._intents.get(intent_id)
        return copy.deepcopy(stored) if stored is not None else None

    def add(self, intent: RuntimeInstanceIntent) -> None:
        if intent.intent_id in self._intents:
            raise ValueError(f"intent {intent.intent_id} already stored")
        self._intents[intent.intent_id] = copy.deepcopy(intent)

    def save(self, intent: RuntimeInstanceIntent, *, expected_version: int) -> None:
        stored = self._intents.get(intent.intent_id)
        if stored is None or stored.version != expected_version:
            raise ValueError(f"intent {intent.intent_id} version conflict")
        self._intents[intent.intent_id] = copy.deepcopy(intent)

    def list_for_instance(self, instance_id: str) -> list[RuntimeInstanceIntent]:
        return [
            copy.deepcopy(intent)
            for intent in self._intents.values()
            if intent.instance_id == instance_id
        ]


@dataclass(slots=True)
class ProbeReport:
    """Evidence the probe prints; every field is measured, not assumed."""

    image_reference: str = ""
    image_id: str = ""
    container_id: str = ""
    prepare_state: str = ""
    ready_state: str = ""
    ready_seconds: float | None = None
    observed_peak_bytes: int | None = None
    stop_state: str = ""
    stop_seconds: float | None = None
    stop_exit_code: int | None = None
    stop_verified_at: str = ""
    container_removed: bool = False
    leftovers: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return {
            "image_reference": self.image_reference,
            "image_id": self.image_id,
            "container_id": self.container_id,
            "prepare_state": self.prepare_state,
            "ready_state": self.ready_state,
            "ready_seconds": self.ready_seconds,
            "observed_peak_bytes": self.observed_peak_bytes,
            "stop_state": self.stop_state,
            "stop_seconds": self.stop_seconds,
            "stop_exit_code": self.stop_exit_code,
            "stop_verified_at": self.stop_verified_at,
            "container_removed": self.container_removed,
            "leftovers": self.leftovers,
        }


def _request(
    *, intent_id: str, action: RuntimeActionKind, fencing_token: int
) -> RuntimeInstanceIntentRequest:
    return RuntimeInstanceIntentRequest(
        intent_id=intent_id,
        target_node_id=_NODE_ID,
        pool_id="auxiliary",
        gateway_id="gateway-1",
        instance_id=_INSTANCE_ID,
        instance_generation=1,
        approved_template_id=_TEMPLATE_ID,
        binding_revision_id="binding-probe",
        manifest_id="manifest-probe",
        config_digest=_CONFIG_DIGEST,
        action=action,
        fencing_token=fencing_token,
        reserved_memory=RuntimeMemoryReservation(
            architecture=MemoryArchitecture.UNIFIED_MEMORY,
            peak=RuntimeMemoryVector(unified_memory_bytes=1024**3),
            steady=RuntimeMemoryVector(unified_memory_bytes=256 * 1024**2),
        ),
        deadline_at=datetime.now(UTC) + timedelta(seconds=_DEADLINE_SECONDS),
    )


def _template(image_reference: str, image_id: str) -> ApprovedRuntimeTemplate:
    return ApprovedRuntimeTemplate(
        template_id=_TEMPLATE_ID,
        image_reference=image_reference,
        image_digest=image_id,
        memory_architecture=MemoryArchitecture.UNIFIED_MEMORY,
        allowed_pool_ids=frozenset({"auxiliary"}),
        max_peak_memory_bytes=2 * 1024**3,
        container_memory_limit_bytes=256 * 1024**2,
        readiness=ReadinessRule(kind=ReadinessKind.TCP_PORT_ACCEPTING, port=_READINESS_PORT),
        command=("python", "-m", "http.server", str(_READINESS_PORT)),
    )


def _observe(
    service: NodeRuntimeControlService, report: ProbeReport, intent_id: str
) -> RuntimeIntentState:
    """Fold one node observation and return the recorded intent state."""
    outcomes = service.reconcile_instance(_INSTANCE_ID, 1)
    intent = outcomes[0].intent if outcomes else service.get_intent(intent_id)
    if intent is None:
        raise SystemExit(f"intent {intent_id} is not stored")
    report.ready_state = intent.observed_instance_state.value
    peak = intent.observed_peak_memory
    if peak is not None:
        report.observed_peak_bytes = peak.unified_memory_bytes
    return intent.intent_state


def run_probe(
    *, socket_path: str, image_reference: str, ready_timeout_seconds: float
) -> ProbeReport:
    """Run one prepare → observe → stop cycle; the container is always removed."""
    report = ProbeReport(image_reference=image_reference)
    api = DockerApi(socket_path=socket_path)
    try:
        api.ping()
        identity = api.inspect_image(image_reference)
        if identity is None:
            raise SystemExit(
                f"image {image_reference} is not present locally; pull it before probing"
            )
        report.image_id = identity.image_id
        service = NodeRuntimeControlService(
            node_id=_NODE_ID,
            templates=ApprovedTemplateRegistry((_template(image_reference, identity.image_id),)),
            store=ProbeIntentStore(),
            runtime=DockerNodeRuntime(
                api=api,
                node_id=_NODE_ID,
                settings=DockerRuntimeSettings(exit_wait_seconds=20.0),
            ),
        )

        prepared = service.submit_intent(
            _request(
                intent_id="intent-probe-prepare",
                action=RuntimeActionKind.PREPARE,
                fencing_token=1,
            )
        )
        report.prepare_state = prepared.intent.observed_instance_state.value
        container = api.find_container(
            {MANAGED_BY_LABEL: MANAGED_BY_VALUE, INSTANCE_ID_LABEL: _INSTANCE_ID}
        )
        if container is None:
            raise SystemExit("the prepared container could not be found again")
        report.container_id = container.container_id

        started = time.monotonic()
        while True:
            state = _observe(service, report, "intent-probe-prepare")
            if state is RuntimeIntentState.SUCCEEDED:
                report.ready_seconds = round(time.monotonic() - started, 3)
                break
            if state is RuntimeIntentState.FAILED:
                raise SystemExit(f"instance failed to prepare: {report.ready_state}")
            if time.monotonic() - started > ready_timeout_seconds:
                raise SystemExit(
                    f"instance did not reach ready within {ready_timeout_seconds}s "
                    f"(last observed state {report.ready_state})"
                )
            time.sleep(1.0)

        stop_started = time.monotonic()
        stopped = service.submit_intent(
            _request(
                intent_id="intent-probe-stop",
                action=RuntimeActionKind.STOP,
                fencing_token=2,
            )
        )
        report.stop_seconds = round(time.monotonic() - stop_started, 3)
        report.stop_state = stopped.intent.intent_state.value
        evidence = stopped.intent.stop_evidence
        if evidence is not None:
            report.stop_exit_code = evidence.exit_code
            report.stop_verified_at = evidence.verified_at.isoformat()
        report.container_removed = api.inspect_container(container.container_id) is None
        report.leftovers = list(
            api.list_container_ids(
                {MANAGED_BY_LABEL: MANAGED_BY_VALUE, INSTANCE_ID_LABEL: _INSTANCE_ID}
            )
        )
    finally:
        api.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/var/run/docker.sock")
    parser.add_argument("--image", default="python:3.14.9-slim-bookworm")
    parser.add_argument("--ready-timeout-seconds", type=float, default=60.0)
    arguments = parser.parse_args(argv)

    report = run_probe(
        socket_path=arguments.socket,
        image_reference=arguments.image,
        ready_timeout_seconds=arguments.ready_timeout_seconds,
    )
    print(json.dumps(report.as_json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
