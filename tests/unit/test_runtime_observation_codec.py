"""Complete response preservation, private provenance, and source-boundary checks."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.runtime_intents import RuntimeService
from speechnexus_backend.speech.domain.runtime_intents import (
    RuntimeRejectedError,
    RuntimeResult,
    unknown,
)
from speechnexus_backend.speech.domain.runtime_observations import ObserveRuntimeIntent
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.runtime_codec import (
    request_from_wire,
    request_to_wire,
    validate_original_observation,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    decode_observation,
    observation_for_boundary,
    prepare_observation,
    readiness_from_document,
    readiness_to_document,
    validate_stored_readiness,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_sql import _validate_progress
from speechnexus_backend.speech.infrastructure.runtime_postgres import PostgresRuntimeTransactions
from speechnexus_backend.speech.interfaces.runtime_transactions import RuntimeTransactionBoundary
from tests.fake.runtime_intents import NOW, PLAN, REQUEST
from tests.fake.runtime_observations import OBSERVE, READINESS, RECORD
from tests.unit.test_runtime_codec import FixedRuntimePort
from tests.unit.test_runtime_postgres import FaultPool


def _wire() -> dict[str, Any]:
    value = request_to_wire(REQUEST)
    value.pop("deadline_at")
    value.update(
        intent_state="succeeded",
        observed_instance_state="ready",
        created_at="2026-10-23T12:00:00.123456789Z",
        updated_at="2026-10-23T12:00:01.123456789Z",
    )
    return value


def _command(wire: dict[str, Any] | None = None) -> ObserveRuntimeIntent:
    prepared = prepare_observation(_wire() if wire is None else wire)
    return replace(OBSERVE, observation=prepared.observation, original_document=prepared.document)


def test_raw_response_is_complete_and_decimal_time_precision_survives() -> None:
    wire = _wire()
    wire["instance_generation"] = 1.0
    original = copy.deepcopy(wire)
    prepared = prepare_observation(wire)
    assert wire == original and type(wire["instance_generation"]) is float
    assert prepared.observation.created_submicrosecond == "789"
    assert prepared.observation.updated_submicrosecond == "789"
    assert json.loads(prepared.document)["created_at"] == wire["created_at"]
    assert type(json.loads(prepared.document)["instance_generation"]) is int
    assert decode_observation(prepared.document) == prepared


def test_post_replay_flag_and_optional_nulls_do_not_change_runtime_facts() -> None:
    first = prepare_observation(_wire())
    second = prepare_observation(
        {
            **_wire(),
            "idempotent_replay": True,
            "stop_evidence": None,
            "observed_peak_memory": None,
            "error_code": None,
        }
    )
    assert first.semantic_document == second.semantic_document and first.document != second.document
    record = replace(RECORD, intent_state="succeeded", observation_document=first.document)
    assert _validate_progress(record, second)
    changed = prepare_observation({**_wire(), "updated_at": "2026-10-23T12:00:01.123456790Z"})
    with pytest.raises(RuntimeRejectedError, match="intent_conflict"):
        _validate_progress(record, changed)


@pytest.mark.parametrize("change", ["created_after_update", "stop_after_update"])
def test_same_microsecond_still_preserves_actual_chronology(change: str) -> None:
    wire = _wire()
    wire.update(
        action="stop",
        observed_instance_state="stopped",
        created_at="2026-10-23T12:00:00.1234561Z",
        updated_at="2026-10-23T12:00:00.1234567Z",
    )
    wire["stop_evidence"] = {
        "process_or_container_exited": True,
        "resources_released_confirmed": True,
        "verified_at": "2026-10-23T12:00:00.1234567Z",
    }
    if change == "created_after_update":
        wire["created_at"] = "2026-10-23T12:00:00.1234568Z"
    else:
        wire["stop_evidence"]["verified_at"] = "2026-10-23T12:00:00.1234568Z"
    with pytest.raises(ValueError):
        prepare_observation(wire)


def test_private_readiness_round_trip_keeps_every_pin_and_original_validity() -> None:
    document = readiness_to_document(READINESS)
    assert readiness_from_document(document) == READINESS
    assert validate_stored_readiness(document, REQUEST, PLAN) == READINESS
    with pytest.raises(ValueError):
        validate_stored_readiness(document, replace(REQUEST, instance_generation=2), PLAN)


@pytest.mark.parametrize("change", ["version", "missing", "extra", "binding", "counter", "digest"])
def test_malformed_readiness_is_never_adopted_or_filled(change: str) -> None:
    value = json.loads(readiness_to_document(READINESS))
    if change == "version":
        value["record_version"] = True
    elif change == "missing":
        value.pop("evidence_digest")
    elif change == "extra":
        value["approved"] = True
    elif change == "binding":
        value["binding"].pop("config_digest")
    elif change == "counter":
        value["instance_generation"] = True
    else:
        value["evidence_digest"] = "not-a-digest"
    with pytest.raises(ValueError):
        readiness_from_document(json.dumps(value))


@pytest.mark.parametrize("value", [None, "", "{}", '"invalid"', "x" * 65537])
def test_invalid_or_unbounded_observation_record_cannot_be_restored(value: Any) -> None:
    with pytest.raises(ValueError):
        decode_observation(value)


class ObservationPort(FixedRuntimePort):
    def __init__(self, *, ambiguous: bool = False) -> None:
        super().__init__()
        self.observations: list[ObserveRuntimeIntent] = []
        self.ambiguous = ambiguous

    def observe_runtime(self, command: ObserveRuntimeIntent) -> RuntimeResult:
        self.observations.append(command)
        if self.ambiguous:
            return unknown(command.observation.identity)
        record = replace(
            self.record,
            intent_state=command.observation.intent_state,
            updated_at_us=NOW + 1,
            version=2,
            observation_document=command.original_document,
        )
        return RuntimeResult("committed", record.identity, record)


def _boundary(port: ObservationPort, *, assembled: bool = True) -> RuntimeTransactionBoundary:
    return RuntimeTransactionBoundary(
        RuntimeService(port),
        owner_id="scheduler-boundary-test",
        decode_request=request_from_wire,
        render_request=request_to_wire,
        decode_observation=validate_original_observation,
        parse_observation=observation_for_boundary if assembled else None,
    )


def _request() -> dict[str, Any]:
    return {
        "observation": _wire(),
        "expected_instance_version": "1",
        "expected_intent_version": "1",
    }


def test_boundary_preserves_original_response_and_keeps_server_context_private() -> None:
    port = ObservationPort()
    request = _request()
    original = copy.deepcopy(request)
    result = _boundary(port).observe_runtime(request, readiness=READINESS)
    assert (
        result["operation"] == "observe_runtime"
        and result["value"]["runtime"] == request["observation"]
    )
    assert request == original and len(port.observations) == 1
    assert port.observations[0].owner_id == "scheduler-boundary-test"
    assert port.observations[0].readiness == READINESS
    assert "readiness_document" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    ["client_owner", "client_receipt", "integer_revision", "bool_revision", "missing_revision"],
)
def test_boundary_does_not_accept_client_authority_or_coerce_revision_shapes(change: str) -> None:
    request = _request()
    if change == "client_owner":
        request["owner_id"] = "other-owner"
    elif change == "client_receipt":
        request["readiness"] = {"qualified": True}
    elif change == "integer_revision":
        request["expected_instance_version"] = 1
    elif change == "bool_revision":
        request["expected_instance_version"] = True
    else:
        request.pop("expected_intent_version")
    port = ObservationPort()
    with pytest.raises(ContractValidationError):
        _boundary(port).observe_runtime(request, readiness=None)
    assert port.observations == []


def test_unassembled_boundary_cannot_fabricate_an_observation() -> None:
    port = ObservationPort()
    with pytest.raises(ContractValidationError, match="not been assembled"):
        _boundary(port, assembled=False).observe_runtime(_request(), readiness=None)
    assert port.observations == []


def test_ambiguous_observation_is_not_resubmitted_by_application() -> None:
    port = ObservationPort(ambiguous=True)
    result = _boundary(port).observe_runtime(_request(), readiness=None)
    assert (
        result["outcome"] == "unknown"
        and result["recovery"] == "query_original"
        and len(port.observations) == 1
    )


@pytest.mark.parametrize("error", [psycopg.OperationalError(), psycopg.InterfaceError()])
def test_observation_transport_failure_does_not_write_again(error: Exception) -> None:
    pool = FaultPool(error)
    result = PostgresRuntimeTransactions(cast(SpeechPool, pool)).observe_runtime(_command())
    assert result.outcome == "unknown" and result.recovery == "query_original" and pool.calls == 1


def test_forged_fact_document_pair_is_rejected_before_acquiring_a_connection() -> None:
    pool = FaultPool(psycopg.OperationalError())
    command = _command()
    forged = replace(
        command,
        original_document=prepare_observation({**_wire(), "gateway_id": "other-gateway"}).document,
    )
    with pytest.raises(ValueError, match="do not match"):
        PostgresRuntimeTransactions(cast(SpeechPool, pool)).observe_runtime(forged)
    assert pool.calls == 0
