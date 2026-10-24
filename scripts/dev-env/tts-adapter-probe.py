"""Real CustomVoice synthesis through the committed TTS adapter.

The probe starts one fixed private container per stage, drives the committed
``VllmTtsSynthesizer``/``LocalTtsEncoder`` for a WAV case and an
instructions/PCM case, decodes the delivered WAV back to 16 kHz, then
round-trips it through the committed ASR adapter against a second private
container. It records container and network cleanup plus the protected LLM
start time, and never writes model paths, credentials, request text or audio
bytes into the report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from engine_probe_support import protected_start

from speechnexus_gateway.application.file_tts import FileTtsSynthesis
from speechnexus_gateway.domain.asr import AsrAudio, AsrPolicy, AsrRecognizer
from speechnexus_gateway.domain.media import AudioDecodeLimits, AudioSourceFormat
from speechnexus_gateway.domain.tts import (
    TtsDeliveryFormat,
    TtsEngineFormat,
    TtsPolicy,
    TtsRequest,
    TtsSynthesizer,
)
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.infrastructure.vllm_asr import (
    VllmAsrEndpoint,
    VllmAsrRecognizer,
)
from speechnexus_gateway.infrastructure.vllm_tts import (
    VllmTtsEndpoint,
    VllmTtsSynthesizer,
)

DEFAULT_TEXT = "这是一次真实的语音合成验证，用来确认引擎可以返回完整音频。"
TTS_BINDING = "binding-tts-customvoice-1p7b"
ASR_BINDING = "binding-asr-0p6b-roundtrip"
PORT = 8000
_CASES = (
    ("wav", TtsEngineFormat.WAV, TtsDeliveryFormat.WAV, ""),
    ("pcm_mp3", TtsEngineFormat.PCM, TtsDeliveryFormat.MP3, "语速平稳"),
)


def _docker(
    *arguments: str, timeout: float = 120.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], check=check, capture_output=True, text=True, timeout=timeout
    )


def _docker_json(*arguments: str) -> Any:
    return json.loads(_docker(*arguments).stdout)


def _image_id(reference: str) -> str:
    return str(_docker_json("image", "inspect", reference)[0]["Id"])


def _container_names() -> set[str]:
    return set(_docker("ps", "-a", "--format", "{{.Names}}").stdout.split())


def _network_names() -> set[str]:
    return set(_docker("network", "ls", "--format", "{{.Name}}").stdout.split())


def _create_internal_network(name: str) -> None:
    _docker("network", "create", "--internal", "--driver", "bridge", name)
    network = _docker_json("network", "inspect", name)[0]
    if not network.get("Internal") or network.get("Driver") != "bridge":
        raise RuntimeError("the probe network is not an internal bridge")


def _start_container(
    *,
    name: str,
    network: str,
    image: str,
    entrypoint: str,
    command: list[str],
    model_source: Path,
    model_target: str,
    memory: str,
    environment: dict[str, str] | None = None,
) -> None:
    arguments = [
        "run",
        "-d",
        "--name",
        name,
        "--gpus",
        "all",
        "--network",
        network,
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--entrypoint",
        entrypoint,
        "-v",
        f"{model_source}:{model_target}:ro",
    ]
    for key, value in (environment or {}).items():
        arguments.extend(["-e", f"{key}={value}"])
    arguments.extend([image, *command])
    _docker(*arguments, timeout=300.0)


def _container_address(name: str, network: str) -> str:
    inspected = _docker_json("inspect", name)[0]
    address = inspected["NetworkSettings"]["Networks"][network]["IPAddress"]
    if not address:
        raise RuntimeError("the probe container has no address on its private network")
    return str(address)


def _wait_health(base_url: str, *, timeout_seconds: float) -> float:
    started = time.monotonic()
    deadline = started + timeout_seconds
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(base_url + "/health")
            except httpx.HTTPError:
                time.sleep(2.0)
                continue
            if response.status_code == 200:
                return time.monotonic() - started
            time.sleep(2.0)
    raise TimeoutError("the probe container never reported healthy")


def _stop_container(name: str) -> dict[str, object]:
    started = time.monotonic()
    _docker("stop", "--time", "30", name, timeout=180.0)
    stopped_seconds = time.monotonic() - started
    state = _docker_json("inspect", name)[0]["State"]
    if state["Running"] or state["Status"] != "exited":
        raise RuntimeError("the probe container did not exit")
    evidence = {
        "stop_seconds": round(stopped_seconds, 3),
        "status": str(state["Status"]),
        "exit_code": int(state["ExitCode"]),
        "running": bool(state["Running"]),
    }
    _docker("rm", name)
    if name in _container_names():
        raise RuntimeError("the probe container was not removed")
    return evidence


def _cleanup_container(name: str) -> None:
    if name in _container_names():
        _stop_container(name)


def _decode_16k(wav_bytes: bytes) -> tuple[bytes, dict[str, object]]:
    stream = PyAvAudioDecoder().open(
        io.BytesIO(wav_bytes),
        source_format=AudioSourceFormat("audio/wav"),
        limits=AudioDecodeLimits(),
        output_sample_rate_hz=16000,
        cancelled=lambda: False,
    )
    blocks: list[bytes] = []
    try:
        for block in stream:
            blocks.append(block.pcm16)
        summary = stream.summary
    finally:
        stream.close()
    if summary is None:
        raise RuntimeError("the delivered audio did not decode completely")
    return b"".join(blocks), {
        "source_sample_rate_hz": summary.clock.sample_rate_hz,
        "source_samples": summary.source_samples,
        "output_samples": summary.output_samples,
        "duration_ms": summary.duration_ms,
    }


_IGNORED = frozenset("，。,.!?！？、；;：:")


def _normalize(text: str) -> str:
    return "".join(
        character for character in text if not character.isspace() and character not in _IGNORED
    )


def _char_error_rate(expected: str, observed: str) -> float:
    left, right = _normalize(expected), _normalize(observed)
    row = list(range(len(right) + 1))
    for index, character in enumerate(left, start=1):
        previous, row = row, [index]
        for position, candidate in enumerate(right, start=1):
            row.append(
                min(
                    previous[position] + 1,
                    row[-1] + 1,
                    previous[position - 1] + (character != candidate),
                )
            )
    return round(row[-1] / max(len(left), 1), 4)


async def _synthesize(
    *,
    synthesizer: TtsSynthesizer,
    text: str,
    policy: TtsPolicy,
    delivery_format: TtsDeliveryFormat,
    execution_id: str,
) -> tuple[dict[str, object], bytes]:
    synthesis = FileTtsSynthesis(
        synthesizer=synthesizer,
        encoder=LocalTtsEncoder(),
        request=TtsRequest("request-" + execution_id, text),
        policy=policy,
        delivery_format=delivery_format,
        max_payload_bytes=32 * 1024 * 1024,
        cancelled=lambda: False,
        execution_id=execution_id,
    )
    started = time.monotonic()
    result = await synthesis.run()
    facts: dict[str, object] = {
        "engine_media_type": result.engine_media_type,
        "pcm_bytes": result.pcm_bytes,
        "duration_ms": result.duration_ms,
        "sample_rate_hz": result.sample_rate_hz,
        "delivery_format": result.delivery.format.value,
        "delivery_media_type": result.delivery.media_type,
        "delivery_bytes": len(result.delivery.payload),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "source_pcm_digest": result.delivery.source_pcm_digest,
    }
    return facts, result.delivery.payload


async def _roundtrip(*, recognizer: AsrRecognizer, pcm16: bytes, text: str) -> dict[str, object]:
    started = time.monotonic()
    recognized = await recognizer.transcribe(
        AsrAudio("request-tts-roundtrip-1", pcm16),
        AsrPolicy("config-asr-roundtrip-1"),
        cancelled=lambda: False,
    )
    return {
        "binding_revision_id": recognizer.binding_revision_id,
        "seconds": round(time.monotonic() - started, 3),
        "char_error_rate": _char_error_rate(text, recognized.text),
        "exact_match": _normalize(text) == _normalize(recognized.text),
        "observed_chars": len(_normalize(recognized.text)),
    }


def run_probe(arguments: argparse.Namespace) -> dict[str, object]:
    text = (
        arguments.text_file.read_text(encoding="utf-8").strip()
        if arguments.text_file
        else arguments.text
    )
    suffix = uuid4().hex[:12]
    network = f"speechnexus-tts-adapter-{suffix}"
    evidence = arguments.evidence_directory / suffix
    evidence.mkdir(parents=True, exist_ok=False)
    tts_container = f"nexus-tts-adapter-{suffix}"
    asr_container = f"nexus-tts-roundtrip-{suffix}"
    report: dict[str, object] = {
        "internal_network": True,
        "request_text_recorded": False,
        "model_paths_recorded": False,
        "tts_image": _image_id(arguments.tts_image),
        "asr_image": _image_id(arguments.asr_image),
        "tts_served_model": arguments.tts_served_model,
        "asr_served_model": arguments.asr_served_model,
    }
    protected_before = protected_start()
    report["protected_llm_started_at"] = protected_before
    delivered: dict[TtsDeliveryFormat, bytes] = {}
    cases: dict[str, object] = {}
    _create_internal_network(network)
    try:
        _start_container(
            name=tts_container,
            network=network,
            image=arguments.tts_image,
            entrypoint="vllm-omni",
            command=[
                "serve",
                "/models/tts",
                "--omni",
                "--served-model-name",
                arguments.tts_served_model,
                "--host",
                "0.0.0.0",
                "--port",
                str(PORT),
                "--gpu-memory-utilization",
                str(arguments.tts_gpu_memory_utilization),
                "--max-model-len",
                str(arguments.tts_max_model_len),
                "--disable-uvicorn-access-log",
            ],
            model_source=arguments.tts_model_dir,
            model_target="/models/tts",
            memory=arguments.tts_memory,
            environment={"VLLM_USE_V2_MODEL_RUNNER": "0"},
        )
        address = _container_address(tts_container, network)
        report["tts_ready_seconds"] = round(
            _wait_health(
                f"http://{address}:{PORT}", timeout_seconds=arguments.ready_timeout_seconds
            ),
            3,
        )
        synthesizer = VllmTtsSynthesizer(
            VllmTtsEndpoint(address, PORT, arguments.tts_served_model, TTS_BINDING)
        )
        for label, engine_format, delivery_format, instructions in _CASES:
            policy = TtsPolicy(
                f"config-tts-{label}-1",
                arguments.voice,
                instructions=instructions,
                engine_format=engine_format,
                sample_rate_hz=24000,
            )
            facts, payload = asyncio.run(
                _synthesize(
                    synthesizer=synthesizer,
                    text=text,
                    policy=policy,
                    delivery_format=delivery_format,
                    execution_id=f"execution-tts-{label}-1",
                )
            )
            cases[label] = facts
            delivered[delivery_format] = payload
        report["cases"] = cases
        report["tts_stop"] = _stop_container(tts_container)

        wav_bytes = delivered[TtsDeliveryFormat.WAV]
        (evidence / "speech-wav.wav").write_bytes(wav_bytes)
        (evidence / "speech-pcm_mp3.mp3").write_bytes(delivered[TtsDeliveryFormat.MP3])
        pcm16, decode_facts = _decode_16k(wav_bytes)
        report["decode_24k_to_16k"] = decode_facts
        _start_container(
            name=asr_container,
            network=network,
            image=arguments.asr_image,
            entrypoint="vllm",
            command=[
                "serve",
                "/models/asr",
                "--served-model-name",
                arguments.asr_served_model,
                "--dtype",
                "bfloat16",
                "--gpu-memory-utilization",
                str(arguments.asr_gpu_memory_utilization),
                "--max-num-seqs",
                "1",
                "--max-model-len",
                "8192",
                "--limit-mm-per-prompt",
                '{"audio":1}',
                "--enforce-eager",
                "--host",
                "0.0.0.0",
                "--port",
                str(PORT),
                "--enable-request-id-headers",
                "--no-enable-log-requests",
                "--no-enable-log-outputs",
                "--disable-uvicorn-access-log",
                "--disable-log-stats",
            ],
            model_source=arguments.asr_model_dir,
            model_target="/models/asr",
            memory=arguments.asr_memory,
        )
        asr_address = _container_address(asr_container, network)
        report["asr_ready_seconds"] = round(
            _wait_health(
                f"http://{asr_address}:{PORT}", timeout_seconds=arguments.ready_timeout_seconds
            ),
            3,
        )
        recognizer = VllmAsrRecognizer(
            VllmAsrEndpoint(asr_address, PORT, arguments.asr_served_model, ASR_BINDING)
        )
        report["asr_roundtrip"] = asyncio.run(
            _roundtrip(recognizer=recognizer, pcm16=pcm16, text=text)
        )
        report["asr_stop"] = _stop_container(asr_container)
    finally:
        _cleanup_container(tts_container)
        _cleanup_container(asr_container)
        remaining_networks = _network_names()
        if network in remaining_networks:
            _docker("network", "rm", network)
    report["cleanup"] = {
        "containers_left": len({tts_container, asr_container} & _container_names()),
        "networks_left": len({network} & _network_names()),
        "protected_llm_unchanged": protected_start() == protected_before,
    }
    report["evidence_sha256"] = {
        name: "sha256:" + hashlib.sha256((evidence / name).read_bytes()).hexdigest()
        for name in ("speech-wav.wav", "speech-pcm_mp3.mp3")
    }
    (evidence / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tts-image", default="vllm/vllm-omni:latest")
    parser.add_argument("--asr-image", default="local/qwen3-asr-vllm:0.28.0-arm64")
    parser.add_argument("--tts-model-dir", type=Path, required=True)
    parser.add_argument("--asr-model-dir", type=Path, required=True)
    parser.add_argument("--tts-served-model", default="qwen3-tts-1p7b-custom")
    parser.add_argument("--asr-served-model", default="qwen3-asr-0p6b")
    parser.add_argument("--voice", default="Vivian")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--text-file", type=Path, default=None)
    parser.add_argument("--tts-memory", default="16g")
    parser.add_argument("--asr-memory", default="10g")
    parser.add_argument("--tts-gpu-memory-utilization", type=float, default=0.06)
    parser.add_argument("--asr-gpu-memory-utilization", type=float, default=0.05)
    parser.add_argument("--tts-max-model-len", type=int, default=4096)
    parser.add_argument("--ready-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(run_probe(arguments), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
