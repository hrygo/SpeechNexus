"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, cast

from . import scenario_profiles_models as models
from .validation import validate_profile


def parse(name: Literal["ScenarioProfileRevision"], value: Any) -> models.ScenarioProfileRevision:
    return cast(models.ScenarioProfileRevision, validate_profile("scenario_profiles", name, value))
