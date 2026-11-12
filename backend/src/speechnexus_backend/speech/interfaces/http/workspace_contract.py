"""Project the workspace read and write paths without colliding with other surfaces.

The prefix is a component namespace, not a claim about methods: this surface
carries the two accepted writes as well as the reads.
"""

from typing import Any

from speechnexus_contracts.admin_http import contract

from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract


def workspace_http_contract() -> dict[str, Any]:
    return namespaced_contract(contract(), "WorkspaceAdmin_")
