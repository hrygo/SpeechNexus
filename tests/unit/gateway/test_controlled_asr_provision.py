"""Offline provisioning and schema/consumer checks use synthetic keys only."""

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from speechnexus_gateway.infrastructure.vllm_asr import load_controlled_asr_endpoint
from tests.unit.gateway.test_controlled_asr_target import descriptor, write
from tests.unit.gateway.test_controlled_asr_target import setup as setup

ROOT = Path(__file__).resolve().parents[3]


def validator() -> Draft202012Validator:
    schema = json.loads(
        (ROOT / "deploy/engines/vllm-asr-lifecycle/controlled-asr-endpoint.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_operator_provisioning_creates_a_private_consumer_descriptor_and_refuses_overwrite(
    tmp_path: Path,
    setup: Any,
) -> None:
    request, _, _ = setup
    document = descriptor(request)
    del document["bearer_token"]
    source = write(tmp_path / "input.json", document)
    destination = tmp_path / "endpoint.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/dev-env/provision-controlled-asr.py"),
        "--input",
        str(source),
        "--output",
        str(destination),
    ]
    environment = {**os.environ, "SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY": "00" * 32}
    result = subprocess.run(command, capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stderr
    published = json.loads(destination.read_text())
    validator().validate(published)
    assert destination.stat().st_mode & 0o777 == 0o600
    configured = load_controlled_asr_endpoint(destination)
    assert configured.execution_target is not None
    assert configured.execution_target.instance_id == request.identity.instance_id
    assert configured.bearer_token is not None
    assert configured.bearer_token not in result.stdout + result.stderr
    previous = destination.read_bytes()
    retry = subprocess.run(command, capture_output=True, text=True, env=environment)
    assert retry.returncode == 1
    assert destination.read_bytes() == previous
    assert retry.stderr.strip() == "controlled_asr_provision_failed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("port", True),
        ("port", "8000"),
        ("port", 0),
        ("port", 65536),
        ("protocol", "v2"),
        ("served_model", ""),
        ("served_model", "model/x"),
        ("bearer_token", "ab" * 32 + "\n"),
        ("bearer_token", None),
        ("target.instance_generation", True),
        ("target.instance_generation", 0),
        ("target.instance_generation", "1"),
        ("target.instance_generation", 2**63),
        ("target.host_node_id", ""),
        ("target.host_node_id", "a"),
        ("target.gateway_id", "UPPERCASE"),
        ("target.pool_id", "asr-realtime"),
        ("target.instance_id", "instance\n"),
        ("target.binding_revision_id", None),
    ],
)
def test_source_contract_and_actual_loader_reject_the_same_invalid_boundary_values(
    tmp_path: Path,
    setup: Any,
    field: str,
    value: object,
) -> None:
    request, _, _ = setup
    document = deepcopy(descriptor(request))
    parent, _, name = field.rpartition(".")
    container = document[parent] if parent else document
    container[name] = value
    assert not validator().is_valid(document)
    with pytest.raises(ValueError):
        load_controlled_asr_endpoint(write(tmp_path / "endpoint.json", document))
