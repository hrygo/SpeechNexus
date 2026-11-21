"""Drive only the already-admitted qualification execution and inspect its evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import cast

from speechnexus_contracts.builtin_voice_qualification import parse as parse_authorization
from speechnexus_contracts.execution_models import SubmitExecutionRequest

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.speech_synthesis import SpeechUnavailableError
from speechnexus_backend.speech.domain.qualification_probe_decision import (
    ProbeIdentity,
    ProbeTerminalEvidence,
    verify_probe_audio_and_terminal,
)
from speechnexus_backend.speech.domain.speech_synthesis import SpeechFormat, SpeechPlan
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.gateway_synthesis import (
    GatewaySynthesizer,
    TtsResultObjects,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.qualification_admission_postgres import (
    PostgresQualificationAdmission,
)
from speechnexus_backend.speech.infrastructure.tts_admission import (
    PostgresOriginalTtsCommands,
    PostgresTtsAdmission,
    TtsFacts,
    TtsRecipe,
    _identities,
)


class ProbeExecutionError(ValueError):
    """The original execution or its terminal evidence is unavailable."""


@dataclass(frozen=True, slots=True)
class VerifiedProbeExecution:
    submit_intent_id: str
    audio_sha256: str
    generated_samples: int
    stop_evidence_digest: str
    terminal_observed_at_us: int


class _ProbeRecovery(PostgresTtsAdmission):
    """Recover a purpose-bound probe without accepting an ordinary TTS intent."""

    def recover(self, request_id: str) -> tuple[SpeechPlan, SubmitExecutionRequest]:
        identity, _, _, _, _ = _identities(request_id)
        original = self._originals.read(identity)
        try:
            if original is None or original.get("kind") != "builtin_voice_qualification_probe":
                raise ValueError
            value = dict(original["plan"])
            value["response_format"] = SpeechFormat(value["response_format"])
            plan = SpeechPlan(**value)
            if plan.request_id != request_id:
                raise ValueError
            # Reuse the exact command/lease reconstruction checks; the actual
            # persisted intent remains probe-tagged and the service query is
            # scoped to the dedicated authorization receipt.
            checked = {**original, "kind": "tts_file"}
            return plan, self._recover(plan, identity, checked)
        except ValueError, TypeError, KeyError:
            raise SpeechUnavailableError("Original probe cannot be recovered") from None


def run_original_probe(
    *,
    pool: SpeechPool,
    port: PostgresQualificationAdmission,
    recipe: TtsRecipe,
    request_id: str,
    gateway: GatewayExecutionClient,
    objects: TtsResultObjects,
    timeout_seconds: float = 120,
) -> VerifiedProbeExecution:
    """Run or recover exactly the persisted admission; never create a replacement.

    GatewaySynthesizer performs original-ID query/replay and reads only the
    gateway's version-pinned result reference. A separate durable owner row is
    required for terminal stop/release; an audio response alone is insufficient.
    """
    adapter = _ProbeRecovery(
        cast(TtsFacts, None),
        AdmissionService(port),
        PostgresOriginalTtsCommands(pool),
        recipe,
        max_command_bytes=1_048_576,
    )
    driver = GatewaySynthesizer(gateway, adapter, objects)
    try:
        plan, original = adapter.recover(request_id)
        audio = driver.recover(request_id, timeout_seconds=timeout_seconds)
    except Exception:
        raise ProbeExecutionError("probe_execution_unavailable") from None
    finally:
        driver.close()
    try:
        with pool.connection(timeout=3) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SELECT set_config('statement_timeout', '3000', true)")
            row = connection.execute(
                "SELECT a.intent, s.submit_intent_id, s.voice_revision_id, s.execution_state, "
                "s.sample_rate_hz, s.generated_pcm_samples, s.hard_output_ceiling_samples, "
                "l.host_node_id, l.gateway_id, l.instance_id, l.instance_generation, "
                "x.binding_revision_id, x.policy_revision_id, e.evidence_kind, e.evidence_digest, "
                "e.execution_stopped, e.resources_released, e.observed_at_us "
                "FROM speech_builtin_voice_probe_admissions p "
                "JOIN speech_builtin_voice_probe_authorizations a USING (authorization_id) "
                "JOIN admission_attempts x ON x.operation_id=p.operation_id "
                "AND x.attempt_id=p.attempt_id "
                "JOIN admission_leases l ON l.operation_id=x.operation_id "
                "AND l.attempt_id=x.attempt_id "
                "JOIN submit_intents s ON s.lease_id=l.lease_id "
                "JOIN execution_stop_evidence e ON e.submit_intent_id=s.submit_intent_id "
                "WHERE p.authorization_id=%s AND s.submit_intent_id=%s",
                (port._authorization_id, original["submit_intent_id"]),
            ).fetchall()
        if len(row) != 1:
            raise ValueError
        value = row[0]
        auth = parse_authorization("BuiltinVoiceQualificationAuthorization", value["intent"])
        if value["execution_state"] != "succeeded" or value["sample_rate_hz"] != 24000:
            raise ValueError
        expected = ProbeIdentity(
            original["submit_intent_id"],
            auth["binding_revision_id"],
            auth["policy_revision_id"],
            auth["host_node_id"],
            auth["gateway_id"],
            auth["instance_id"],
            auth["instance_generation"],
            auth["voice_revision_id"],
            auth["native_speaker_id"],
        )
        observed = ProbeIdentity(
            value["submit_intent_id"],
            value["binding_revision_id"],
            value["policy_revision_id"],
            value["host_node_id"],
            value["gateway_id"],
            value["instance_id"],
            value["instance_generation"],
            value["voice_revision_id"],
            auth["native_speaker_id"],
        )
        terminal = ProbeTerminalEvidence(
            value["submit_intent_id"],
            value["evidence_kind"],
            value["evidence_digest"],
            value["execution_stopped"] == 1,
            value["resources_released"] == 1,
            value["observed_at_us"],
        )
        evidence = verify_probe_audio_and_terminal(
            expected=expected,
            observed=observed,
            wav=audio.payload,
            terminal=terminal,
            generated_samples=value["generated_pcm_samples"],
            output_ceiling_samples=value["hard_output_ceiling_samples"],
            maximum_wav_bytes=plan.max_payload_bytes,
        )
        return VerifiedProbeExecution(
            evidence.submit_intent_id,
            "sha256:" + hashlib.sha256(audio.payload).hexdigest(),
            evidence.samples,
            evidence.stop_evidence_digest,
            terminal.observed_at_us,
        )
    except Exception:
        raise ProbeExecutionError("probe_terminal_evidence_unavailable") from None
