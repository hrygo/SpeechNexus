"""Consume the existing source-derived synthetic binding request, without wire copies."""

import copy
import json
from pathlib import Path
from typing import Any


def binding_request() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    examples = json.loads((root / "tests/fixtures/contracts/binding-http.json").read_text())[
        "examples"
    ]
    return copy.deepcopy(
        next(item["value"]["body"] for item in examples if item["id"] == "create-original")
    )
