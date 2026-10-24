"""Only the source-selected binding draft methods enter the served OpenAPI."""

from typing import Any

from speechnexus_contracts.binding_http import contract

from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract


def binding_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "BindingDraft_")
