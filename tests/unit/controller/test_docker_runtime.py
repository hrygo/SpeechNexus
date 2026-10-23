"""Restricted container adapter: approved image, one container per generation, verified stop."""

from __future__ import annotations

import socket
from typing import Any

import pytest
from tests.fake.controller import (
    IMAGE_DIGEST,
    SAMPLE_IMAGE_REFERENCE,
    build_request,
    build_template,
)
from tests.fake.docker_api import FakeDockerEngine

from speechnexus_node_controller.domain.intents import (
    IntentConflictError,
    ObservedInstanceState,
    RuntimeActionKind,
    UnapprovedRuntimeTemplateError,
)
from speechnexus_node_controller.domain.templates import (
    ReadinessKind,
    ReadinessRule,
    ReadOnlyBind,
)
from speechnexus_node_controller.infrastructure.docker_runtime import (
    CONFIG_DIGEST_LABEL,
    GENERATION_LABEL,
    INSTANCE_ID_LABEL,
    MANAGED_BY_LABEL,
    NODE_ID_LABEL,
    TEMPLATE_ID_LABEL,
    DockerNodeRuntime,
    DockerRuntimeSettings,
)

_NODE = "node-local-1"


def _engine(**overrides: Any) -> FakeDockerEngine:
    images = {"local/other-engine:1": "sha256:" + "d" * 64}
    images.update(overrides.pop("images", {SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST}))
    return FakeDockerEngine(images=images, **overrides)


def _runtime(engine: FakeDockerEngine, **settings: Any) -> DockerNodeRuntime:
    return DockerNodeRuntime(
        api=engine,
        node_id=_NODE,
        settings=DockerRuntimeSettings(**settings),
    )


def test_prepare_verifies_the_approved_image_before_creating_anything() -> None:
    engine = _engine(images={"local/other-engine:1": "sha256:" + "d" * 64})
    runtime = _runtime(engine)
    with pytest.raises(UnapprovedRuntimeTemplateError):
        runtime.prepare(build_request(), build_template())
    assert engine.created == []


def test_prepare_creates_one_container_for_the_generation() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    observation = runtime.prepare(build_request(), build_template())
    assert observation.observed_instance_state is ObservedInstanceState.READY
    assert len(engine.created) == 1


def test_prepare_reuses_the_container_instead_of_creating_a_second_one() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    runtime.prepare(build_request(), build_template())
    runtime.prepare(build_request(), build_template())
    assert len(engine.created) == 1


def test_prepare_refuses_a_generation_that_already_carries_another_digest() -> None:
    engine = _engine()
    request = build_request()
    engine.create_container(
        name="speechnexus-instance-instance-asr-1-g1",
        image_reference=SAMPLE_IMAGE_REFERENCE,
        command=None,
        labels={
            MANAGED_BY_LABEL: "speechnexus-node-controller",
            INSTANCE_ID_LABEL: request.instance_id,
            GENERATION_LABEL: str(request.instance_generation),
            TEMPLATE_ID_LABEL: request.approved_template_id,
            CONFIG_DIGEST_LABEL: "sha256:" + "e" * 64,
        },
        host_config={},
    )
    runtime = _runtime(engine)
    with pytest.raises(IntentConflictError):
        runtime.prepare(request, build_template())
    assert len(engine.created) == 1


def test_prepare_carries_identity_labels_and_hardening() -> None:
    engine = _engine()
    request = build_request()
    runtime = _runtime(engine)
    runtime.prepare(
        request,
        build_template(
            command=("serve", "--port", "9000"),
            gpu_device_count=1,
            read_only_binds=(ReadOnlyBind(source="/models/asr", target="/models/asr"),),
        ),
    )
    container = engine.created[0]
    assert container.labels[MANAGED_BY_LABEL] == "speechnexus-node-controller"
    assert container.labels[NODE_ID_LABEL] == _NODE
    assert container.labels[INSTANCE_ID_LABEL] == request.instance_id
    assert container.labels[GENERATION_LABEL] == str(request.instance_generation)
    assert container.labels[TEMPLATE_ID_LABEL] == request.approved_template_id
    assert container.labels[CONFIG_DIGEST_LABEL] == request.config_digest
    assert container.command == ("serve", "--port", "9000")
    assert container.host_config["Privileged"] is False
    assert container.host_config["Init"] is True
    assert container.host_config["CapDrop"] == ["ALL"]
    assert container.host_config["SecurityOpt"] == ["no-new-privileges"]
    assert container.host_config["RestartPolicy"] == {"Name": "no"}
    assert container.host_config["Binds"] == ["/models/asr:/models/asr:ro"]
    assert container.host_config["DeviceRequests"] == [
        {"Driver": "nvidia", "Count": 1, "Capabilities": [["gpu"]]}
    ]


