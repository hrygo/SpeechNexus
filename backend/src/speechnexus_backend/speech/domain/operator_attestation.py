"""Operator attestation domain: the trust declaration and the claims it authorizes.

Nothing here reads or writes storage. Every rejection is a stable code so the
operator command can name the exact missing or disagreeing declaration instead of
returning one generic failure. These values never become runtime evidence: a
qualification derived from them still cannot satisfy ``require_native_ready``,
settle a lease, release budget or converge an instance generation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}\Z")
_SCHEMA = re.compile(r"[a-z][a-z0-9_]{2,62}\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)

TRUST_DECLARATION_FORMAT = "speechnexus-operator-attested-engine-v1"
ATTESTATION_REQUEST_FORMAT = "speechnexus-operator-attested-qualification-v1"
TEMPLATE_FORMAT = "speechnexus-template-v1"
LIFECYCLE_FORMAT = "speechnexus-external-lifecycle-v1"

OPERATOR_VERIFIER_ID = "operator-attested-customvoice-v1"
OPERATOR_VERIFIER_VERSION = "1"

MAX_DECLARATION_VALIDITY_SECONDS = 2_592_000
"""Thirty days: an attestation is a bounded statement, never an enduring one."""

REQUIRED_GAPS = frozenset(
    {
        "no_controller_observation",
        "no_native_runtime_evidence",
        "no_controller_stop_evidence",
        "no_budget_release_by_attestation",
        "no_lifecycle_convergence_by_attestation",
    }
)
"""Every declaration must acknowledge what this path does not provide."""

OPERATOR_CHECKS = frozenset(
    {
        "source_approved",
        "artifact_integrity",
        "engine_identity",
        "voice_speaker_binding",
        "declared_capabilities",
        "instance_registration",
        "attestation_window",
        "gateway_policy",
    }
)
"""The declared checks this path actually performs; distinct from the probe set."""

_CAPABILITY_KEYS = ("languages", "audio_formats", "sample_rates_hz", "modes")
_DOMAIN_KEYS = {
    "languages": "languages",
    "audio_formats": "audio_formats",
    "sample_rates_hz": "sample_rates_hz",
    "modes": "audio_delivery_modes",
}


class OperatorAttestationError(ValueError):
    """A classified refusal that names the missing or disagreeing declaration."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_digest(value: object) -> str:
    """The digest form the qualification paths use for trusted operator originals.

    Byte-for-byte the same canonicalization as ``qualification_probe_authorizer``:
    a reviewer hashing the same document on disk must get the same value.
    """
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError
    return value


def _positive(value: object, *, maximum: int = 9_223_372_036_854_775_807) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError
    return value


