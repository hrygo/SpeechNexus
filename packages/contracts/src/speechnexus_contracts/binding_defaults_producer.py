"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, cast

from . import binding_defaults_producer_models as models
from .validation import validate_profile


def parse(
    name: Literal["BindingDefaultsCreateRequest"], value: Any
) -> models.BindingDefaultsCreateRequest:
    return cast(
        models.BindingDefaultsCreateRequest,
        validate_profile("binding_defaults_producer", name, value),
    )
