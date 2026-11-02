"""Private, authorized probe admission using the ordinary atomic budget transaction."""

from __future__ import annotations

import json
from typing import Any, cast

from speechnexus_contracts.builtin_voice_qualification import parse as parse_authorization
from speechnexus_contracts.execution import parse

from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRejectedError,
    AdmissionResult,
    unknown,
)
from speechnexus_backend.speech.domain.qualification_native_observation import (
    ExpectedNativeRuntime,
    require_native_ready,
)
from speechnexus_backend.speech.infrastructure.admission_codec import PreparedAdmission
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.admission_recovery_sql import read_original
from speechnexus_backend.speech.infrastructure.admission_sql import admit_in_transaction
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import (
    lock_binding,
    revision_is_disabled,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import _DeadlineCursor
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import decode_observation
from speechnexus_backend.speech.infrastructure.transcription_submit import (
    command_digest,
    deadline_text,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_sql import insert_tts_settlement

_PROBE_TEXT = "SpeechNexus qualification probe."


class PostgresQualificationAdmission(PostgresAdmissionTransactions):
    """Only a separate qualification operator may use this port.

    Ordinary TTS admission never calls it. A per-command stored authorization is
    independently approved before this method can reserve any resource.
    """

    def __init__(self, *args: Any, authorization_id: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._authorization_id = authorization_id

    def _record_settlement(self, cursor: _DeadlineCursor, prepared: PreparedAdmission) -> None:
        # _admit already verified the exact authorization and command on this
        # transaction; replay is idempotently checked by the shared helper.
        body = json.loads(prepared.command.business_command.document)
        request = parse("SubmitExecutionRequest", body["gateway_request"])
        insert_tts_settlement(
            cursor, request=request, deadline_at_us=prepared.command.request.deadline_at_us
        )

    def query_admission(
        self, identity: AdmissionIdentity, *, request_digest: str | None = None
    ) -> AdmissionResult:
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._timeout * 1000))),),
                )
                receipt = connection.execute(
                    "SELECT r.operation_id, r.attempt_id "
                    "FROM speech_builtin_voice_probe_admissions r "
                    "JOIN speech_builtin_voice_probe_authorizations a USING (authorization_id) "
                    "CROSS JOIN speech_publication_installation i "
                    "WHERE r.authorization_id=%s AND i.singleton IS TRUE "
                    "AND a.intent->>'database'=current_database() "
                    "AND a.intent->>'schema'=current_schema() "
                    "AND a.intent->>'installation_id'=i.installation_id::text",
                    (self._authorization_id,),
                ).fetchone()
            if receipt is None or (receipt["operation_id"], receipt["attempt_id"]) != (
                identity.operation_id,
                identity.attempt_id,
            ):
                return unknown(identity)
        except Exception:
            return unknown(identity)
        return super().query_admission(identity, request_digest=request_digest)

    def _admit(self, cursor: _DeadlineCursor, prepared: PreparedAdmission) -> AdmissionResult:
        request = prepared.command.request
        cursor.execute(
            "SELECT r.operation_id, r.attempt_id, r.request_digest "
            "FROM speech_builtin_voice_probe_admissions r "
            "JOIN speech_builtin_voice_probe_authorizations a USING (authorization_id) "
            "CROSS JOIN speech_publication_installation i "
            "WHERE r.authorization_id=%s AND i.singleton IS TRUE "
            "AND a.intent->>'database'=current_database() AND a.intent->>'schema'=current_schema() "
            "AND a.intent->>'installation_id'=i.installation_id::text",
            (self._authorization_id,),
        )
        receipts = cursor.fetchall()
        if receipts:
            receipt = receipts[0]
            if (receipt["operation_id"], receipt["attempt_id"], receipt["request_digest"]) != (
                request.identity.operation_id,
                request.identity.attempt_id,
                prepared.request_digest,
            ):
                raise AdmissionRejectedError("idempotency_conflict")
            original = read_original(
                cursor, request.identity, max_command_bytes=self._max_bytes, prepared=prepared
            )
            if original is None:
                raise AdmissionRejectedError("reconciliation_required")
            return original
        if (
            read_original(
                cursor, request.identity, max_command_bytes=self._max_bytes, prepared=prepared
            )
            is not None
        ):
            raise AdmissionRejectedError("idempotency_conflict")
        cursor.execute(
            "SELECT intent FROM speech_builtin_voice_probe_authorizations "
            "WHERE authorization_id=%s",
            (self._authorization_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise AdmissionRejectedError("policy_or_resource_disabled")
        try:
            auth = parse_authorization("BuiltinVoiceQualificationAuthorization", rows[0]["intent"])
            body = json.loads(prepared.command.business_command.document)
            gateway = cast(dict[str, Any], parse("SubmitExecutionRequest", body["gateway_request"]))
            command = cast(dict[str, Any], gateway["command"])
            stage = request.stages[0]
            voice = command["resources"]["voice"]
            params = command["parameters"]
            text = command["text"]
            if (
                len(request.stages) != 1
                or len(request.authorities) != 1
                or stage.reserved_slots != 1
                or stage.stage_id != gateway["stage_id"]
                or body.get("kind") != "builtin_voice_qualification_probe"
                or body["plan"]["gateway_policy_digest"] != auth["gateway_policy_digest"]
                or body["plan"]["native_speaker_id"] != auth["native_speaker_id"]
                or command["kind"] != "tts"
                or command["task_kind"] != "tts_file"
                or command["stage_role"] != "tts_connector"
                or auth["purpose"] != "builtin_voice_qualification"
                or auth["authorization_id"] != self._authorization_id
                or auth["native_speaker_id"] != "Vivian"
                or auth["native_authority"] is not True
                or gateway["command_digest"] != command_digest(cast(dict[str, object], command))
                or gateway["command_digest"] != auth["command_digest"]
                or command_digest(cast(dict[str, object], gateway))
                != auth["gateway_request_digest"]
                or prepared.request_digest != auth["admission_request_digest"]
                or prepared.command.business_command.request_digest
                != auth["business_request_digest"]
                or voice != {"id": auth["voice_id"], "revision": auth["voice_revision"]}
                or gateway["voice_revision_id"] != auth["voice_revision_id"]
                or command["resources"]["voice_kind"] != "builtin"
                or command["resources"]["voice_material"] is not None
                or command["resources"]["pronunciation"]
                or command["resources"]["vocabulary"]
                or command["resources"]["pronunciation_source"] is not None
                or command["resources"]["vocabulary_source"] is not None
                or text != _PROBE_TEXT
                or command["text_stream"] is not None
                or params["response_format"] != "wav"
                or params["audio_delivery"] != "complete"
                or params["text_commit_mode"] != "explicit"
                or params["language"] != "en"
                or params["pronunciation_revision"] is not None
                or params["instructions"] is not None
                or params["speed"] != 1
                or params["seed"] is not None
                or params["planner"]["max_chars"] > 400
                or len(text) > params["planner"]["max_chars"]
                or gateway["sample_rate_hz"] != 24000
                or not 1 <= gateway["max_output_audio_ms"] <= 10000
                or gateway["deadline_at"] != deadline_text(request.deadline_at_us)
                or (request.identity.operation_id, request.identity.attempt_id)
                != (auth["operation_id"], auth["attempt_id"])
                or (request.binding_revision_id, request.manifest_id, request.policy_revision_id)
                != (auth["binding_revision_id"], auth["manifest_id"], auth["policy_revision_id"])
                or (
                    stage.host_node_id,
                    stage.gateway_id,
                    stage.instance_id,
                    stage.instance_generation,
                )
                != (
                    auth["host_node_id"],
                    auth["gateway_id"],
                    auth["instance_id"],
                    auth["instance_generation"],
                )
                or gateway["workspace_id"] != request.workspace_id
                or gateway["fencing_token"] != request.authorities[0].fencing_token
                or any(
                    gateway[k] != getattr(stage, k)
                    for k in (
                        "stage_id",
                        "lease_id",
                        "pool_id",
                        "host_node_id",
                        "gateway_id",
                        "instance_id",
                        "instance_generation",
                    )
                )
                or gateway["binding_revision_id"] != request.binding_revision_id
                or gateway["policy_revision_id"] != request.policy_revision_id
                or gateway["operation_id"] != request.identity.operation_id
                or gateway["attempt_id"] != request.identity.attempt_id
            ):
                raise ValueError
        except ValueError, TypeError, KeyError, IndexError, AttributeError:
            raise AdmissionRejectedError("policy_or_resource_disabled") from None
        cursor.execute(
            "SELECT current_database() AS database, current_schema() AS schema, "
            "installation_id::text AS installation_id FROM speech_publication_installation "
            "WHERE singleton IS TRUE"
        )
        targets = cursor.fetchall()
        if len(targets) != 1 or targets[0] != {
            "database": auth["database"],
            "schema": auth["schema"],
            "installation_id": auth["installation_id"],
        }:
            raise AdmissionRejectedError("policy_or_resource_disabled")
        lock_binding(cursor, auth["binding_id"])
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':voice:' || %s, 0))",
            (auth["voice_id"],),
        )
        # Recheck single-use and the original after waiting for the common locks.
        cursor.execute(
            "SELECT operation_id FROM speech_builtin_voice_probe_admissions "
            "WHERE authorization_id=%s",
            (self._authorization_id,),
        )
        if cursor.fetchall():
            return self._admit(cursor, prepared)
        if revision_is_disabled(cursor, auth["binding_id"], request.binding_revision_id):
            raise AdmissionRejectedError("policy_or_resource_disabled")
        cursor.execute(
            "SELECT 1 FROM speech_builtin_voice_probe_authorizations a "
            "JOIN speech_builtin_voice_source_approvals s "
            "ON s.source_approval_id=a.source_approval_id AND s.voice_id=a.voice_id "
            "JOIN speech_binding_revisions b ON b.binding_id=%s AND b.revision_id=%s "
            "JOIN speech_binding_publication_evidence e ON e.object_id=b.binding_id "
            "AND e.revision=b.revision AND e.revision_id=b.revision_id "
            "JOIN speech_publications p ON p.kind='binding' AND p.object_id=b.binding_id "
            "AND p.revision=b.revision AND p.revision_id=b.revision_id "
            "WHERE a.authorization_id=%s AND a.voice_id=%s AND a.source_approval_id=%s "
            "AND s.expires_at > clock_timestamp() AND s.native_speaker_id='Vivian' "
            "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_source_revocations r "
            "WHERE r.source_approval_id=s.source_approval_id) "
            "AND e.capacity_manifest_id=%s AND e.qualification_report_id=%s "
            "AND e.capacity_manifest_digest=%s AND e.qualification_report_digest=%s "
            "AND EXISTS (SELECT 1 FROM engine_instances i WHERE i.host_node_id=%s "
            "AND i.gateway_id=%s AND i.instance_id=%s AND i.instance_generation=%s "
            "AND i.binding_revision_id=%s AND i.manifest_id=%s AND i.approved_template_id=%s "
            "AND i.config_digest=%s) "
            "AND floor(extract(epoch FROM clock_timestamp())*1000000)::bigint < %s",
            (
                auth["binding_id"],
                request.binding_revision_id,
                self._authorization_id,
                auth["voice_id"],
                auth["source_approval_id"],
                auth["manifest_id"],
                auth["report_id"],
                auth["manifest_digest"],
                auth["report_digest"],
                auth["host_node_id"],
                auth["gateway_id"],
                auth["instance_id"],
                auth["instance_generation"],
                auth["binding_revision_id"],
                auth["manifest_id"],
                auth["template_id"],
                auth["template_config_digest"],
                auth["expires_at_us"],
            ),
        )
        if len(cursor.fetchall()) != 1:
            raise AdmissionRejectedError("policy_or_resource_disabled")
        result = admit_in_transaction(
            cursor, prepared, clock=cursor.clock, max_command_bytes=self._max_bytes
        )
        if result.outcome != "committed":
            # A probe replay must already have its own receipt; an unrelated
            # admission that raced our earlier check must not acquire one.
            raise AdmissionRejectedError("idempotency_conflict")
        # The ordinary transaction has now locked and validated the current
        # instance, head, original prepare intent, readiness and pool fencing.
        # Bind the separate native evidence to those same locked facts.
        cursor.execute(
            "SELECT r.observation_document FROM runtime_instance_heads h "
            "JOIN runtime_instance_intents r ON r.instance_id=h.instance_id "
            "AND r.instance_generation=h.current_generation "
            "AND r.intent_id=h.current_prepare_intent_id AND r.target_node_id=%s "
            "WHERE h.instance_id=%s AND h.current_generation=%s "
            "AND r.action='prepare' AND r.intent_state='succeeded'",
            (auth["host_node_id"], auth["instance_id"], auth["instance_generation"]),
        )
        observations = cursor.fetchall()
        try:
            if len(observations) != 1:
                raise ValueError
            observation = decode_observation(observations[0]["observation_document"]).observation
            binding = observation.binding
            if (
                binding.target_node_id != auth["host_node_id"]
                or binding.gateway_id != auth["gateway_id"]
                or binding.approved_template_id != auth["template_id"]
                or binding.binding_revision_id != auth["binding_revision_id"]
                or binding.manifest_id != auth["manifest_id"]
                or binding.config_digest != auth["template_config_digest"]
            ):
                raise ValueError
            require_native_ready(
                observation,
                ExpectedNativeRuntime(
                    binding,
                    auth["instance_id"],
                    auth["instance_generation"],
                    request.authorities[0].fencing_token,
                    auth["image_digest"],
                    auth["template_digest"],
                    auth["controller_artifact_digest"],
                ),
                now_us=cursor.clock(),
                max_age_us=300_000_000,
            )
        except ValueError, TypeError, KeyError:
            raise AdmissionRejectedError("policy_or_resource_disabled") from None
        cursor.execute(
            "INSERT INTO speech_builtin_voice_probe_admissions "
            "(authorization_id,operation_id,attempt_id,request_digest) VALUES (%s,%s,%s,%s)",
            (
                self._authorization_id,
                request.identity.operation_id,
                request.identity.attempt_id,
                prepared.request_digest,
            ),
        )
        return result
