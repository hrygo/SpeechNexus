"""Only the source-selected binding draft methods enter the served OpenAPI."""

from typing import Any

from speechnexus_contracts.binding_defaults_http import contract as defaults_contract
from speechnexus_contracts.binding_http import contract

from speechnexus_backend.speech.interfaces.http.contract_projection import namespaced_contract


def binding_http_contract(*, producer: bool = False) -> dict[str, Any]:
    legacy = namespaced_contract(contract(), "BindingDraft_")
    if producer:
        defaults = namespaced_contract(defaults_contract(), "BindingDefaults_")
        legacy["paths"].update(defaults["paths"])
        for category, definitions in defaults["components"].items():
            legacy["components"].setdefault(category, {}).update(definitions)
    return legacy
