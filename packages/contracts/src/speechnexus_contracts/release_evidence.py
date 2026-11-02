"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import release_evidence_models as models
from .validation import validate_profile


@overload
def parse(name: Literal["QualificationReport"], value: Any) -> models.QualificationReport: ...


@overload
def parse(name: Literal["CapacityManifest"], value: Any) -> models.CapacityManifest: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("release_evidence", name, value)
