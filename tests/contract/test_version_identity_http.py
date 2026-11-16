"""The version identity surface and the digest both consumers are generated from.

This is the contract half of #853: the API reports a digest at runtime and every
Web bundle bakes the same value in at build time. If the two generated constants
could drift from `contracts/`, the runtime comparison would compare nothing, so
the equality is asserted here rather than trusted.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from openapi_spec_validator import validate
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.contract_digest import CONTRACT_DIGEST
from speechnexus_contracts.version_http import contract, validate_response

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts/dev-env/generate-contract-bindings.py"
TYPESCRIPT_DIGEST = ROOT / "packages/contracts/src/contract_digest.generated.ts"
PATH = "/v1/nexus/version"
OPERATION = "getVersionIdentity"


def generated_digest_from_path() -> str:
    """Recompute the digest with the generator's own implementation."""
    spec = importlib.util.spec_from_file_location("generate_contract_bindings", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.contract_digest(ROOT / "contracts"))


def test_both_generated_digests_equal_a_fresh_computation() -> None:
    typescript = TYPESCRIPT_DIGEST.read_text()
    match = re.search(r'CONTRACT_DIGEST =\s*"(sha256:[0-9a-f]{64})";', typescript)
    assert match is not None, "the generated TypeScript digest is missing"

    recomputed = generated_digest_from_path()
    assert match.group(1) == recomputed
    assert recomputed == CONTRACT_DIGEST


def test_the_digest_covers_machine_contracts_but_not_prose() -> None:
    # A README edit must not invalidate a deployment's identity, while a machine
    # contract edit must. Assert the shape of the input set instead of a value
    # that would have to be re-frozen on every contract change.
    generator = importlib.util.spec_from_file_location("generate_contract_bindings", GENERATOR)
    assert generator is not None and generator.loader is not None
    module = importlib.util.module_from_spec(generator)
    generator.loader.exec_module(module)

    included = {
        path.relative_to(ROOT / "contracts").as_posix()
        for path in (ROOT / "contracts").rglob("*")
        if path.is_file() and path.suffix in {".json", ".yaml"}
    }
    assert "speech-openapi.yaml" in included
    assert "bindings.json" in included
    assert "README.md" not in included
    assert module.contract_digest(ROOT / "contracts").startswith("sha256:")


def test_the_generated_bundle_declares_the_operation() -> None:
    document = contract()
    operation = document["paths"][PATH]["get"]
    assert operation["operationId"] == OPERATION
    validate(document)


def test_a_matching_response_is_accepted() -> None:
    body = {"version": "0.1.0.dev0", "contract_digest": CONTRACT_DIGEST}
    response = validate_response(
        OPERATION, 200, {"body": body, "headers": {"cache-control": "no-store"}}
    )
    assert response["body"] == body


@pytest.mark.parametrize(
    "body",
    (
        {"version": "0.1.0.dev0"},
        {"version": "0.1.0.dev0", "contract_digest": "sha256:not-a-digest"},
        {"version": "0.1.0.dev0", "contract_digest": CONTRACT_DIGEST, "source_commit": "abc"},
        {"version": "", "contract_digest": CONTRACT_DIGEST},
    ),
)
def test_a_response_that_violates_the_contract_is_rejected(body: dict[str, object]) -> None:
    with pytest.raises(ContractValidationError):
        validate_response(OPERATION, 200, {"body": body, "headers": {"cache-control": "no-store"}})


def test_a_cached_response_is_rejected() -> None:
    # The identity is a build fact; a cached answer would let a mixed deployment
    # look consistent, so the no-store directive is part of the contract.
    body = {"version": "0.1.0.dev0", "contract_digest": CONTRACT_DIGEST}
    with pytest.raises(ContractValidationError):
        validate_response(OPERATION, 200, {"body": body, "headers": {}})


def test_the_operation_rejects_an_undeclared_status() -> None:
    body = {"version": "0.1.0.dev0", "contract_digest": CONTRACT_DIGEST}
    with pytest.raises(ContractValidationError):
        validate_response(OPERATION, 201, {"body": body, "headers": {"cache-control": "no-store"}})
