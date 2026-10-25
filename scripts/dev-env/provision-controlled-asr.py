"""Provision a private per-generation gateway descriptor, without contacting services."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from speechnexus_gateway.domain.asr import AsrExecutionTarget
from speechnexus_gateway.infrastructure.vllm_asr import VllmAsrEndpoint
from speechnexus_node_controller.infrastructure.engine_credentials import EngineCredentials


def provision(source: Path, destination: Path, root_key: str) -> None:
    """The operator owns both deployment configs; this is not a runtime RPC."""
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as configured:
        if not stat.S_ISREG(os.fstat(configured.fileno()).st_mode):
            raise ValueError
        payload = configured.read(16385)
    if len(payload) > 16384:
        raise ValueError
    document = json.loads(payload, object_pairs_hook=_unique_object)
    if set(document) != {"protocol", "target", "address", "port", "served_model"}:
        raise ValueError
    if document["protocol"] != "vllm_api_key_v1":
        raise ValueError
    target = AsrExecutionTarget(**document["target"])
    key = EngineCredentials(root_key).for_target(**document["target"])
    VllmAsrEndpoint(
        address=document["address"],
        port=document["port"],
        served_model=document["served_model"],
        binding_revision_id=target.binding_revision_id,
        bearer_token=key,
        execution_target=target,
    )
    document["bearer_token"] = key
    payload = (json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    # Never overwrite an existing deployment; no secret reaches stdout/argv.
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        provision(
            args.input, args.output, os.environ["SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY"]
        )
    except Exception:
        # Configuration/path/parser errors must never echo either key or document.
        print("controlled_asr_provision_failed", file=sys.stderr)
        return 1
    print("controlled_asr_descriptor_created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