def _repository(value: object) -> str:
    """A model repository id or revision: no slash-free identifier requirement."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError
    return value


def _timestamp_us(value: object) -> tuple[int, str]:
    """Return the microsecond instant and the canonical UTC text of a timestamp."""
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    utc = parsed.astimezone(UTC)
    text = utc.isoformat().replace("+00:00", "Z")
    delta = utc - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds, text


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError
    return value


@dataclass(frozen=True, slots=True)
class TrustDeclaration:
    """The deployment's explicit decision to trust an externally managed engine."""

    declared_by: str
    declared_at_us: int
    database: str
    schema: str
    installation_id: str
    trusted_instances: frozenset[str]
    acknowledged_gaps: frozenset[str]
    max_validity_seconds: int
    digest: str
    document: dict[str, Any]

    @classmethod
    def parse(cls, value: object) -> TrustDeclaration:
        try:
            root = _exact_mapping(
                value,
                frozenset(
                    {
                        "format",
                        "declared_by",
                        "declared_at",
                        "installation",
                        "scope",
                        "trusted_instances",
                        "acknowledged_gaps",
                        "max_attestation_validity_seconds",
                    }
                ),
            )
            if root["format"] != TRUST_DECLARATION_FORMAT:
                raise OperatorAttestationError("trust_declaration_format_invalid")
            installation = _exact_mapping(
                root["installation"], frozenset({"database", "schema", "installation_id"})
            )
            scope = _exact_mapping(root["scope"], frozenset({"lifecycle_controller"}))
            if scope["lifecycle_controller"] != "operator_external":
                raise OperatorAttestationError("trust_declaration_format_invalid")
            database = installation["database"]
            schema = installation["schema"]
            installation_id = installation["installation_id"]
            if (
                not isinstance(database, str)
                or _DATABASE.fullmatch(database) is None
                or not isinstance(schema, str)
                or _SCHEMA.fullmatch(schema) is None
                or not isinstance(installation_id, str)
                or _UUID.fullmatch(installation_id) is None
            ):
                raise OperatorAttestationError("trust_declaration_format_invalid")
            instances = root["trusted_instances"]
            if type(instances) is not list or not instances:
                raise OperatorAttestationError("trust_declaration_format_invalid")
            trusted = frozenset(_identifier(instance) for instance in instances)
            if len(trusted) != len(instances):
                raise OperatorAttestationError("trust_declaration_format_invalid")
            gaps = root["acknowledged_gaps"]
            if type(gaps) is not list or any(not isinstance(gap, str) for gap in gaps):
                raise OperatorAttestationError("trust_declaration_gaps_unacknowledged")
            acknowledged = frozenset(gaps)
            if not acknowledged >= REQUIRED_GAPS:
                raise OperatorAttestationError("trust_declaration_gaps_unacknowledged")
            validity = _positive(
                root["max_attestation_validity_seconds"],
                maximum=MAX_DECLARATION_VALIDITY_SECONDS,
            )
            declared_at_us, _ = _timestamp_us(root["declared_at"])
        except OperatorAttestationError:
            raise
        except ValueError, TypeError, KeyError, OverflowError:
            raise OperatorAttestationError("trust_declaration_format_invalid") from None
        return cls(
            declared_by=_identifier(root["declared_by"]),
            declared_at_us=declared_at_us,
            database=database,
            schema=schema,
            installation_id=installation_id,
            trusted_instances=trusted,
            acknowledged_gaps=acknowledged,
            max_validity_seconds=validity,
            digest=canonical_digest(root),
            document=root,
        )

    def require_live(self, now_us: int) -> None:
        if self.declared_at_us > now_us:
            raise OperatorAttestationError("trust_declaration_format_invalid")

    def require_target(self, *, database: str, schema: str, installation_id: str) -> None:
        mismatched = [
            name
            for name, declared, actual in (
                ("database", self.database, database),
                ("schema", self.schema, schema),
                ("installation_id", self.installation_id, installation_id),
            )
            if declared != actual
        ]
        if mismatched:
            raise OperatorAttestationError("trust_declaration_target_mismatch")

    def require_instance(self, instance_id: str) -> None:
        if instance_id not in self.trusted_instances:
            raise OperatorAttestationError("trust_declaration_instance_not_trusted")


@dataclass(frozen=True, slots=True)
class AttestationWindow:
    """The declared validity of one attestation, bounded by four independent facts."""

    observed_at_us: int
    expires_at_us: int
    observed_at_text: str

    @classmethod
    def parse(cls, value: object) -> AttestationWindow:
        try:
            window = _exact_mapping(value, frozenset({"observed_at", "expires_at"}))
            observed_at_us, observed_at_text = _timestamp_us(window["observed_at"])
            expires_at_us, _ = _timestamp_us(window["expires_at"])
        except ValueError, TypeError, KeyError, OverflowError:
            raise OperatorAttestationError("attestation_window_invalid") from None
        if expires_at_us <= observed_at_us:
            raise OperatorAttestationError("attestation_window_invalid")
        return cls(observed_at_us, expires_at_us, observed_at_text)

    def require_live(
        self,
        *,
        now_us: int,
        max_validity_seconds: int,
        source_expires_us: int,
        release_expires_us: int,
    ) -> None:
        if self.expires_at_us - self.observed_at_us > max_validity_seconds * 1_000_000:
            raise OperatorAttestationError("attestation_window_too_long")
        if not self.observed_at_us <= now_us < self.expires_at_us:
            raise OperatorAttestationError("attestation_window_expired")
        if self.expires_at_us > source_expires_us or self.expires_at_us > release_expires_us:
            raise OperatorAttestationError("attestation_window_too_long")


