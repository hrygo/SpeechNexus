"""Namespace source components for a composition root without broadening methods."""

from __future__ import annotations

import copy
from typing import Any, cast


def namespaced_contract(source: dict[str, Any], prefix: str) -> dict[str, Any]:
    def rewrite(node: Any) -> Any:
        if isinstance(node, list):
            return [rewrite(value) for value in node]
        if not isinstance(node, dict):
            return node
        result = {key: rewrite(value) for key, value in node.items()}
        if "$ref" in node:
            tokens = node["$ref"].split("/")
            if tokens[:2] != ["#", "components"] or len(tokens) < 4:
                raise ValueError("Expected closed HTTP component references")
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
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete", "head", "options"}:
                operation.setdefault("security", copy.deepcopy(result["security"]))
    return result
