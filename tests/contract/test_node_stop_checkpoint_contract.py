"""The private checkpoint envelope reuses, rather than copies, the runtime request schema."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Resource
from referencing.jsonschema import DRAFT202012

from speechnexus_node_controller.domain.intents import (
    InvalidRuntimeRequestError,
    RuntimeActionKind,
)
from speechnexus_node_controller.domain.stop_checkpoint import (
    StopCheckpoint,
    parse_stop_checkpoint,
    stop_checkpoint_payload,
)
from tests.contract.test_p0_public_contracts import _CONTRACTS, _REGISTRY, _resolved_yaml
from tests.fake.controller import FIXED_NOW, build_request

_PATH = _CONTRACTS.parent / "node-controller" / "contracts" / "stop-checkpoint.schema.json"
_SCHEMA = _resolved_yaml(_PATH)
_RUNTIME_PATH = _CONTRACTS / "internal" / "runtime-control.openapi.yaml"
_RESOURCE = Resource.from_contents(_resolved_yaml(_RUNTIME_PATH), default_specification=DRAFT202012)
_VALIDATOR = Draft202012Validator(
    _SCHEMA,
    registry=_REGISTRY.with_resource(_RUNTIME_PATH.as_uri(), _RESOURCE),
    format_checker=FormatChecker(),
)


def checkpoints() -> tuple[StopCheckpoint, StopCheckpoint, StopCheckpoint]:
    target = StopCheckpoint(
        request=build_request(action=RuntimeActionKind.STOP), container_id="container-original"
    )
    exited = replace(target, exit_code=143, exited_at=FIXED_NOW)
    released = replace(exited, released_at=FIXED_NOW + timedelta(seconds=1))
    return target, exited, released


def test_all_three_private_checkpoint_stages_round_trip_and_match_the_machine_contract() -> None:
    Draft202012Validator.check_schema(_SCHEMA)
    for checkpoint in checkpoints():
        payload = stop_checkpoint_payload(checkpoint)
        _VALIDATOR.validate(payload)
        assert parse_stop_checkpoint(payload) == checkpoint
    target, exited, released = checkpoints()
    assert target.evidence() is None and exited.evidence() is None
    evidence = released.evidence()
    assert evidence is not None
    assert evidence.runtime_container_id == target.container_id


@pytest.mark.parametrize(
    "patch",
    [
        {"format_version": True},
        {"format_version": 2},
        {"extra": "unrecognized"},
        {"container_id": ""},
        {"container_id": "x" * 129},
        {"exit_code": True},
        {"exit_code": 0},
        {"exited_at": FIXED_NOW.isoformat()},
        {"released_at": FIXED_NOW.isoformat()},
        {"exited_at": "2026-10-24T09:00:00", "exit_code": 0},
        {"exited_at": "not-a-date", "exit_code": 0},
        {"released_at": 5},
    ],
)
def test_checkpoint_parser_and_contract_both_reject_invalid_partial_proofs(
    patch: dict[str, Any],
) -> None:
    payload = stop_checkpoint_payload(checkpoints()[0]) | patch
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(payload)
    with pytest.raises(InvalidRuntimeRequestError):
        parse_stop_checkpoint(payload)


@pytest.mark.parametrize("change", ("prepare", "unknown-request-field", "missing-field"))
def test_request_identity_remains_the_closed_upstream_contract(change: str) -> None:
    payload = copy.deepcopy(stop_checkpoint_payload(checkpoints()[0]))
    if change == "prepare":
        payload["request"]["action"] = "prepare"
    elif change == "unknown-request-field":
        payload["request"]["new_authority"] = True
    else:
        del payload["request"]["fencing_token"]
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(payload)
    with pytest.raises(InvalidRuntimeRequestError):
        parse_stop_checkpoint(payload)