@dataclass(frozen=True, slots=True)
class AttestationRequest:
    """Everything the operator declares about one externally managed engine."""

    attestation_id: str
    approval_id: str
    database: str
    schema: str
    installation_id: str
    voice_id: str
    revision: str
    revision_id: str
    source_approval_id: str
    binding_id: str
    binding_revision_id: str
    instance_id: str
    instance_generation: int
    fencing_token: int
    template_id: str
    image_digest: str
    template_document: dict[str, Any]
    template_digest: str
    lifecycle_document: dict[str, Any]
    lifecycle_digest: str
    model_id: str
    model_revision: str
    model_artifact_digest: str
    capabilities: dict[str, Any]
    window: AttestationWindow
    document: dict[str, Any]

    @classmethod
    def parse(cls, value: object) -> AttestationRequest:
        try:
            root = _exact_mapping(
                value,
                frozenset(
                    {
                        "format",
                        "attestation_id",
                        "approval_id",
                        "installation",
                        "voice",
                        "binding",
                        "engine",
                        "model",
                        "capabilities",
                        "window",
                    }
                ),
            )
            if root["format"] != ATTESTATION_REQUEST_FORMAT:
                raise OperatorAttestationError("attestation_request_invalid")
            installation = _exact_mapping(
                root["installation"], frozenset({"database", "schema", "installation_id"})
            )
            voice = _exact_mapping(
                root["voice"],
                frozenset({"voice_id", "revision", "revision_id", "source_approval_id"}),
            )
            binding = _exact_mapping(
                root["binding"], frozenset({"binding_id", "binding_revision_id"})
            )
            engine = _exact_mapping(
                root["engine"],
                frozenset(
                    {
                        "instance_id",
                        "instance_generation",
                        "fencing_token",
                        "template_id",
                        "image_digest",
                        "template_document",
                        "lifecycle_document",
                    }
                ),
            )
            model = _exact_mapping(
                root["model"], frozenset({"model_id", "model_revision", "model_artifact_digest"})
            )
            capabilities = root["capabilities"]
            if type(capabilities) is not dict:
                raise OperatorAttestationError("attestation_request_invalid")
            database = installation["database"]
            schema = installation["schema"]
            installation_id = installation["installation_id"]
            if (
                not isinstance(database, str)
                or _DATABASE.fullmatch(database) is None
                or not isinstance(schema, str)
                or _SCHEMA.fullmatch(schema) is None
                or not isinstance(installation_id, str)
                or _UUID.fullmatch(installation_id) is None
            ):
                raise OperatorAttestationError("attestation_request_invalid")
            revision = voice["revision"]
            if (
                not isinstance(revision, str)
                or not re.fullmatch(r"[1-9][0-9]{0,18}", revision)
                or not isinstance(voice["revision_id"], str)
                or not 0 < len(voice["revision_id"]) <= 512
            ):
                raise OperatorAttestationError("attestation_request_invalid")
            template = engine["template_document"]
            lifecycle = engine["lifecycle_document"]
            if (
                type(template) is not dict
                or template.get("format") != TEMPLATE_FORMAT
                or type(template.get("template")) is not dict
            ):
                raise OperatorAttestationError("attestation_request_invalid")
            if type(lifecycle) is not dict or lifecycle.get("format") != LIFECYCLE_FORMAT:
                raise OperatorAttestationError("attestation_request_invalid")
            request = cls(
                attestation_id=_identifier(root["attestation_id"]),
                approval_id=_identifier(root["approval_id"]),
                database=database,
                schema=schema,
                installation_id=installation_id,
                voice_id=_identifier(voice["voice_id"]),
                revision=revision,
                revision_id=voice["revision_id"],
                source_approval_id=_identifier(voice["source_approval_id"]),
                binding_id=_identifier(binding["binding_id"]),
                binding_revision_id=_identifier(binding["binding_revision_id"]),
                instance_id=_identifier(engine["instance_id"]),
                instance_generation=_positive(engine["instance_generation"], maximum=2**31 - 1),
                fencing_token=_positive(engine["fencing_token"]),
                template_id=_identifier(engine["template_id"]),
                image_digest=_digest(engine["image_digest"]),
                template_document=template,
                template_digest=canonical_digest(template),
                lifecycle_document=lifecycle,
                lifecycle_digest=canonical_digest(lifecycle),
                model_id=_repository(model["model_id"]),
                model_revision=_repository(model["model_revision"]),
                model_artifact_digest=_digest(model["model_artifact_digest"]),
                capabilities=capabilities,
                window=AttestationWindow.parse(root["window"]),
                document=root,
            )
        except OperatorAttestationError:
            raise
        except ValueError, TypeError, KeyError, OverflowError:
            raise OperatorAttestationError("attestation_request_invalid") from None
        return request


def declaration_digest(declaration: TrustDeclaration, request: AttestationRequest) -> str:
    """Digest over both originals: a reviewer can recompute it from the two files."""
    return canonical_digest(
        {
            "trust_declaration": declaration.document,
            "attestation_request": request.document,
        }
    )


def require_template_identity(
    request: AttestationRequest,
    *,
    approved_template_id: str,
    image_digest: str,
) -> None:
    """The template original must describe the registered template and pinned image."""
    template = request.template_document["template"]
    if (
        template.get("template_id") != request.template_id
        or request.template_id != approved_template_id
        or template.get("image_digest") != request.image_digest
        or template.get("engine_auth") != "vllm_api_key_v1"
    ):
        raise OperatorAttestationError("template_identity_mismatch")
    if request.image_digest != image_digest:
        raise OperatorAttestationError("image_digest_mismatch")