def test_container_name_is_derived_from_the_instance_identity() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    runtime.prepare(build_request(instance_id="instance:asr-1"), build_template())
    assert engine.created[0].name == "speechnexus-instance-instance-asr-1-g1"


def test_tcp_readiness_reports_loading_until_the_port_accepts() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        template = build_template(
            readiness=ReadinessRule(ReadinessKind.TCP_PORT_ACCEPTING, port=port)
        )
        engine = _engine(addresses=("127.0.0.1",))
        runtime = _runtime(engine)
        observation = runtime.prepare(build_request(), template)
        assert observation.observed_instance_state is ObservedInstanceState.READY
    finally:
        listener.close()

    engine = _engine(addresses=("127.0.0.1",))
    runtime = _runtime(engine)
    closed_port_template = build_template(
        readiness=ReadinessRule(ReadinessKind.TCP_PORT_ACCEPTING, port=port)
    )
    observation = runtime.prepare(build_request(), closed_port_template)
    assert observation.observed_instance_state is ObservedInstanceState.LOADING


def test_prepare_reports_the_observed_peak_memory() -> None:
    engine = _engine(memory_peak_bytes=3 * 1024**3)
    runtime = _runtime(engine)
    observation = runtime.prepare(build_request(), build_template())
    assert observation.observed_peak_memory is not None
    assert observation.observed_peak_memory.unified_memory_bytes == 3 * 1024**3


def test_stop_without_a_container_reports_absent_and_no_evidence() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.observed_instance_state is ObservedInstanceState.ABSENT
    assert observation.stop_evidence is None


def test_stop_verifies_exit_and_removal_before_reporting_evidence() -> None:
    engine = _engine(exit_code=0)
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.observed_instance_state is ObservedInstanceState.STOPPED
    assert observation.stop_evidence is not None
    assert observation.stop_evidence.exit_code == 0
    assert observation.stop_evidence.runtime_container_id in engine.removed
    assert engine.containers == {}


def test_stop_without_confirmed_removal_reports_no_evidence() -> None:
    engine = _engine(fail_remove_and_keep=True)
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.stop_evidence is None
    assert observation.observed_instance_state is ObservedInstanceState.STOPPED


def test_stop_that_cannot_wait_for_exit_keeps_reporting_the_live_instance() -> None:
    engine = _engine(fail_wait=True)
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.stop_evidence is None
    assert observation.observed_instance_state is ObservedInstanceState.READY
    assert engine.containers != {}


def test_stop_that_times_out_keeps_reporting_the_live_instance() -> None:
    engine = _engine(fail_stop=True)
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.stop(build_request(action=RuntimeActionKind.STOP), build_template())
    assert observation.stop_evidence is None
    assert observation.observed_instance_state is ObservedInstanceState.READY


def test_inspect_reports_absent_without_fabricating_evidence() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    observation = runtime.inspect(build_request(), build_template())
    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.ABSENT
    assert observation.stop_evidence is None


def test_inspect_quarantines_a_container_that_carries_another_digest() -> None:
    engine = _engine()
    request = build_request()
    engine.create_container(
        name="speechnexus-instance-instance-asr-1-g1",
        image_reference=SAMPLE_IMAGE_REFERENCE,
        command=None,
        labels={
            MANAGED_BY_LABEL: "speechnexus-node-controller",
            INSTANCE_ID_LABEL: request.instance_id,
            GENERATION_LABEL: str(request.instance_generation),
            TEMPLATE_ID_LABEL: request.approved_template_id,
            CONFIG_DIGEST_LABEL: "sha256:" + "e" * 64,
        },
        host_config={},
    )
    runtime = _runtime(engine)
    observation = runtime.inspect(request, build_template())
    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.QUARANTINED
    assert observation.quarantine_reason is not None


def test_inspect_of_a_running_container_reports_the_ready_state() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    request = build_request()
    runtime.prepare(request, build_template())
    observation = runtime.inspect(request, build_template())
    assert observation is not None
    assert observation.observed_instance_state is ObservedInstanceState.READY
    assert observation.observed_peak_memory is not None
