"""Probe writer requires live, authenticated policy and runtime identity."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_backend.qualification_probe_authorizer import (
    ProbeAuthorizationError,
    require_live_gateway_policy,
)
from speechnexus_backend.settings import Settings


def test_live_policy_and_identity_must_match(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = "sha256:" + "1" * 64
    auth: dict[str, Any] = {
        "gateway_policy_digest": digest,
        "policy_revision_id": "policy-1",
        "voice_id": "voice-1",
        "voice_revision": "1",
        "voice_revision_id": "voice-rev-1",
        "native_speaker_id": "Vivian",
        "binding_revision_id": "binding-rev-1",
        "host_node_id": "node-1",
        "gateway_id": "gateway-1",
        "instance_id": "instance-1",
        "instance_generation": 2,
    }
    evidence = {
        **auth,
        "node_id": auth["host_node_id"],
        "observed_at": datetime.now(UTC).isoformat(),
    }

    class Client:
        async def query_tts_configuration_evidence(self) -> dict[str, Any]:
            return evidence

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        "speechnexus_backend.qualification_probe_authorizer.GatewayExecutionClient.from_settings",
        lambda _: Client(),
    )
    settings = Settings.model_construct()
    require_live_gateway_policy(settings, auth)
    evidence["gateway_policy_digest"] = "sha256:" + "2" * 64
    with pytest.raises(ProbeAuthorizationError):
        require_live_gateway_policy(settings, auth)
    evidence["gateway_policy_digest"] = digest
    evidence["instance_generation"] = 3
    with pytest.raises(ProbeAuthorizationError):
        require_live_gateway_policy(settings, auth)
    evidence["instance_generation"] = 2
    evidence["observed_at"] = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    with pytest.raises(ProbeAuthorizationError):
        require_live_gateway_policy(settings, auth)
