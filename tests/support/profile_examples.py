"""One frozen, synthetic consumer example, shared by the new behavior tests."""

import copy
import json
from pathlib import Path
from typing import Any


def profile_request() -> dict[str, Any]:
    examples = json.loads(
        (Path(__file__).parents[1] / "fixtures/contracts/consumer-profiles.json").read_text()
    )["examples"]
    return copy.deepcopy(
        next(
            e["value"]
            for e in examples
            if (e["schema"] == "ProfileRevisionCreateRequest" and e["valid"])
        )
    )
