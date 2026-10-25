"""Native credentials scoped to a controlled instance generation.

Only the controller/provisioner holds the root key. Gateways receive the derived
opaque key for their configured target, never the root key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field

from speechnexus_node_controller.domain.intents import (
    POOL_IDS,
    RuntimeInstanceIntentRequest,
    require_identifier,
)

_KEY = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class EngineCredentials:
    root_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.root_key) is not str or _KEY.fullmatch(self.root_key) is None:
            raise ValueError("engine authentication requires a 32-byte lowercase hex root key")

    def for_instance(self, request: RuntimeInstanceIntentRequest) -> str:
        """Fencing stays in the existing DB protocol; this key fences generations."""
        return self.for_target(
            host_node_id=request.target_node_id,
            gateway_id=request.gateway_id,
            pool_id=request.pool_id,
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            binding_revision_id=request.binding_revision_id,
        )

    def for_target(
        self,
        *,
        host_node_id: str,
        gateway_id: str,
        pool_id: str,
        instance_id: str,
        instance_generation: int,
        binding_revision_id: str,
    ) -> str:
        for value in (host_node_id, gateway_id, instance_id, binding_revision_id):
            require_identifier(value, "engine_target")
        if (
            pool_id not in POOL_IDS
            or type(instance_generation) is not int
            or not 1 <= instance_generation <= 2**63 - 1
        ):
            raise ValueError("invalid native engine credential target")
        scope = [
            "speechnexus.vllm-api-key.v1",
            host_node_id,
            gateway_id,
            pool_id,
            instance_id,
            instance_generation,
            binding_revision_id,
        ]
        payload = json.dumps(scope, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return hmac.new(bytes.fromhex(self.root_key), payload, hashlib.sha256).hexdigest()
