"""Project selected workspace reads without colliding with other API components."""

from typing import Any

from speechnexus_contracts.admin_http import contract

from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract


def workspace_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "WorkspaceRead_")
