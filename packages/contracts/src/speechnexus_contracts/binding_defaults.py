"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, cast

from . import binding_defaults_models as models
from .validation import validate_profile


def parse(
    name: Literal["BindingParameterDefaultsSnapshot"], value: Any
) -> models.BindingParameterDefaultsSnapshot:
    return cast(
        models.BindingParameterDefaultsSnapshot, validate_profile("binding_defaults", name, value)
    )
