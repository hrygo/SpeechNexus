"""Issue a single probe authorization from independently controlled evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb
from speechnexus_contracts.builtin_voice_qualification import parse

from speechnexus_backend.qualification_probe_artifacts import verify_local_tts_artifacts
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.qualification_native_observation import (
    ExpectedNativeRuntime,
    require_native_ready,
)
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import revision_is_disabled
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    find_binding_revision_document,
    find_evidence,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import find_publication
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import decode_observation
from speechnexus_backend.speech.interfaces.binding_release_evidence import (
    verify_binding_release_evidence,
)


class ProbeAuthorizationError(ValueError):
    """No trusted authorization could be issued."""


def require_live_gateway_policy(settings: Settings, auth: dict[str, Any]) -> None:
    """Query the configured authenticated gateway and compare its live mapping."""

    async def read() -> dict[str, Any]:
        gateway = GatewayExecutionClient.from_settings(settings)
        try:
            return dict(await gateway.query_tts_configuration_evidence())
        finally:
            await gateway.aclose()

    try:
        evidence = asyncio.run(read())
        for key in (
            "gateway_policy_digest",
            "policy_revision_id",
            "voice_id",
            "voice_revision",
            "voice_revision_id",
            "native_speaker_id",
            "binding_revision_id",
            "gateway_id",
            "instance_id",
            "instance_generation",
        ):
            if evidence[key] != auth[key]:
                raise ValueError
        if evidence["node_id"] != auth["host_node_id"]:
            raise ValueError
        observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
        now = datetime.now(UTC)
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= 60:
            raise ValueError
    except Exception:
        raise ProbeAuthorizationError("live_gateway_policy_evidence_unavailable") from None


def _canonical_digest(value: object) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def issue_probe_authorization(
    store: PostgresPublications,
    *,
    settings: Settings,
    intent: dict[str, Any],
    template_document: bytes,
    controller_inventory: bytes,
    pin_path: Path,
    model_root: Path,
    artifact_pin: bytes,
    manifest: bytes,
    report: bytes,
    scenario_evidence: bytes,
) -> str:
    """Verify raw artifacts and locked authorities before inserting a single-use grant.

    Template and controller inventory are trusted operator deployment originals,
    not statements produced by the request. The writer DB role is independent
    from the business and probe-execution roles.
    """
    require_live_gateway_policy(settings, intent)
    try:
        auth = parse("BuiltinVoiceQualificationAuthorization", intent)
        template = json.loads(template_document)
        controller = json.loads(controller_inventory)
        t = template["template"]
        if (
            template["format"] != "speechnexus-template-v1"
            or controller["format"] != "speechnexus-controller-python-v1"
            or _canonical_digest(template) != auth["template_digest"]
            or _canonical_digest(controller) != auth["controller_artifact_digest"]
            or t["template_id"] != auth["template_id"]
            or t["image_digest"] != auth["image_digest"]
            or t["engine_auth"] != "vllm_api_key_v1"
        ):
            raise ValueError
        pin_digest = "sha256:" + hashlib.sha256(artifact_pin).hexdigest()
        artifacts = verify_local_tts_artifacts(
            pin_path=pin_path, model_root=model_root, expected_pin_digest=pin_digest
        )
        if (
            artifacts.model_artifact_digest != auth["model_artifact_digest"]
            or artifacts.expected_image_digest != auth["image_digest"]
        ):
            raise ValueError
    except Exception:
        raise ProbeAuthorizationError("probe_authorization_unverified") from None

    def operation(cursor: Any) -> str:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:'||current_schema()||':binding:'||%s,0))",
            (auth["binding_id"],),
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:'||current_schema()||':voice:'||%s,0))",
            (auth["voice_id"],),
        )
        cursor.execute(
            "SELECT current_database() AS db,current_schema() AS schema,"
            "installation_id::text AS installation_id FROM speech_publication_installation "
            "WHERE singleton IS TRUE"
        )
        targets = cursor.fetchall()
        if len(targets) != 1 or (
            targets[0]["db"],
            targets[0]["schema"],
            targets[0]["installation_id"],
        ) != (auth["database"], auth["schema"], auth["installation_id"]):
            raise ProbeAuthorizationError("probe_authorization_unverified")
        cursor.execute(
            "SELECT intent FROM speech_builtin_voice_probe_authorizations "
            "WHERE authorization_id=%s",
            (auth["authorization_id"],),
        )
        existing = cursor.fetchall()
        if existing:
            if len(existing) != 1 or existing[0]["intent"] != auth:
                raise ProbeAuthorizationError("probe_authorization_conflict")
            return "replayed"
        cursor.execute(
            "SELECT s.source_approval_id FROM speech_builtin_voice_source_approvals s "
            "WHERE s.source_approval_id=%s AND s.voice_id=%s "
            "AND s.native_speaker_id='Vivian' AND s.expires_at>clock_timestamp() "
            "AND s.expires_at >= to_timestamp(%s / 1000000.0) "
            "AND NOT EXISTS (SELECT 1 FROM speech_builtin_voice_source_revocations r "
            "WHERE r.source_approval_id=s.source_approval_id)",
            (auth["source_approval_id"], auth["voice_id"], auth["expires_at_us"]),
        )
        if len(cursor.fetchall()) != 1:
            raise ProbeAuthorizationError("probe_authorization_unverified")
        cursor.execute(
            "SELECT revision FROM speech_binding_revisions WHERE binding_id=%s AND revision_id=%s",
            (auth["binding_id"], auth["binding_revision_id"]),
        )
        revisions = cursor.fetchall()
        if len(revisions) != 1:
            raise ProbeAuthorizationError("probe_authorization_unverified")
        revision = int(revisions[0]["revision"])
        if revision_is_disabled(cursor, auth["binding_id"], auth["binding_revision_id"]):
            raise ProbeAuthorizationError("probe_authorization_unverified")
        publication = find_publication(cursor, "binding", auth["binding_id"], revision)
        evidence = find_evidence(cursor, auth["binding_id"], revision)
        definition = find_binding_revision_document(cursor, auth["binding_id"], revision)
        if publication is None or evidence is None or definition is None:
            raise ProbeAuthorizationError("probe_authorization_unverified")
        verified = verify_binding_release_evidence(
            publication=publication,
            recorded_evidence=evidence,
            binding_definition=definition[1].encode(),
            artifact_pin=artifact_pin,
            manifest=manifest,
            report=report,
            scenario_evidence=scenario_evidence,
            now=datetime.now(UTC),
        )
        if (
            (
                verified.manifest_id,
                verified.report_id,
                verified.manifest_digest,
                verified.report_digest,
            )
            != (
                auth["manifest_id"],
                auth["report_id"],
                auth["manifest_digest"],
                auth["report_digest"],
            )
            or verified.expires_at is None
            or auth["expires_at_us"] > int(verified.expires_at.timestamp() * 1_000_000)
            or not any(
                p.model_digest == auth["model_artifact_digest"]
                and p.image_digest == auth["image_digest"]
                for p in verified.stage_pins
            )
        ):
            raise ProbeAuthorizationError("probe_authorization_unverified")
        cursor.execute(
            "SELECT r.observation_document FROM runtime_instance_heads h "
            "JOIN runtime_instance_intents r ON r.instance_id=h.instance_id AND "
            "r.instance_generation=h.current_generation "
            "AND r.intent_id=h.current_prepare_intent_id "
            "AND r.target_node_id=%s JOIN engine_instances i ON i.instance_id=h.instance_id "
            "AND i.instance_generation=h.current_generation WHERE h.instance_id=%s "
            "AND h.current_generation=%s AND r.action='prepare' AND r.intent_state='succeeded' "
            "AND i.instance_state='ready' FOR UPDATE OF h,i",
            (auth["host_node_id"], auth["instance_id"], auth["instance_generation"]),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise ProbeAuthorizationError("probe_authorization_unverified")
        observed = decode_observation(rows[0]["observation_document"]).observation
        binding = observed.binding
        if (
            binding.target_node_id,
            binding.gateway_id,
            binding.approved_template_id,
            binding.binding_revision_id,
            binding.manifest_id,
            binding.config_digest,
        ) != (
            auth["host_node_id"],
            auth["gateway_id"],
            auth["template_id"],
            auth["binding_revision_id"],
            auth["manifest_id"],
            auth["template_config_digest"],
        ):
            raise ProbeAuthorizationError("probe_authorization_unverified")
        cursor.execute(
            "SELECT floor(extract(epoch from clock_timestamp())*1000000)::bigint AS now_us"
        )
        now_us = cursor.fetchall()[0]["now_us"]
        require_native_ready(
            observed,
            ExpectedNativeRuntime(
                binding,
                auth["instance_id"],
                auth["instance_generation"],
                observed.fencing_token,
                auth["image_digest"],
                auth["template_digest"],
                auth["controller_artifact_digest"],
            ),
            now_us=now_us,
            max_age_us=300_000_000,
        )
        if auth["expires_at_us"] <= now_us:
            raise ProbeAuthorizationError("probe_authorization_unverified")
        cursor.execute(
            "INSERT INTO speech_builtin_voice_probe_authorizations "
            "(authorization_id,source_approval_id,voice_id,intent) VALUES (%s,%s,%s,%s)",
            (auth["authorization_id"], auth["source_approval_id"], auth["voice_id"], Jsonb(auth)),
        )
        return "committed"

    return store._run(operation, preserve=(ProbeAuthorizationError,))
