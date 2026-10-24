"""Probe the warmup readiness gate against a real approved ASR container.

Requires explicit runtime authorization, the pinned local ASR image/model and
espeak-ng. The probe synthesises one fixed 20-second window, declares it as an
approved warmup body, and drives the real controller until the instance reports
``ready``. It records the per-attempt duration so the first long-window cost and
the immediate repeat are both visible. No transcript, credential or model path
is printed, and scheduler budgets are untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from engine_probe_support import (
    docker_json,
    memory_available,
    protected_start,
    require_internal_network,
)

from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    MemoryArchitecture,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeInstanceIntentRequest,
    RuntimeIntentState,
    RuntimeMemoryReservation,
    RuntimeMemoryVector,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedTemplateRegistry,
    ReadinessKind,
    ReadinessRule,
    WarmupRequest,
)
from speechnexus_node_controller.infrastructure.docker_api import DockerApi
from speechnexus_node_controller.infrastructure.docker_runtime import (
    GENERATION_LABEL,
    INSTANCE_ID_LABEL,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    NODE_ID_LABEL,
    DockerNodeRuntime,
    DockerRuntimeSettings,
)
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore
from speechnexus_node_controller.infrastructure.template_config import load_approved_templates

_NODE = "node-asr-warmup-probe"
_WARMUP_TEXT = (
    "The speech system warms the longest supported window before it serves any caller. "
    "This fixed synthetic passage exists only to exercise the encoder, the decoder and the "
    "streaming response path in one bounded request."
)


class _WarmupLogHandler(logging.Handler):
    """Collect the adapter's structured warmup attempts without their payload."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.attempts: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() != "warmup attempt completed":
            return
        self.attempts.append(
            {
                "attempt": getattr(record, "attempt", None),
                "elapsed_ms": getattr(record, "elapsed_ms", None),
                "response_bytes": getattr(record, "response_bytes", None),
            }
        )


