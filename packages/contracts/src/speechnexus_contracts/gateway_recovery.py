"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, cast

from . import gateway_recovery_models as models
from .validation import validate_profile


def parse(name: Literal["GatewayRecoveryConfig"], value: Any) -> models.GatewayRecoveryConfig:
    return cast(models.GatewayRecoveryConfig, validate_profile("gateway_recovery", name, value))
