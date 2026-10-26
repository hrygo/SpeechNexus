"""Generation credentials are deterministic across restart, opaque and narrowly scoped."""

from dataclasses import replace
from typing import Any

import pytest
from tests.fake.controller import build_request

from speechnexus_node_controller.infrastructure.engine_credentials import EngineCredentials


def test_native_credential_has_a_fixed_independent_protocol_vector() -> None:
    request = build_request(gateway_id="gateway-local-1", binding_revision_id="binding-asr-v1")
    credential = EngineCredentials("00" * 32)
    assert credential.for_instance(request) == (
        "706124da577c4464b5bff275d2c777a6b574038cf90a39e5d84def24ad860ee9"
    )
    assert EngineCredentials("00" * 32).for_instance(request) == credential.for_instance(request)
    assert "00" * 32 not in repr(credential)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("target_node_id", "node-other"),
        ("gateway_id", "gateway-other"),
        ("pool_id", "asr-realtime"),
        ("instance_id", "instance-other"),
        ("instance_generation", 2),
        ("binding_revision_id", "binding-other"),
    ],
)
def test_changing_any_target_dimension_changes_its_native_key(name: str, value: Any) -> None:
    request = build_request()
    credentials = EngineCredentials("00" * 32)
    assert credentials.for_instance(request) != credentials.for_instance(
        replace(request, **{name: value})
    )


def test_new_intent_or_owner_token_does_not_change_the_same_generation_key() -> None:
    request = build_request()
    credentials = EngineCredentials("00" * 32)
    assert credentials.for_instance(request) == credentials.for_instance(
        replace(request, intent_id="intent-recovery", fencing_token=2)
    )
    assert EngineCredentials("11" * 32).for_instance(request) != credentials.for_instance(request)


@pytest.mark.parametrize("key", ["", "a" * 63, "A" * 64, "g" * 64, "a" * 65, "\n" + "a" * 64])
def test_invalid_root_keys_fail_without_echoing_the_key(key: str) -> None:
    with pytest.raises(ValueError) as failure:
        EngineCredentials(key)
    if key:
        assert key not in str(failure.value)
