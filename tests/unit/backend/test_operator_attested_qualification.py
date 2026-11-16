"""Operator-attested qualification: declarations, provenance and closed capabilities.

These checks are deliberately split from the PostgreSQL cases: the digestion of
declared originals, the machine-distinguishability of the two fact variants and
the capabilities an attestation cannot open are pure domain facts.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from speechnexus_backend.attest_builtin_voice import load_trust_declaration
from speechnexus_backend.attest_builtin_voice import main as attest_main
from speechnexus_backend.qualification_provenance import (
    EVIDENCE_LEGACY,
    SOURCE_CONTROLLER,
    SOURCE_OPERATOR,
    provenance_report,
    summarize,
)
from speechnexus_backend.speech.domain.builtin_voice_qualification import BuiltinVoiceFacts
from speechnexus_backend.speech.domain.lease_release import (
    LeaseStop,
    ReleaseLease,
    release,
)
from speechnexus_backend.speech.domain.leases import LeaseRejectedError
from speechnexus_backend.speech.domain.operator_attestation import (
    OPERATOR_CHECKS,
    REQUIRED_GAPS,
    AttestationRequest,
    AttestationWindow,
    OperatorAttestationError,
    RegistrationFacts,
    TrustDeclaration,
    build_operator_facts,
    canonical_digest,
    declaration_digest,
    require_capabilities,
    require_gateway_evidence,
    require_lifecycle_identity,
    require_model_artifact,
    require_registration,
    require_template_identity,
)
from speechnexus_backend.speech.domain.qualification_native_observation import (
    ExpectedNativeRuntime,
    require_native_ready,
)
from speechnexus_backend.speech.domain.runtime_intents import RuntimeHead, RuntimeRecord
from speechnexus_backend.speech.domain.runtime_observations import (
    ObserveRuntimeIntent,
    RuntimeObservation,
    RuntimeObservedStop,
    decide_runtime_observation,
)
from speechnexus_backend.speech.infrastructure.runtime_codec import request_to_wire
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import prepare_observation
from tests.fake.runtime_intents import COMMAND, NOW, OWNER, PLAN, REQUEST
from tests.fake.runtime_observations import HOST, INSTANCE, PREREQUISITES
from tests.unit.test_production_leases import decide as quarantine_decision

IMAGE_DIGEST = "sha256:" + "1" * 64
TEMPLATE_DIGEST = "sha256:" + "2" * 64
LIFECYCLE_DIGEST = "sha256:" + "3" * 64
MODEL_DIGEST = "sha256:" + "4" * 64
DECLARATION_DIGEST = "sha256:" + "5" * 64
GATEWAY_DIGEST = "sha256:" + "6" * 64
INSTALLATION = "0b9a3eda-2b32-4c2c-9c39-7e6c1c8d6f01"

TEMPLATE = {
    "format": "speechnexus-template-v1",
    "template": {
        "template_id": "template-tts-customvoice",
        "image_digest": IMAGE_DIGEST,
        "engine_auth": "vllm_api_key_v1",
    },
}
LIFECYCLE = {
    "format": "speechnexus-external-lifecycle-v1",
    "lifecycle_controller": "operator_external",
    "declared_by": "ops-oncall",
    "instance_id": "instance-tts-1",
    "instance_generation": 1,
    "fencing_token": 7,
    "engine_identity": {
        "image_digest": IMAGE_DIGEST,
        "template_id": "template-tts-customvoice",
        "engine_auth": "vllm_api_key_v1",
    },
}


def _timestamp(us: int) -> str:
    return (
        datetime.fromtimestamp(us / 1_000_000, tz=UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _declaration(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "format": "speechnexus-operator-attested-engine-v1",
        "declared_by": "ops-oncall",
        "declared_at": _timestamp(NOW - 7_200_000_000),
        "installation": {
            "database": "speechnexus",
            "schema": "speech",
            "installation_id": INSTALLATION,
        },
        "scope": {"lifecycle_controller": "operator_external"},
        "trusted_instances": ["instance-tts-1"],
        "acknowledged_gaps": sorted(REQUIRED_GAPS),
        "max_attestation_validity_seconds": 86_400,
    }
    value.update(changes)
    return value


def _request(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "format": "speechnexus-operator-attested-qualification-v1",
        "attestation_id": "att-tts-1",
        "approval_id": "approval-att-tts-1",
        "installation": {
            "database": "speechnexus",
            "schema": "speech",
            "installation_id": INSTALLATION,
        },
        "voice": {
            "voice_id": "voice-vivian",
            "revision": "1",
            "revision_id": "voice-rev-1",
            "source_approval_id": "source-1",
        },
        "binding": {"binding_id": "binding-tts", "binding_revision_id": "binding-rev-1"},
        "engine": {
            "instance_id": "instance-tts-1",
            "instance_generation": 1,
            "fencing_token": 7,
            "template_id": "template-tts-customvoice",
            "image_digest": IMAGE_DIGEST,
            "template_document": TEMPLATE,
            "lifecycle_document": LIFECYCLE,
        },
        "model": {
            "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "model_revision": "rev-1",
            "model_artifact_digest": MODEL_DIGEST,
        },
        "capabilities": {
            "languages": ["en"],
            "audio_formats": ["wav"],
            "sample_rates_hz": [24000],
            "modes": ["complete"],
            "max_input_codepoints": 400,
        },
        "window": {
            "observed_at": _timestamp(NOW - 1_000_000),
            "expires_at": _timestamp(NOW + 3_600_000_000),
        },
    }
    value.update(changes)
    return value


def _parsed_request(**changes: Any) -> AttestationRequest:
    return AttestationRequest.parse(_request(**changes))


def _facts(**changes: Any) -> dict[str, Any]:
    request = _parsed_request()
    values: dict[str, Any] = {
        "request": request,
        "declared_by": "ops-oncall",
        "declaration_digest_value": DECLARATION_DIGEST,
        "source_revision": "source-rev-1",
        "source_digest": "sha256:" + "7" * 64,
        "report_id": "report-1",
        "report_digest": "sha256:" + "8" * 64,
        "manifest_id": "manifest-1",
        "manifest_digest": "sha256:" + "9" * 64,
        "binding_id": "binding-tts",
        "binding_revision": "1",
        "host_node_id": "host-1",
        "gateway_id": "gateway-1",
        "policy_revision_id": "policy-1",
        "gateway_policy_digest": GATEWAY_DIGEST,
        "capabilities": {
            "languages": ["en"],
            "audio_formats": ["wav"],
            "sample_rates_hz": [24000],
            "modes": ["complete"],
            "max_input_codepoints": 400,
        },
    }
    values.update(changes)
    return build_operator_facts(**values)


# --- trust declaration -------------------------------------------------------


def test_missing_trust_declaration_is_named_and_never_inferred() -> None:
    with pytest.raises(OperatorAttestationError) as missing:
        load_trust_declaration(None)
    assert missing.value.code == "trust_declaration_missing"


def test_the_cli_refuses_before_connecting_when_trust_is_not_declared(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL",
        "postgresql://nobody@127.0.0.1:1/synthetic",
    )
    paths: dict[str, str] = {}
    for name in ("artifact-pin", "manifest", "report", "scenario-evidence", "request"):
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
        paths[name] = str(path)
    code = attest_main(
        [
            "--schema",
            "speech",
            "--workspace-id",
            "wsp-synthetic",
            "--attestation-request-file",
            paths["request"],
            "--artifact-pin",
            paths["artifact-pin"],
            "--manifest",
            paths["manifest"],
            "--report",
            paths["report"],
            "--scenario-evidence",
            paths["scenario-evidence"],
        ]
    )
    captured = capsys.readouterr()
    assert code == 4
    assert "error: trust_declaration_missing" in captured.err
    assert captured.out == ""


def test_the_cli_names_the_missing_writer_dsn_before_connecting(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL", raising=False)
    paths: dict[str, str] = {}
    for name in ("artifact-pin", "manifest", "report", "scenario-evidence", "request"):
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
        paths[name] = str(path)
    code = attest_main(
        [
            "--schema",
            "speech",
            "--workspace-id",
            "wsp-synthetic",
            "--attestation-request-file",
            paths["request"],
            "--artifact-pin",
            paths["artifact-pin"],
            "--manifest",
            paths["manifest"],
            "--report",
            paths["report"],
            "--scenario-evidence",
            paths["scenario-evidence"],
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "error: SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL is not set" in captured.err
    assert captured.out == ""


def test_declaration_must_acknowledge_every_closed_capability() -> None:
    raw = _declaration()
    explicit = sorted(REQUIRED_GAPS - {"no_budget_release_by_attestation"})
    raw["acknowledged_gaps"] = explicit
    with pytest.raises(OperatorAttestationError) as error:
        TrustDeclaration.parse(raw)
    assert error.value.code == "trust_declaration_gaps_unacknowledged"
    parsed = TrustDeclaration.parse(_declaration())
    assert parsed.acknowledged_gaps == REQUIRED_GAPS


def test_declaration_format_scope_and_target_are_enforced() -> None:
    for changes in (
        {"format": "speechnexus-template-v1"},
        {"scope": {"lifecycle_controller": "node_controller"}},
        {"max_attestation_validity_seconds": 2_592_001},
    ):
        with pytest.raises(OperatorAttestationError) as error:
            TrustDeclaration.parse(_declaration(**changes))
        assert error.value.code in (
            "trust_declaration_format_invalid",
            "trust_declaration_gaps_unacknowledged",
        )
    declaration = TrustDeclaration.parse(_declaration())
    with pytest.raises(OperatorAttestationError) as mismatch:
        declaration.require_target(database="other", schema="speech", installation_id=INSTALLATION)
    assert mismatch.value.code == "trust_declaration_target_mismatch"
    with pytest.raises(OperatorAttestationError) as untrusted:
        declaration.require_instance("instance-other")
    assert untrusted.value.code == "trust_declaration_instance_not_trusted"


# --- declared originals ------------------------------------------------------


def test_declared_originals_are_digested_not_trusted() -> None:
    request = _parsed_request()
    assert request.template_digest == canonical_digest(TEMPLATE)
    assert request.lifecycle_digest == canonical_digest(LIFECYCLE)
    # A reviewer recomputing the digest from the same file gets the same value.
    assert request.template_digest == canonical_digest(json.loads(json.dumps(TEMPLATE)))


def test_template_and_lifecycle_identity_name_the_disagreeing_declaration() -> None:
    request = _parsed_request()
    require_template_identity(
        request, approved_template_id="template-tts-customvoice", image_digest=IMAGE_DIGEST
    )
    require_lifecycle_identity(request, declared_by="ops-oncall")
    with pytest.raises(OperatorAttestationError) as wrong_template:
        require_template_identity(
            request, approved_template_id="template-other", image_digest=IMAGE_DIGEST
        )
    assert wrong_template.value.code == "template_identity_mismatch"
    with pytest.raises(OperatorAttestationError) as wrong_image:
        require_template_identity(
            request,
            approved_template_id="template-tts-customvoice",
            image_digest="sha256:" + "a" * 64,
        )
    assert wrong_image.value.code == "image_digest_mismatch"
    with pytest.raises(OperatorAttestationError) as other_declarer:
        require_lifecycle_identity(request, declared_by="someone-else")
    assert other_declarer.value.code == "lifecycle_digest_mismatch"
    for changed in (
        replace(request, instance_generation=2),
        replace(request, lifecycle_document={**LIFECYCLE, "fencing_token": 8}),
        replace(request, lifecycle_document={**LIFECYCLE, "engine_identity": None}),
    ):
        with pytest.raises(OperatorAttestationError) as error:
            require_lifecycle_identity(changed, declared_by="ops-oncall")
        assert error.value.code == "lifecycle_digest_mismatch"


def test_model_and_instance_claims_are_checked_against_registration() -> None:
    request = _parsed_request()
    pins = ((MODEL_DIGEST, IMAGE_DIGEST),)
    require_model_artifact(
        request,
        pin_model={
            "repository": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "revision": "rev-1",
        },
        stage_pins=pins,
    )
    with pytest.raises(OperatorAttestationError) as model:
        require_model_artifact(
            request,
            pin_model={
                "repository": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                "revision": "rev-2",
            },
            stage_pins=pins,
        )
    assert model.value.code == "model_artifact_digest_mismatch"
    facts = RegistrationFacts(
        instance_id="instance-tts-1",
        instance_generation=1,
        instance_state="ready",
        host_node_id="host-1",
        gateway_id="gateway-1",
        pool_id="tts-batch",
        binding_revision_id="binding-rev-1",
        manifest_id="manifest-1",
        approved_template_id="template-tts-customvoice",
        config_digest="sha256:" + "b" * 64,
        last_fencing_token=7,
        head_generation=1,
        owner_fencing_token=7,
        owner_expires_at_us=NOW + 60_000_000,
        policy_revision_id="policy-1",
    )
    require_registration(request, facts=facts, now_us=NOW)
    for changed, code in (
        ({"instance_generation": 2}, "instance_generation_mismatch"),
        ({"head_generation": 2}, "instance_generation_mismatch"),
        ({"instance_state": "stopping"}, "instance_not_registered"),
        ({"binding_revision_id": "binding-rev-2"}, "binding_registration_mismatch"),
        ({"approved_template_id": "template-other"}, "template_identity_mismatch"),
        ({"last_fencing_token": 6}, "stale_fencing_token"),
        ({"owner_fencing_token": 6}, "stale_fencing_token"),
        ({"owner_expires_at_us": NOW}, "stale_fencing_token"),
    ):
        with pytest.raises(OperatorAttestationError) as error:
            require_registration(request, facts=replace(facts, **changed), now_us=NOW)
        assert error.value.code == code


# --- window and capabilities -------------------------------------------------


def test_window_is_bounded_by_declaration_source_and_release() -> None:
    window = AttestationWindow.parse(
        {
            "observed_at": _timestamp(NOW - 1_000_000),
            "expires_at": _timestamp(NOW + 3_600_000_000),
        }
    )
    window.require_live(
        now_us=NOW,
        max_validity_seconds=86_400,
        source_expires_us=NOW + 7_200_000_000,
        release_expires_us=NOW + 7_200_000_000,
    )
    with pytest.raises(OperatorAttestationError) as expired:
        window.require_live(
            now_us=NOW + 3_600_000_000,
            max_validity_seconds=86_400,
            source_expires_us=NOW + 7_200_000_000,
            release_expires_us=NOW + 7_200_000_000,
        )
    assert expired.value.code == "attestation_window_expired"
    with pytest.raises(OperatorAttestationError) as too_long:
        window.require_live(
            now_us=NOW,
            max_validity_seconds=60,
            source_expires_us=NOW + 7_200_000_000,
            release_expires_us=NOW + 7_200_000_000,
        )
    assert too_long.value.code == "attestation_window_too_long"
    with pytest.raises(OperatorAttestationError) as source_bound:
        window.require_live(
            now_us=NOW,
            max_validity_seconds=86_400,
            source_expires_us=NOW,
            release_expires_us=NOW + 7_200_000_000,
        )
    assert source_bound.value.code == "attestation_window_too_long"


def test_declared_capabilities_cannot_exceed_binding_domains() -> None:
    domains = {
        "languages": ["en"],
        "audio_formats": ["wav"],
        "sample_rates_hz": [24000],
        "audio_delivery_modes": ["complete"],
        "max_input_codepoints": 400,
    }
    declared = {
        "languages": ["en"],
        "audio_formats": ["wav"],
        "sample_rates_hz": [24000],
        "modes": ["complete"],
        "max_input_codepoints": 400,
    }
    assert require_capabilities(declared, domains) == declared
    for changed in (
        {"languages": ["en", "zh"]},
        {"audio_formats": ["mp3"]},
        {"sample_rates_hz": [48000]},
        {"modes": ["stream"]},
        {"max_input_codepoints": 401},
    ):
        with pytest.raises(OperatorAttestationError) as error:
            require_capabilities({**declared, **changed}, domains)
        assert error.value.code == "capability_outside_binding_domains"


def test_gateway_evidence_is_live_and_matches_durable_facts() -> None:
    evidence = {
        "gateway_policy_digest": GATEWAY_DIGEST,
        "policy_revision_id": "policy-1",
        "voice_id": "voice-vivian",
        "voice_revision": "1",
        "voice_revision_id": "voice-rev-1",
        "native_speaker_id": "Vivian",
        "binding_revision_id": "binding-rev-1",
        "node_id": "host-1",
        "gateway_id": "gateway-1",
        "instance_id": "instance-tts-1",
        "instance_generation": 1,
        "observed_at": _timestamp(NOW - 1_000_000),
    }
    expected = {
        key: value
        for key, value in evidence.items()
        if key not in ("gateway_policy_digest", "observed_at")
    }
    assert require_gateway_evidence(evidence, expected=expected, now_us=NOW) == GATEWAY_DIGEST
    for changed in (
        {"policy_revision_id": "policy-2"},
        {"observed_at": _timestamp(NOW - 120_000_000)},
        {"gateway_policy_digest": "sha256:" + "c" * 63},
    ):
        with pytest.raises(OperatorAttestationError) as error:
            require_gateway_evidence({**evidence, **changed}, expected=expected, now_us=NOW)
        assert error.value.code == "gateway_policy_evidence_unavailable"


# --- facts variants ---------------------------------------------------------


def test_operator_facts_are_self_describing_and_distinct_from_probe_facts() -> None:
    facts = _facts()
    BuiltinVoiceFacts(facts)
    assert facts["verifier_id"] == "operator-attested-customvoice-v1"
    assert set(facts["required_checks"]) == OPERATOR_CHECKS
    assert "probe_evidence_digest" not in facts
    assert facts["declaration_digest"] == DECLARATION_DIGEST
    attestation = facts["attestation"]
    assert (
        attestation["kind"],
        attestation["instance_generation"],
        attestation["fencing_token"],
    ) == (
        "operator_attested",
        "1",
        "7",
    )
    # A probe record cannot borrow the operator shape and vice versa.
    with pytest.raises(ValueError, match="incomplete qualification facts"):
        BuiltinVoiceFacts({**facts, "probe_evidence_digest": DECLARATION_DIGEST})
    broken = dict(facts)
    broken["attestation"] = {**attestation, "instance_generation": "2"}
    with pytest.raises(ValueError, match="attestation facts disagree"):
        BuiltinVoiceFacts(broken)
    probe_err = dict(facts)
    probe_err["verifier_id"] = "builtin-customvoice-v1"
    with pytest.raises(ValueError):
        BuiltinVoiceFacts(probe_err)


def test_controller_facts_still_require_exactly_the_probe_shape() -> None:
    facts = dict(_facts())
    facts.pop("attestation")
    facts.pop("declaration_digest")
    facts["probe_evidence_digest"] = DECLARATION_DIGEST
    facts["verifier_id"] = "builtin-customvoice-v1"
    facts["required_checks"] = [
        "source_approved",
        "artifact_integrity",
        "engine_identity",
        "voice_speaker_binding",
        "wav_complete",
        "output_bounds",
        "native_terminal",
        "stop_release",
    ]
    BuiltinVoiceFacts(facts)
    with pytest.raises(ValueError, match="invalid required checks"):
        BuiltinVoiceFacts({**facts, "required_checks": sorted(OPERATOR_CHECKS)})


def test_declaration_digest_covers_both_originals() -> None:
    declaration = TrustDeclaration.parse(_declaration())
    request = _parsed_request()
    digest = declaration_digest(declaration, request)
    assert digest == canonical_digest(
        {
            "trust_declaration": declaration.document,
            "attestation_request": request.document,
        }
    )
    changed = _parsed_request(
        window={
            "observed_at": _timestamp(NOW - 2_000_000),
            "expires_at": _timestamp(NOW + 3_600_000_000),
        }
    )
    assert declaration_digest(declaration, changed) != digest


# --- closed capabilities ----------------------------------------------------


def _manual_registration_observation() -> Any:
    """The observation ``engine_registration`` synthesizes for a manual engine."""
    wire = request_to_wire(REQUEST)
    wire.pop("deadline_at")
    wire.update(
        intent_state="succeeded",
        observed_instance_state="ready",
        created_at="2026-10-23T12:00:00.123456789Z",
        updated_at="2026-10-23T12:00:01.123456789Z",
    )
    return prepare_observation(wire).observation


def test_operator_attestation_does_not_unlock_the_native_probe_path() -> None:
    observation = _manual_registration_observation()
    assert observation.native_runtime_evidence is None
    expected = ExpectedNativeRuntime(
        observation.binding,
        observation.instance_id,
        observation.instance_generation,
        observation.fencing_token,
        IMAGE_DIGEST,
        TEMPLATE_DIGEST,
        LIFECYCLE_DIGEST,
    )
    with pytest.raises(ValueError, match="native_runtime_evidence_unavailable"):
        require_native_ready(observation, expected, now_us=NOW, max_age_us=86_400_000_000)
    # Even a fully consistent operator declaration changes nothing here.
    BuiltinVoiceFacts(_facts())
    with pytest.raises(ValueError, match="native_runtime_evidence_unavailable"):
        require_native_ready(observation, expected, now_us=NOW, max_age_us=86_400_000_000)


def _stop_observation(*, stop_evidence: RuntimeObservedStop | None) -> RuntimeObservation:
    return RuntimeObservation(
        "intent-stop-0001",
        REQUEST.binding,
        REQUEST.instance_id,
        1,
        "stop",
        7,
        PLAN.memory,
        "succeeded",
        "stopped",
        NOW - 500,
        NOW - 100,
        stop_evidence=stop_evidence,
    )


def _decide_stop(observation: RuntimeObservation, *, leases_released: bool = True) -> Any:
    stop_request = replace(REQUEST, intent_id="intent-stop-0001", action="stop")
    head = RuntimeHead(REQUEST.instance_id, 1, 1, REQUEST.intent_id, "intent-stop-0001", 1)
    record = RuntimeRecord(
        stop_request,
        PLAN,
        COMMAND.owner_id,
        "sha256:" + "d" * 64,
        "pending",
        NOW - 1000,
        NOW - 1000,
        1,
    )
    instance = replace(INSTANCE, instance_state="ready", base_budget_kind="steady", held_slots=0)
    return decide_runtime_observation(
        ObserveRuntimeIntent(observation, 1, 1, COMMAND.owner_id, "synthetic-stop"),
        record=record,
        head=head,
        instance=instance,
        host=HOST,
        ownership=OWNER,
        prerequisites=replace(PREREQUISITES, all_leases_released=leases_released),
        now_us=NOW,
    )


def test_stop_success_without_physical_evidence_is_impossible() -> None:
    with pytest.raises(ValueError, match="Stop success requires actual release evidence"):
        _stop_observation(stop_evidence=None)
    with pytest.raises(ValueError, match="Both physical stop facts are required"):
        RuntimeObservedStop(
            process_or_container_exited=True,
            resources_released_confirmed=False,
            verified_at_us=NOW - 200,
        )


def test_base_budget_is_released_only_by_controller_stop_evidence() -> None:
    released = _decide_stop(
        _stop_observation(
            stop_evidence=RuntimeObservedStop(
                process_or_container_exited=True,
                resources_released_confirmed=True,
                verified_at_us=NOW - 200,
            )
        )
    )
    assert (released.base_budget_kind, released.instance_state) == ("released", "stopped")
    # Leases still running: the physical stop is recorded, the budget is not returned.
    reconciling = _decide_stop(
        _stop_observation(
            stop_evidence=RuntimeObservedStop(
                process_or_container_exited=True,
                resources_released_confirmed=True,
                verified_at_us=NOW - 200,
            )
        ),
        leases_released=False,
    )
    assert (reconciling.base_budget_kind, reconciling.instance_state) == (
        "steady",
        "stopping",
    )


def test_operator_delete_cannot_discharge_execution_cleanup_pending() -> None:
    with pytest.raises(ValueError, match="Unverified lease stop kind"):
        LeaseStop(
            "submit-test",
            "operator_attested_qualification",
            "sha256:" + "e" * 64,
            100,
        )
    isolated = quarantine_decision()
    command = ReleaseLease(isolated.lease.lease_id, isolated.lease.version)
    for execution in (None,):
        with pytest.raises(LeaseRejectedError, match="execution_cleanup_pending"):
            release(
                isolated.lease,
                replace(isolated.accounting, node_state="quarantined"),
                command,
                execution=execution,
                other_leases_released=True,
                now_us=101,
            )


# --- read surface -----------------------------------------------------------


def _row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "voice_id": "voice-vivian",
        "revision": "1",
        "revision_id": "voice-rev-1",
        "binding_revision_id": "binding-rev-1",
        "recorded_source_kind": None,
        "attestation_id": None,
        "recorded_at": None,
        "instance_id": "instance-tts-1",
        "instance_generation": "1",
        "fencing_token": None,
        "approval_expires_at_us": NOW + 3_600_000_000,
        "declared_by": None,
        "declaration_digest": None,
        "image_digest": None,
        "template_digest": None,
        "controller_artifact_digest": None,
        "observed_at_us": None,
        "expires_at_us": None,
        "now_us": NOW,
    }
    row.update(changes)
    return row


def test_read_surface_distinguishes_both_sources_and_legacy_rows() -> None:
    operator_row = _row(
        recorded_source_kind=SOURCE_OPERATOR,
        attestation_id="att-tts-1",
        declared_by="ops-oncall",
        declaration_digest=DECLARATION_DIGEST,
        observed_at_us=NOW - 1_000_000,
        expires_at_us=NOW + 3_600_000_000,
    )
    report = provenance_report(
        [
            _row(recorded_source_kind=SOURCE_CONTROLLER),
            operator_row,
            _row(voice_id="voice-legacy"),
        ],
        now_us=NOW,
    )
    assert [entry["source_kind"] for entry in report] == [
        SOURCE_CONTROLLER,
        SOURCE_OPERATOR,
        SOURCE_CONTROLLER,
    ]
    assert report[1]["provenance_recorded"] is True
    assert report[1]["attestation_id"] == "att-tts-1"
    assert report[1]["attestation_state"] == "live"
    assert "not a controller observation" in report[1]["note"]
    assert report[2]["provenance_recorded"] is False
    assert report[2]["evidence"] == EVIDENCE_LEGACY
    summary = summarize(report)
    assert (summary[SOURCE_CONTROLLER], summary[SOURCE_OPERATOR]) == (2, 1)
    assert summary[EVIDENCE_LEGACY] == 1


def test_read_surface_marks_an_elapsed_attestation() -> None:
    report = provenance_report(
        [
            _row(
                recorded_source_kind=SOURCE_OPERATOR,
                attestation_id="att-tts-1",
                declared_by="ops-oncall",
                declaration_digest=DECLARATION_DIGEST,
                observed_at_us=NOW - 7_200_000_000,
                expires_at_us=NOW - 1,
            )
        ],
        now_us=NOW,
    )
    assert report[0]["attestation_state"] == "expired"
    assert "re-attest" in report[0]["note"]


def test_attestation_request_rejects_unknown_or_missing_fields() -> None:
    with pytest.raises(OperatorAttestationError) as extra:
        AttestationRequest.parse({**_request(), "extra": True})
    assert extra.value.code == "attestation_request_invalid"
    raw = _request()
    raw["engine"] = {key: value for key, value in raw["engine"].items() if key != "fencing_token"}
    with pytest.raises(OperatorAttestationError) as missing:
        AttestationRequest.parse(raw)
    assert missing.value.code == "attestation_request_invalid"
    with pytest.raises(OperatorAttestationError) as bad_format:
        AttestationRequest.parse({**_request(), "format": "speechnexus-template-v1"})
    assert bad_format.value.code == "attestation_request_invalid"


def test_window_text_round_trips_to_the_facts_timestamp() -> None:
    request = _parsed_request()
    facts = _facts()
    assert datetime.fromisoformat(
        facts["public_document"]["validation"]["checked_at"].replace("Z", "+00:00")
    ) == datetime.fromtimestamp(request.window.observed_at_us / 1_000_000, tz=UTC)
    shifted = _parsed_request(
        window={
            "observed_at": _timestamp(NOW - 3_600_000_000),
            "expires_at": _timestamp(NOW + 3_600_000_000),
        }
    )
    assert shifted.window.observed_at_us == NOW - 3_600_000_000