def _synthetic_window(directory: Path, seconds: float) -> tuple[Path, float]:
    """Render exactly ``seconds`` of mono 16 kHz PCM16 speech from fixed text."""
    executable = shutil.which("espeak-ng")
    if executable is None:
        raise RuntimeError("install espeak-ng before probing a synthetic warmup window")
    source = directory / "warmup-source.wav"
    subprocess.run(
        [executable, "-v", "en-us", "-s", "150", "-w", str(source), _WARMUP_TEXT],
        check=True,
        capture_output=True,
        timeout=120,
    )
    with wave.open(str(source), "rb") as original:
        if original.getnchannels() != 1 or original.getsampwidth() != 2:
            raise RuntimeError("espeak-ng did not produce mono PCM16")
        rate = original.getframerate()
        frames = original.readframes(original.getnframes())
    target = int(seconds * rate) * 2
    if len(frames) >= target:
        trimmed = frames[:target]
    else:
        trimmed = frames + b"\x00" * (target - len(frames))
    window = directory / "warmup-window.wav"
    with wave.open(str(window), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(trimmed)
    return window, len(trimmed) / (rate * 2)


def _multipart(window: Path, model: str, boundary: str) -> bytes:
    """Build the same fixed transcription request the engine adapter sends."""
    fields = {
        "model": model,
        "response_format": "json",
        "stream": "true",
        "stream_include_usage": "true",
        "stream_continuous_usage_stats": "false",
        "temperature": "0.0",
        "max_completion_tokens": "1024",
    }
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
        for name, value in fields.items()
    ]
    parts.extend(
        [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode(),
            window.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(parts)


def _create_internal_network(name: str) -> None:
    subprocess.run(
        ["docker", "network", "create", "--internal", "--driver", "bridge", name],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    network = docker_json("network", "inspect", name)[0]
    if not network.get("Internal") or network.get("Driver") != "bridge":
        raise RuntimeError("the probe network is not an internal bridge")


def run_probe(arguments: argparse.Namespace) -> dict[str, object]:
    template = ApprovedTemplateRegistry(load_approved_templates(arguments.templates_file)).require(
        arguments.template_id
    )
    if (
        template.readiness.kind is not ReadinessKind.HTTP_STATUS_OK
        or template.readiness.port is None
        or template.memory_architecture is not MemoryArchitecture.UNIFIED_MEMORY
        or "asr-batch" not in template.allowed_pool_ids
    ):
        raise ValueError("this probe requires a unified-memory ASR HTTP health template")
    suffix = uuid4().hex[:12]
    network_name = f"speechnexus-b02-warmup-{suffix}"
    evidence_directory = arguments.evidence_directory / suffix
    evidence_directory.mkdir(parents=True, exist_ok=False)
    instance_id = f"instance-asr-warmup-{suffix}"
    labels = {
        MANAGED_BY_LABEL: MANAGED_BY_VALUE,
        NODE_ID_LABEL: _NODE,
        INSTANCE_ID_LABEL: instance_id,
        GENERATION_LABEL: "1",
    }
    report: dict[str, object] = {
        "template_id": template.template_id,
        "window_seconds": arguments.window_seconds,
        "declared_attempts": arguments.attempts,
        "internal_network": True,
        "scheduler_budget_used": False,
        "audio_or_transcript_recorded": False,
    }
    failures: list[str] = []
    protected_started_at = protected_start()
    report["protected_llm_started_at"] = protected_started_at
    _create_internal_network(network_name)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="speechnexus-asr-warmup-") as workdir,
            DockerApi() as api,
        ):
            work = Path(workdir)
            window, audio_seconds = _synthetic_window(work, arguments.window_seconds)
            audio_sha256 = "sha256:" + hashlib.sha256(window.read_bytes()).hexdigest()
            body = _multipart(
                window, arguments.served_model_name, boundary=f"nexus-warmup-{suffix}"
            )
            body_path = work / "warmup.multipart"
            body_path.write_bytes(body)
            body_sha256 = "sha256:" + hashlib.sha256(body).hexdigest()
            warmup = WarmupRequest(
                path="/v1/audio/transcriptions",
                content_type=f"multipart/form-data; boundary=nexus-warmup-{suffix}",
                body_path=str(body_path),
                body_sha256=body_sha256,
                attempts=arguments.attempts,
                timeout_seconds=arguments.warmup_timeout_seconds,
                max_response_bytes=1024 * 1024,
            )
            effective = replace(
                template,
                network_name=network_name,
                readiness=ReadinessRule(
                    ReadinessKind.HTTP_WARMUP_OK, port=template.readiness.port, warmup=warmup
                ),
            )
            require_internal_network(effective)
            available_before = memory_available()
            if available_before < effective.max_peak_memory_bytes + 8 * 1024**3:
                raise RuntimeError("insufficient headroom for the approved peak plus 8 GiB")
            report.update(
                {
                    "audio_seconds": round(audio_seconds, 3),
                    "audio_sha256": audio_sha256,
                    "warmup_body_sha256": body_sha256,
                    "warmup_body_bytes": len(body),
                    "available_before_bytes": available_before,
                }
            )
            peak = RuntimeMemoryVector(unified_memory_bytes=effective.max_peak_memory_bytes)
            request = RuntimeInstanceIntentRequest(
                intent_id=f"intent-prepare-{suffix}",
                target_node_id=_NODE,
                instance_id=instance_id,
                instance_generation=1,
                approved_template_id=effective.template_id,
                pool_id="asr-batch",
                gateway_id="gateway-asr-warmup-probe",
                binding_revision_id="binding-asr-warmup-probe",
                manifest_id="manifest-asr-warmup-probe",
                config_digest="sha256:"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "template_id": effective.template_id,
                            "image_digest": effective.image_digest,
                            "warmup": body_sha256,
                            "attempts": arguments.attempts,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                action=RuntimeActionKind.PREPARE,
                fencing_token=1,
                # The probe cannot qualify a reduction, so steady stays at peak.
                reserved_memory=RuntimeMemoryReservation(
                    architecture=MemoryArchitecture.UNIFIED_MEMORY, peak=peak, steady=peak
                ),
                deadline_at=datetime.now(UTC)
                + timedelta(seconds=arguments.ready_timeout_seconds + 600),
            )
            store = SqliteIntentStore(work / "intents.sqlite3")
            runtime = DockerNodeRuntime(
                api=api,
                node_id=_NODE,
                settings=DockerRuntimeSettings(stop_timeout_seconds=45, exit_wait_seconds=60),
            )
            controller = NodeRuntimeControlService(
                node_id=_NODE,
                templates=ApprovedTemplateRegistry((effective,)),
                store=store,
                runtime=runtime,
            )
            handler = _WarmupLogHandler()
            adapter_logger = logging.getLogger(
                "speechnexus_node_controller.infrastructure.docker_runtime"
            )
            adapter_logger.addHandler(handler)
            adapter_logger.setLevel(logging.INFO)
            observed_states: list[str] = []
            try:
                started = time.monotonic()
                intent = controller.submit_intent(request).intent
                while True:
                    state = intent.observed_instance_state.value
                    if not observed_states or observed_states[-1] != state:
                        observed_states.append(state)
                    if intent.intent_state is RuntimeIntentState.SUCCEEDED:
                        break
                    if intent.intent_state.is_terminal:
                        raise RuntimeError("the ASR prepare intent failed")
                    if time.monotonic() - started > arguments.ready_timeout_seconds:
                        raise TimeoutError("the ASR engine did not become ready in time")
                    time.sleep(1)
                    controller.reconcile_instance(instance_id, 1)
                    current = controller.get_intent(request.intent_id)
                    if current is None:
                        raise RuntimeError("the prepare intent disappeared")
                    intent = current
                report["ready_seconds"] = round(time.monotonic() - started, 3)
                report["observed_states"] = observed_states
                report["warmup_attempts"] = handler.attempts
                container = api.find_container(labels)
                if container is None:
                    raise RuntimeError("the approved container disappeared")
                raw = docker_json("inspect", container.container_id)[0]
                report["image_id"] = raw["Image"]
                report["daemon_observed_memory_bytes"] = container.memory_peak_bytes
                report["available_ready_bytes"] = memory_available()
                stopped_at = time.monotonic()
                stopped = controller.submit_intent(
                    replace(
                        request,
                        intent_id=f"intent-stop-{suffix}",
                        action=RuntimeActionKind.STOP,
                        fencing_token=2,
                    )
                )
                evidence = stopped.intent.stop_evidence
                report["stop_seconds"] = round(time.monotonic() - stopped_at, 3)
                report["stop_double_evidence"] = bool(
                    evidence
                    and evidence.process_or_container_exited
                    and evidence.resources_released_confirmed
                )
                if not report["stop_double_evidence"]:
                    raise RuntimeError("the controller did not confirm both stop facts")
            except Exception as error:
                report["failure_type"] = type(error).__name__
                raise
            finally:
                adapter_logger.removeHandler(handler)
                leftovers = api.list_container_ids(labels)
                report["emergency_cleanup_needed"] = bool(leftovers)
                for container_id in leftovers:
                    api.remove_container(container_id)
                report["leftovers"] = list(api.list_container_ids(labels))
                store.close()
                report["available_after_bytes"] = memory_available()
                report["protected_llm_unchanged"] = protected_start() == protected_started_at
            attempts = [
                attempt for attempt in handler.attempts if attempt["elapsed_ms"] is not None
            ]
            if len(attempts) != arguments.attempts:
                failures.append(
                    f"expected {arguments.attempts} warmup attempts, saw {len(attempts)}"
                )
            if ObservedInstanceState.WARMING_UP.value not in observed_states:
                failures.append("the instance never reported warming_up")
            if observed_states[-1] != ObservedInstanceState.READY.value:
                failures.append("the last observed state was not ready")
            if len(attempts) == 2 and not (
                attempts[0]["elapsed_ms"] >= 3 * max(attempts[1]["elapsed_ms"], 1)
            ):
                failures.append("the first warmup was not materially slower than the repeat")
            if report["leftovers"] or not report["protected_llm_unchanged"]:
                failures.append("the probe did not preserve the required runtime state")
            report["failures"] = failures
            (evidence_directory / "report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
            )
    finally:
        subprocess.run(
            ["docker", "network", "remove", network_name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        report["network_removed"] = network_name not in _network_names()
        (evidence_directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
    if failures:
        raise RuntimeError("; ".join(failures))
    return report


def _network_names() -> set[str]:
    result = subprocess.run(
        ["docker", "network", "ls", "--format", "{{.Name}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return set(result.stdout.split())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates-file", type=Path, required=True)
    parser.add_argument("--template-id", required=True)
    parser.add_argument("--served-model-name", required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--window-seconds", type=float, default=20.0)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--warmup-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--ready-timeout-seconds", type=float, default=600.0)
    arguments = parser.parse_args()
    if not 1 <= arguments.attempts <= 4:
        parser.error("attempts must be between 1 and 4")
    if not 0 < arguments.window_seconds <= 30:
        parser.error("the warmup window must be at most 30 seconds")
    print(json.dumps(run_probe(arguments), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
