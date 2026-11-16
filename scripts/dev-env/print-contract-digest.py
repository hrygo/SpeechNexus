#!/usr/bin/env python3
"""Print the canonical contract digest this checkout's consumers are generated from.

The API reports this value at `GET /v1/nexus/version` and each Web bundle bakes
it in at build time, so the same number is what an operator compares after a
deployment: equal values mean the frontend and the API were generated from one
contract revision. This script is the offline half of that check — it recomputes
the value from `contracts/` without Docker, a browser or a running service, and
can compare it with a digest copied from the running deployment.

Usage (from the repository root):

    uv run python scripts/dev-env/print-contract-digest.py
    uv run python scripts/dev-env/print-contract-digest.py --expect sha256:…

Exit codes: 0 when it printed the digest (or the expectation matched), 1 when
`--expect` names a different digest. The computation is defined in
`scripts/dev-env/generate-contract-bindings.py`; this script imports it rather
than restating the algorithm, so the two can never disagree.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = Path(__file__).with_name("generate-contract-bindings.py")


def load_generator() -> Any:
    """Import the hyphenated generator module by path, like the CI guards do."""
    spec = importlib.util.spec_from_file_location("generate_contract_bindings", GENERATOR)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Unable to load {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expect",
        metavar="SHA256",
        help="Fail when the recomputed digest differs from SHA256 (for example "
        "the value a running GET /v1/nexus/version reported).",
    )
    arguments = parser.parse_args()
    digest = load_generator().contract_digest(ROOT / "contracts")
    print(digest)
    if arguments.expect is not None and arguments.expect.strip() != digest:
        raise SystemExit(
            "Contract digest differs: this checkout is "
            f"{digest}, the compared deployment is {arguments.expect.strip()}"
        )


if __name__ == "__main__":
    main()
