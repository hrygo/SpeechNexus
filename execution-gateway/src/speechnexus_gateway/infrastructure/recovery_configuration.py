"""Activate an offline generated configuration before acquiring database/ingress resources."""

from __future__ import annotations

import json
import os
import ssl
import stat
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from speechnexus_contracts.gateway_recovery import parse

from speechnexus_gateway.infrastructure.node_stop_evidence import (
    NodeStopBinding,
    NodeStopEndpoint,
    NodeStopEvidenceSource,
)
from speechnexus_gateway.settings import Settings


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryConfiguration:
    source: NodeStopEvidenceSource
    interval_seconds: float
    batch_size: int
    pass_budget_seconds: float


def _read_regular(path: Path, limit: int) -> bytes:
    # Secret-volume symlinks are allowed. Opening a FIFO must never block startup.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError("invalid recovery file")
        content = stream.read(limit + 1)
        if not 0 < len(content) <= limit:
            raise ValueError("invalid recovery file")
        return content


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate recovery configuration key")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError("invalid recovery configuration number")


def load_recovery_configuration(settings: Settings) -> RecoveryConfiguration | None:
    """No lazy file/credential reads after activation; errors contain no deployment inputs."""
    if settings.recovery_config_file is None:
        return None
    try:
        schema = json.loads(
            files("speechnexus_contracts").joinpath("gateway_recovery.schema.json").read_text()
        )
        limits = schema["$defs"]["GatewayRecoveryConfig"]["x-nexus-file-limits"]
        value = parse(
            "GatewayRecoveryConfig",
            json.loads(
                _read_regular(settings.recovery_config_file, limits["configuration_bytes"]).decode(
                    "utf-8"
                ),
                object_pairs_hook=_unique,
                parse_constant=_nonfinite,
            ),
        )
        credential = settings.recovery_node_component_token
        if credential is None:
            raise ValueError("missing node credential")
        node = value["node"]
        context = ssl.create_default_context(
            cadata=_read_regular(Path(node["tls_ca_file"]), limits["ca_certificate_bytes"]).decode(
                "ascii"
            )
        )
        source = NodeStopEvidenceSource(
            NodeStopEndpoint(
                settings.node_id,
                node["address"],
                int(node["port"]),
                credential.get_secret_value(),
            ),
            bindings=tuple(
                NodeStopBinding(
                    node_id=settings.node_id,
                    gateway_id=settings.gateway_id,
                    pool_id=binding["pool_id"],
                    binding_revision_id=binding["binding_revision_id"],
                    approved_template_id=binding["approved_template_id"],
                    manifest_id=binding["manifest_id"],
                    config_digest=binding["config_digest"],
                )
                for binding in value["bindings"]
            ),
            ssl_context=context,
            timeout_seconds=value["request_timeout_seconds"],
            max_in_flight=1,
        )
        return RecoveryConfiguration(
            source,
            value["scan_interval_seconds"],
            int(value["batch_size"]),
            value["pass_budget_seconds"],
        )
    except Exception:
        raise ValueError("invalid gateway recovery configuration") from None
