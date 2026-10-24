"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, cast

from . import model_bindings_models as models
from .validation import validate_profile


def parse(name: Literal["ModelBindingRevision"], value: Any) -> models.ModelBindingRevision:
    return cast(models.ModelBindingRevision, validate_profile("model_bindings", name, value))
