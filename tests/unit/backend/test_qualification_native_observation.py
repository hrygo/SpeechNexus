"""Native runtime evidence must belong to the exact approved runtime."""

from dataclasses import replace

import pytest

from speechnexus_backend.speech.domain.qualification_native_observation import (
    ExpectedNativeRuntime,
    require_native_ready,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import prepare_observation
from tests.unit.test_runtime_observation_codec import _wire


def test_native_ready_requires_exact_fresh_controller_evidence() -> None:
    wire = _wire()
    wire["native_runtime_evidence"] = {
        "image_digest": "sha256:" + "1" * 64,
        "template_digest": "sha256:" + "2" * 64,
        "controller_artifact_digest": "sha256:" + "3" * 64,
        "native_auth_kind": "vllm_api_key_v1",
        "observed_at": "2026-10-23T12:00:00.500000Z",
    }
    observed = prepare_observation(wire).observation
    native = observed.native_runtime_evidence
    assert native is not None
    expected = ExpectedNativeRuntime(
        observed.binding,
        observed.instance_id,
        observed.instance_generation,
        observed.fencing_token,
        native.image_digest,
        native.template_digest,
        native.controller_artifact_digest,
    )
    now = native.observed_at_us + 1
    require_native_ready(observed, expected, now_us=now, max_age_us=10)
    for other in (
        replace(expected, instance_generation=expected.instance_generation + 1),
        replace(expected, controller_artifact_digest="sha256:" + "4" * 64),
        replace(expected, template_digest="sha256:" + "4" * 64),
        replace(expected, image_digest="sha256:" + "4" * 64),
    ):
        with pytest.raises(ValueError):
            require_native_ready(observed, other, now_us=now, max_age_us=10)
    with pytest.raises(ValueError):
        require_native_ready(
            replace(observed, native_runtime_evidence=None), expected, now_us=now, max_age_us=10
        )
    with pytest.raises(ValueError):
        require_native_ready(observed, expected, now_us=now + 11, max_age_us=10)
    with pytest.raises(ValueError):
        require_native_ready(observed, expected, now_us=now - 2, max_age_us=10)