def require_lifecycle_identity(
    request: AttestationRequest,
    *,
    declared_by: str,
) -> None:
    """The lifecycle original must describe this exact engine instance and epoch."""
    lifecycle = request.lifecycle_document
    identity = lifecycle.get("engine_identity")
    if (
        lifecycle.get("lifecycle_controller") != "operator_external"
        or lifecycle.get("declared_by") != declared_by
        or identity is None
        or type(identity) is not dict
        or identity.get("image_digest") != request.image_digest
        or identity.get("template_id") != request.template_id
        or identity.get("engine_auth") != "vllm_api_key_v1"
        or lifecycle.get("instance_id") != request.instance_id
        or lifecycle.get("instance_generation") != request.instance_generation
        or lifecycle.get("fencing_token") != request.fencing_token
    ):
        raise OperatorAttestationError("lifecycle_digest_mismatch")


def require_model_artifact(
    request: AttestationRequest,
    *,
    pin_model: dict[str, Any],
    stage_pins: tuple[tuple[str, str], ...],
) -> None:
    """The declared model identity and digest must match the release evidence."""
    if pin_model.get("repository") != request.model_id or pin_model.get("revision") != (
        request.model_revision
    ):
        raise OperatorAttestationError("model_artifact_digest_mismatch")
    if (request.model_artifact_digest, request.image_digest) not in stage_pins:
        raise OperatorAttestationError("model_artifact_digest_mismatch")


def require_capabilities(capabilities: dict[str, Any], domains: dict[str, Any]) -> dict[str, Any]:
    """Declared capabilities must stay inside the binding's published domains."""
    caps: dict[str, Any] = {}
    for key in _CAPABILITY_KEYS:
        declared = capabilities.get(key)
        allowed = domains.get(_DOMAIN_KEYS[key])
        if (
            type(declared) is not list
            or not declared
            or type(allowed) is not list
            or any(item not in allowed for item in declared)
        ):
            raise OperatorAttestationError("capability_outside_binding_domains")
        caps[key] = list(declared)
    limit = capabilities.get("max_input_codepoints")
    ceiling = domains.get("max_input_codepoints")
    if type(limit) is not int or type(ceiling) is not int or not 0 < limit <= ceiling:
        raise OperatorAttestationError("capability_outside_binding_domains")
    caps["max_input_codepoints"] = limit
    return caps


@dataclass(frozen=True, slots=True)
class RegistrationFacts:
    """The durable registration row an attestation is checked against."""

    instance_id: str
    instance_generation: int
    instance_state: str
    host_node_id: str
    gateway_id: str
    pool_id: str
    binding_revision_id: str
    manifest_id: str
    approved_template_id: str
    config_digest: str
    last_fencing_token: int
    head_generation: int
    owner_fencing_token: int
    owner_expires_at_us: int
    policy_revision_id: str


def require_registration(
    request: AttestationRequest,
    *,
    facts: RegistrationFacts,
    now_us: int,
) -> None:
    """Cross-check the declaration against durable registration and pool facts.

    Every expected identity comes from the ledger, never from the request, so an
    operator cannot declare a different binding revision, template or generation.
    """
    if (
        facts.instance_id != request.instance_id
        or request.instance_generation != facts.instance_generation
        or facts.head_generation != request.instance_generation
    ):
        raise OperatorAttestationError("instance_generation_mismatch")
    if facts.instance_state != "ready":
        raise OperatorAttestationError("instance_not_registered")
    if facts.binding_revision_id != request.binding_revision_id:
        raise OperatorAttestationError("binding_registration_mismatch")
    if facts.approved_template_id != request.template_id:
        raise OperatorAttestationError("template_identity_mismatch")
    if (
        facts.last_fencing_token != request.fencing_token
        or facts.owner_fencing_token != request.fencing_token
    ):
        raise OperatorAttestationError("stale_fencing_token")
    if facts.owner_expires_at_us <= now_us:
        raise OperatorAttestationError("stale_fencing_token")
    if not isinstance(facts.policy_revision_id, str) or not facts.policy_revision_id:
        raise OperatorAttestationError("policy_revision_mismatch")


