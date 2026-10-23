"""Generated types, authoritative offline validation, and matching OpenAPI schemas."""

import json
from functools import lru_cache
from importlib.resources import files
from typing import Annotated, Any

from pydantic import BeforeValidator, WithJsonSchema
from speechnexus_contracts import execution_models as models
from speechnexus_contracts.execution import parse


@lru_cache
def wire_schema(name: str) -> dict[str, Any]:
    """Inline only local bundle references for these small nonrecursive control shapes."""
    bundle = json.loads(
        files("speechnexus_contracts").joinpath("execution.schema.json").read_text()
    )

    def expand(value: Any) -> Any:
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            prefix = "#/$defs/"
            if not value["$ref"].startswith(prefix):
                raise ValueError("control schemas must contain only bundled local references")
            target = bundle["$defs"][value["$ref"].removeprefix(prefix)]
            return expand({**target, **{key: item for key, item in value.items() if key != "$ref"}})
        return {key: expand(item) for key, item in value.items()}

    schema: dict[str, Any] = expand(bundle["$defs"][name])
    return schema


GatewayBarrierPayload = Annotated[
    models.GatewayFencingBarrierRequest,
    BeforeValidator(lambda value: parse("GatewayFencingBarrierRequest", value)),
    WithJsonSchema(wire_schema("GatewayFencingBarrierRequest")),
]
GatewayBarrierResponse = Annotated[
    models.GatewayFencingBarrierResponse,
    BeforeValidator(lambda value: parse("GatewayFencingBarrierResponse", value)),
    WithJsonSchema(wire_schema("GatewayFencingBarrierResponse")),
]
