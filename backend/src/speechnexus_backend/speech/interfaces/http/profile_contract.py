"""Only the source-selected profile draft methods enter the served OpenAPI."""

from typing import Any

from speechnexus_contracts.profile_http import contract

from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract


def profile_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "ProfileDraft_")