def require_gateway_evidence(evidence: object, *, expected: dict[str, object], now_us: int) -> str:
    """The authenticated gateway's live mapping must equal the durable facts.

    This reads a response the operator never types: the gateway reports the policy
    digest, voice mapping and instance identity it will actually execute under.
    """
    if type(evidence) is not dict:
        raise OperatorAttestationError("gateway_policy_evidence_unavailable")
    if any(evidence.get(name) != value for name, value in expected.items()):
        raise OperatorAttestationError("gateway_policy_evidence_unavailable")
    digest_value = evidence.get("gateway_policy_digest")
    if not isinstance(digest_value, str) or _DIGEST.fullmatch(digest_value) is None:
        raise OperatorAttestationError("gateway_policy_evidence_unavailable")
    observed_at = evidence.get("observed_at")
    if not isinstance(observed_at, str) or _TIMESTAMP.fullmatch(observed_at) is None:
        raise OperatorAttestationError("gateway_policy_evidence_unavailable")
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise OperatorAttestationError("gateway_policy_evidence_unavailable")
    observed_us = int(observed.timestamp() * 1_000_000)
    if not now_us - 60_000_000 <= observed_us <= now_us + 60_000_000:
        raise OperatorAttestationError("gateway_policy_evidence_unavailable")
    return digest_value


def build_operator_facts(
    *,
    request: AttestationRequest,
    declared_by: str,
    declaration_digest_value: str,
    source_revision: str,
    source_digest: str,
    report_id: str,
    report_digest: str,
    manifest_id: str,
    manifest_digest: str,
    binding_id: str,
    binding_revision: str,
    host_node_id: str,
    gateway_id: str,
    policy_revision_id: str,
    gateway_policy_digest: str,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    """Build the self-describing qualification facts for one attestation."""
    checks = sorted(OPERATOR_CHECKS)
    document = {
        "resource_type": "voice_revision",
        "voice_id": request.voice_id,
        "revision": request.revision,
        "object": "nexus.voice_revision",
        "display_name": "Vivian",
        "capabilities": capabilities,
        "validation": {
            "validation_id": report_id,
            "status": "passed",
            "checked_at": request.window.observed_at_text,
        },
        "published_at": request.window.observed_at_text,
        "retired_at": None,
        "disabled_reason": None,
        "source": {
            "type": "builtin",
            "draft_id": None,
            "draft_revision": None,
            "source_asset_digest": None,
        },
        "status": "published",
        "model_binding": {"id": binding_id, "revision": binding_revision},
    }
    attestation = {
        "kind": "operator_attested",
        "attestation_id": request.attestation_id,
        "approval_id": request.approval_id,
        "declared_by": declared_by,
        "declaration_digest": declaration_digest_value,
        "template_digest": request.template_digest,
        "lifecycle_digest": request.lifecycle_digest,
        "image_digest": request.image_digest,
        "model_artifact_digest": request.model_artifact_digest,
        "instance_id": request.instance_id,
        "instance_generation": str(request.instance_generation),
        "fencing_token": str(request.fencing_token),
        "observed_at_us": request.window.observed_at_us,
        "expires_at_us": request.window.expires_at_us,
    }
    facts = {
        "source_approval_id": request.source_approval_id,
        "source_revision": source_revision,
        "source_digest": source_digest,
        "qualification_report_id": report_id,
        "qualification_report_digest": report_digest,
        "capacity_manifest_id": manifest_id,
        "capacity_manifest_digest": manifest_digest,
        "declaration_digest": declaration_digest_value,
        "attestation": attestation,
        "verifier_id": OPERATOR_VERIFIER_ID,
        "verifier_version": OPERATOR_VERIFIER_VERSION,
        "required_checks": checks,
        "model_id": request.model_id,
        "model_revision": request.model_revision,
        "model_artifact_digest": request.model_artifact_digest,
        "image_digest": request.image_digest,
        "generation": str(request.instance_generation),
        "instance_id": request.instance_id,
        "host_node_id": host_node_id,
        "gateway_id": gateway_id,
        "gateway_policy_revision_id": policy_revision_id,
        "gateway_policy_digest": gateway_policy_digest,
        "binding_id": binding_id,
        "binding_revision": binding_revision,
        "binding_revision_id": request.binding_revision_id,
        "native_speaker_id": "Vivian",
        "checked_at_us": request.window.observed_at_us,
        "expires_at_us": request.window.expires_at_us,
        "public_document": document,
        "qualified_capabilities": capabilities,
    }
    # Structural acceptance is the domain validator itself, never a caller's claim.
    from speechnexus_backend.speech.domain.builtin_voice_qualification import BuiltinVoiceFacts

    BuiltinVoiceFacts(facts)
    return facts
