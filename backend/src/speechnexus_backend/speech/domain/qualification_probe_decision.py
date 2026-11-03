"""Deterministic checks for raw observations of one controlled TTS probe.

These values do not carry authority by themselves. A future producer must read
identity and stop evidence from their owners, and must never accept them as a
caller-provided success report.
"""

from __future__ import annotations

import io
import re
import wave
from dataclasses import dataclass


class ProbeEvidenceError(ValueError):
    """A fixed, non-sensitive refusal of incomplete or inconsistent evidence."""


_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")


@dataclass(frozen=True, slots=True)
class ProbeIdentity:
    submit_intent_id: str
    binding_revision_id: str
    policy_revision_id: str
    host_node_id: str
    gateway_id: str
    instance_id: str
    instance_generation: int
    voice_revision_id: str
    native_speaker_id: str


@dataclass(frozen=True, slots=True)
class ProbeTerminalEvidence:
    submit_intent_id: str
    evidence_kind: str
    evidence_digest: str
    execution_stopped: bool
    resources_released: bool
    observed_at_us: int


@dataclass(frozen=True, slots=True)
class VerifiedProbeAudio:
    submit_intent_id: str
    samples: int
    sample_rate_hz: int
    stop_evidence_digest: str


def verify_probe_audio_and_terminal(
    *,
    expected: ProbeIdentity,
    observed: ProbeIdentity,
    wav: bytes,
    terminal: ProbeTerminalEvidence,
    generated_samples: int,
    output_ceiling_samples: int,
    maximum_wav_bytes: int,
) -> VerifiedProbeAudio:
    """Verify bounded complete mono PCM and same-execution native stop evidence.

    The owner-specific reader must establish that ``observed`` and ``terminal``
    came from its durable facts and that ``wav`` is the matching private result.
    This function does not grant a lease, verify runtime image or attest source.
    """
    try:
        if (
            expected != observed
            or expected.native_speaker_id != "Vivian"
            or type(expected.instance_generation) is not int
            or expected.instance_generation < 1
            or any(
                not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
                for value in (
                    expected.submit_intent_id,
                    expected.binding_revision_id,
                    expected.policy_revision_id,
                    expected.host_node_id,
                    expected.gateway_id,
                    expected.instance_id,
                    expected.voice_revision_id,
                )
            )
            or terminal.submit_intent_id != expected.submit_intent_id
            or terminal.evidence_kind != "engine_terminal_response"
            or terminal.execution_stopped is not True
            or terminal.resources_released is not True
            or type(terminal.observed_at_us) is not int
            or terminal.observed_at_us <= 0
            or len(terminal.evidence_digest) != 71
            or not terminal.evidence_digest.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in terminal.evidence_digest[7:])
            or type(maximum_wav_bytes) is not int
            or not 44 <= maximum_wav_bytes <= 64 * 1024 * 1024
            or type(wav) is not bytes
            or not 44 <= len(wav) <= maximum_wav_bytes
            or type(generated_samples) is not int
            or type(output_ceiling_samples) is not int
            or not 0 < generated_samples <= output_ceiling_samples
        ):
            raise ValueError
        with wave.open(io.BytesIO(wav), "rb") as stream:
            if (
                stream.getnchannels() != 1
                or stream.getsampwidth() != 2
                or stream.getframerate() != 24000
                or stream.getcomptype() != "NONE"
                or stream.getnframes() != generated_samples
                or len(stream.readframes(generated_samples)) != generated_samples * 2
            ):
                raise ValueError
        return VerifiedProbeAudio(
            expected.submit_intent_id, generated_samples, 24000, terminal.evidence_digest
        )
    except ValueError, TypeError, EOFError, wave.Error, OverflowError:
        raise ProbeEvidenceError("probe_evidence_invalid") from None
