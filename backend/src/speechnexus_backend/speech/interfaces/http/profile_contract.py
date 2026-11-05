"""Only the source-selected profile draft methods enter the served OpenAPI."""

from typing import Any

from speechnexus_contracts.profile_http import contract
from speechnexus_contracts.profile_lifecycle_http import contract as lifecycle_contract
from speechnexus_contracts.profile_public_http import contract as public_contract

from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract


def profile_http_contract() -> dict[str, Any]:
    result = namespaced_contract(contract(), "ProfileDraft_")
    for source, prefix in (
        (lifecycle_contract(), "ProfileLifecycle_"),
        (public_contract(), "ProfilePublic_"),
    ):
        addition = namespaced_contract(source, prefix)
        result["paths"].update(addition["paths"])
        for category, values in addition["components"].items():
            result["components"].setdefault(category, {}).update(values)
    return result
