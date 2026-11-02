"""No caller-supplied cleanup/zero-dispatch fact enters the release boundary."""

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_stopped", True),
        ("resources_released", True),
        ("no_dispatch_metadata_absent", True),
        ("processing_receipt", {"eof": True, "closed": True}),
        ("stop_evidence_kind", "zero_dispatch_sealed"),
        ("staging_result", {"text": ""}),
    ],
)
def test_release_boundary_refuses_self_asserted_authority(field: str, value: object) -> None:
    original = {"lease_id": "lease-synthetic", "expected_version": "1"}
    assert validate_coordination("release_lease", original) is original
    with pytest.raises(ContractValidationError):
        validate_coordination("release_lease", {**original, field: value})
