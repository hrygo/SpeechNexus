"""Project the selected read surface without colliding with speech components."""

from __future__ import annotations

import copy
from typing import Any, cast

from speechnexus_contracts.admin_http import contract


def workspace_http_contract() -> dict[str, Any]:
    source = contract()
    prefix = "WorkspaceRead_"

    def rewrite(node: Any) -> Any:
        if isinstance(node, list):
            return [rewrite(value) for value in node]
        if not isinstance(node, dict):
            return node
        result = {key: rewrite(value) for key, value in node.items()}
        if "$ref" in node:
            tokens = node["$ref"].split("/")
            if tokens[:2] != ["#", "components"] or len(tokens) < 4:
                raise ValueError("Expected a closed workspace component reference")
            tokens[3] = prefix + tokens[3]
            result["$ref"] = "/".join(tokens)
        if "security" in node:
            result["security"] = [
                {prefix + name: scopes for name, scopes in requirement.items()}
                for requirement in node["security"]
            ]
        return result

    result = cast(dict[str, Any], rewrite(source))
    result["components"] = {
        category: {prefix + name: rewrite(definition) for name, definition in values.items()}
        for category, values in source["components"].items()
    }
    for item in result["paths"].values():
        # The app also contains independently scoped speech/health operations.
        # Apply this source's anonymous/shared-credential rule to its GETs.
        for operation in item.values():
            operation.setdefault("security", copy.deepcopy(result["security"]))
    return result
