"""Synthetic deployment configuration from the shared contract's positive example."""

import json
from pathlib import Path
from typing import Any


def recovery_document(*, ca: Path, port: int = 8443) -> dict[str, Any]:
    examples = json.loads(
        (Path(__file__).parents[1] / "fixtures/contracts/consumer-profiles.json").read_text()
    )["examples"]
    value: dict[str, Any] = next(
        example["value"] for example in examples if example["id"] == "gateway-recovery-valid"
    )
    value["node"] |= {"tls_ca_file": str(ca), "port": port}
    return value
