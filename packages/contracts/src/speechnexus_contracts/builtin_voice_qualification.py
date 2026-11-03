"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, cast

from . import builtin_voice_qualification_models as models
from .validation import validate_profile


def parse(
    name: Literal["BuiltinVoiceQualificationAuthorization"], value: Any
) -> models.BuiltinVoiceQualificationAuthorization:
    return cast(
        models.BuiltinVoiceQualificationAuthorization,
        validate_profile("builtin_voice_qualification", name, value),
    )
