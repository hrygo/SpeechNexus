"""Probe an approved ASR container through the real controller lifecycle.

Requires explicit runtime authorization, a local pinned image/model, espeak-ng,
and a pre-created Docker --internal network declared in the template. The default
path uses synthetic speech, silence and tone. The explicit file-ASR/VAD mode may
also use hash-verified authorized local excerpts with content logging disabled. No transcript,
credential or model path is printed. This probe does not write scheduler budgets
or qualify production admission, streaming, cancellation or a physical cluster.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from engine_probe_support import (
    docker_json,
    memory_available,
    protected_start,
    require_internal_network,
)

from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    MemoryArchitecture,
    RuntimeActionKind,
    RuntimeInstanceIntentRequest,
    RuntimeIntentState,
    RuntimeMemoryReservation,
    RuntimeMemoryVector,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedTemplateRegistry,
    ReadinessKind,
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

_NODE = "node-asr-probe"
_SPEECH = "The speech system is ready. Please check the sound and save the result."


def _samples(directory: Path) -> list[tuple[str, Path, str | None]]:
    executable = shutil.which("espeak-ng")
    if executable is None:
        raise RuntimeError("install espeak-ng before probing synthetic speech")
    speech = directory / "speech.wav"
    subprocess.run(
        [executable, "-v", "en-us", "-s", "155", "-w", str(speech), _SPEECH],
        check=True,
        capture_output=True,
        timeout=30,
    )
    samples: list[tuple[str, Path, str | None]] = [
        ("speech-first", speech, _SPEECH),
        ("speech-repeat", speech, _SPEECH),
    ]
    for name, seconds, frequency in [("silence-4s", 4, 0), ("tone-30s", 30, 440)]:
        path = directory / f"{name}.wav"
        rate = 16000
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(
                b"".join(
                    struct.pack("<h", round(4000 * math.sin(2 * math.pi * frequency * i / rate)))
                    for i in range(seconds * rate)
                )
            )
        samples.append((name, path, None))
    return samples


def _word_error_rate(reference: str, actual: str) -> float:
    expected = re.findall(r"\w+", reference.casefold())
    observed = re.findall(r"\w+", actual.casefold())
    row = list(range(len(observed) + 1))
    for index, word in enumerate(expected, start=1):
        previous, row = row, [index]
        for column, candidate in enumerate(observed, start=1):
            row.append(
                min(
                    previous[column] + 1,
                    row[-1] + 1,
                    previous[column - 1] + (word != candidate),
                )
            )
    return row[-1] / len(expected)


def _transcribe(
    client: httpx.Client, model: str, name: str, path: Path, expected: str | None
) -> dict[str, object]:
    request_id = f"probe-{uuid4().hex}"
    with wave.open(str(path), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
    started = time.monotonic()
    with path.open("rb") as source:
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"X-Request-ID": request_id},
            data={
                "model": model,
                "language": "en",
                "response_format": "json",
                "max_completion_tokens": "64",
                "temperature": "0",
            },
            files={"file": (f"{name}.wav", source, "audio/wav")},
        )
    elapsed = time.monotonic() - started
    result: dict[str, object] = {
        "sample": name,
        "http_status": response.status_code,
        "audio_seconds": round(duration, 3),
        "elapsed_seconds": round(elapsed, 3),
        "request_id_echoed": response.headers.get("x-request-id") == request_id,
    }
    if response.status_code != 200:
        # Error bodies may contain internal paths. Keep only the status.
        raise RuntimeError(f"synthetic ASR sample {name} returned HTTP {response.status_code}")
    text = response.json().get("text")
    if not isinstance(text, str):
        raise RuntimeError("the ASR response has no string text")
    result["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    result["text_empty"] = not text.strip()
    if expected is not None:
        result["word_error_rate"] = round(_word_error_rate(expected, text), 4)
    return result


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
    prepared_audio = arguments.file_asr_vad_model is not None
    if prepared_audio:
        required = {
            "--no-enable-log-requests",
            "--no-enable-log-outputs",
            "--disable-uvicorn-access-log",
        }
        forbidden = {
            "--enable-log-requests",
            "--enable-log-outputs",
            "--no-disable-uvicorn-access-log",
        }
        if (
            template.command is None
            or not required.issubset(template.command)
            or forbidden.intersection(template.command)
        ):
            raise ValueError(
                "file ASR probes require explicitly disabled request/output/access logs"
            )
    require_internal_network(template)
    protected_started_at = protected_start()
    available_before = memory_available()
    if available_before < template.max_peak_memory_bytes + 8 * 1024**3:
        raise RuntimeError("insufficient headroom for the approved peak plus 8 GiB")
    suffix = uuid4().hex[:12]
    evidence_directory = arguments.evidence_directory / suffix
    evidence_directory.mkdir(parents=True, exist_ok=False)
    instance_id = f"instance-asr-probe-{suffix}"
    labels = {
        MANAGED_BY_LABEL: MANAGED_BY_VALUE,
        NODE_ID_LABEL: _NODE,
        INSTANCE_ID_LABEL: instance_id,
        GENERATION_LABEL: "1",
    }
    peak = RuntimeMemoryVector(unified_memory_bytes=template.max_peak_memory_bytes)
    request = RuntimeInstanceIntentRequest(
        intent_id=f"intent-prepare-{suffix}",
        target_node_id=_NODE,
        instance_id=instance_id,
        instance_generation=1,
        approved_template_id=template.template_id,
        pool_id="asr-batch",
        gateway_id="gateway-asr-probe",
        binding_revision_id="binding-asr-probe",
        manifest_id="manifest-asr-probe",
        config_digest="sha256:" + hashlib.sha256(arguments.templates_file.read_bytes()).hexdigest(),
        action=RuntimeActionKind.PREPARE,
        fencing_token=1,
        # Retain the full peak in steady too: this probe cannot qualify a reduction.
        reserved_memory=RuntimeMemoryReservation(
            architecture=MemoryArchitecture.UNIFIED_MEMORY, peak=peak, steady=peak
        ),
        deadline_at=datetime.now(UTC) + timedelta(seconds=arguments.ready_timeout_seconds + 600),
    )
    report: dict[str, object] = {
        "template_id": template.template_id,
        "internal_network": True,
        "synthetic_only": arguments.corpus_root is None,
        "scheduler_budget_used": False,
        "protected_llm_started_at": protected_started_at,
        "available_before_bytes": available_before,
    }
    with (
        tempfile.TemporaryDirectory(prefix="speechnexus-asr-probe-") as workdir,
        DockerApi() as api,
    ):
        work = Path(workdir)
        samples = [] if prepared_audio else _samples(work)
        store = SqliteIntentStore(work / "intents.sqlite3")
        runtime = DockerNodeRuntime(
            api=api,
            node_id=_NODE,
            settings=DockerRuntimeSettings(stop_timeout_seconds=45, exit_wait_seconds=60),
        )

        def service() -> NodeRuntimeControlService:
            return NodeRuntimeControlService(
                node_id=_NODE,
                templates=ApprovedTemplateRegistry((template,)),
                store=store,
                runtime=runtime,
            )

        controller = service()

        def save_engine_log() -> None:
            for container_id in api.list_container_ids(labels):
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "2000", container_id],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                (evidence_directory / "engine.log").write_bytes(logs.stdout + logs.stderr)

        try:
            started = time.monotonic()
            prepared = controller.submit_intent(request)
            report["first_observed_state"] = prepared.intent.observed_instance_state.value
            while prepared.intent.intent_state is not RuntimeIntentState.SUCCEEDED:
                if prepared.intent.intent_state.is_terminal:
                    raise RuntimeError("the ASR prepare intent failed")
                if time.monotonic() - started > arguments.ready_timeout_seconds:
                    raise TimeoutError("the ASR engine did not become healthy in time")
                time.sleep(1)
                controller.reconcile_instance(instance_id, 1)
                intent = controller.get_intent(request.intent_id)
                if intent is None:
                    raise RuntimeError("the prepare intent disappeared")
                prepared = replace(prepared, intent=intent)
            report["ready_seconds"] = round(time.monotonic() - started, 3)
            state = api.find_container(labels)
            if state is None:
                raise RuntimeError("the approved container disappeared")
            raw = docker_json("inspect", state.container_id)[0]
            report["image_id"] = raw["Image"]
            report["container_memory_limit_bytes"] = raw["HostConfig"]["Memory"]
            report["container_memory_swap_bytes"] = raw["HostConfig"]["MemorySwap"]
            report["host_ports_published"] = bool(raw["HostConfig"].get("PortBindings"))
            report["available_ready_bytes"] = memory_available()
            report["daemon_observed_memory_bytes"] = state.memory_peak_bytes
            # Reconstruct the controller and its local receipt store while the engine lives.
            store.close()
            store = SqliteIntentStore(work / "intents.sqlite3")
            controller = service()
            replay = controller.submit_intent(request)
            report["restart_replayed_original"] = replay.idempotent_replay
            report["containers_after_restart"] = len(api.list_container_ids(labels))
            if not replay.idempotent_replay or report["containers_after_restart"] != 1:
                raise RuntimeError("reconstructing the controller did not preserve one instance")
            address = state.network_addresses[0]
            if prepared_audio:
                from file_asr_probe_cases import file_asr_cases

                report["request_output_access_logging_disabled"] = True
                report["file_asr"] = asyncio.run(
                    file_asr_cases(
                        arguments, address, template.readiness.port, work, evidence_directory
                    )
                )
            else:
                with httpx.Client(
                    base_url=f"http://{address}:{template.readiness.port}",
                    timeout=120,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    report["samples"] = [
                        _transcribe(client, arguments.served_model_name, *sample)
                        for sample in samples
                    ]
            save_engine_log()
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
            report["stop_state"] = stopped.intent.intent_state.value
            report["stop_exit_code"] = evidence.exit_code if evidence else None
            report["stop_double_evidence"] = bool(
                evidence
                and evidence.process_or_container_exited
                and evidence.resources_released_confirmed
            )
            if not evidence or stopped.intent.intent_state is not RuntimeIntentState.SUCCEEDED:
                raise RuntimeError("the controller did not confirm both stop facts")
        except Exception as error:
            report["failure_type"] = type(error).__name__
            raise
        finally:
            # Emergency cleanup is scoped to this unique run and is never stop evidence.
            leftovers = api.list_container_ids(labels)
            report["emergency_cleanup_needed"] = bool(leftovers)
            if leftovers:
                save_engine_log()
            for container_id in leftovers:
                api.remove_container(container_id)
            report["leftovers"] = list(api.list_container_ids(labels))
            store.close()
            report["available_after_bytes"] = memory_available()
            report["protected_llm_unchanged"] = protected_start() == protected_started_at
            (evidence_directory / "report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
            )
    if report["leftovers"] or not report["protected_llm_unchanged"]:
        raise RuntimeError("the probe did not preserve the required final runtime state")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates-file", type=Path, required=True)
    parser.add_argument("--template-id", required=True)
    parser.add_argument("--served-model-name", required=True)
    parser.add_argument("--ready-timeout-seconds", type=float, default=300)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--file-asr-vad-model", type=Path)
    parser.add_argument("--file-asr-vad-sha256")
    parser.add_argument("--corpus-root", type=Path)
    parser.add_argument("--file-asr-repeat-corpus", action="store_true")
    arguments = parser.parse_args()
    if bool(arguments.file_asr_vad_model) != bool(arguments.file_asr_vad_sha256):
        parser.error("file ASR requires both the VAD model and its expected digest")
    if arguments.corpus_root is not None and arguments.file_asr_vad_model is None:
        parser.error("real corpus probes require the bounded file-ASR/VAD path")
    if arguments.file_asr_repeat_corpus and arguments.corpus_root is None:
        parser.error("repeated corpus measurements require explicit authorized corpus inputs")
    print(json.dumps(run_probe(arguments), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
