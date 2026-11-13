"""Refuse gateway-prefixed environment variables that name no declared setting.

``Settings`` loads with pydantic ``extra="ignore"``, so ``SPEECHNEXUS_GATEWAY_*``
names that match no field are dropped without a word: a misspelled
``SPEECHNEXUS_GATEWAY_CONTROL_DATABSE_URL`` starts clean and leaves the operator
believing execution control is configured. ``extra="forbid"`` cannot be used --
the process environment also holds ``PATH``, ``HOSTNAME`` and CI variables -- so
the scan is limited to the prefix this surface declares.

The engine is a verbatim copy of ``speechnexus_backend.environment``; the
gateway package may not import the backend, and ``tests/ci`` compares the copies
by AST so the duplication cannot rot. Only the registry below is package-local.
"""

from __future__ import annotations

import difflib
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger(__name__)

_MAX_CANDIDATES = 3
_CANDIDATE_CUTOFF = 0.75


@dataclass(frozen=True, slots=True)
class NonSettingPlane:
    """Prefixes, suffixes or exact names that are not settings keys.

    ``owner`` is the process or deployment input that does consume the name.
    ``runtime`` distinguishes a name another runtime process owns (warned) from
    a deployment-plane input that no process owns inside a container (silent).
    Matching is exact name first, then the longest matching prefix or suffix, so
    a specific name always beats the family that would otherwise swallow it.
    """

    prefixes: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    owner: str = ""
    runtime: bool = False


PLANES: tuple[NonSettingPlane, ...] = (
    # Host-side inputs: Compose interpolates them on the host and forwards the
    # derived name (SPEECHNEXUS_GATEWAY_GATEWAY_ID), so forwarding a whole env
    # file must not be refused. Nothing inside the container reads them.
    NonSettingPlane(
        keys=(
            "SPEECHNEXUS_GATEWAY_ID",
            "SPEECHNEXUS_GATEWAY_LOCK_DIR",
            "SPEECHNEXUS_GATEWAY_SPOOL_DIR",
        ),
        suffixes=("_ROLE_PASSWORD", "_HOST_PORT"),
        owner="Compose interpolation and the role-provisioning runbook",
    ),
)


def declared_environment_keys(settings_cls: type) -> frozenset[str]:
    """Return the environment names ``settings_cls`` declares, derived from its fields."""
    config: Any = getattr(settings_cls, "model_config", {})
    prefix = config.get("env_prefix") or ""
    keys: set[str] = set()
    for name, field in settings_cls.model_fields.items():  # type: ignore[attr-defined]
        alias = field.validation_alias
        if isinstance(alias, str):
            keys.add(alias)
            continue
        choices = getattr(alias, "choices", None)
        if choices:
            keys.update(choice for choice in choices if isinstance(choice, str))
            continue
        keys.add(f"{prefix}{name.upper()}")
    return frozenset(keys)


def environment_prefixes(settings_cls: type) -> tuple[str, ...]:
    """Return the prefixes ``settings_cls`` declares; the scan is limited to them."""
    config: Any = getattr(settings_cls, "model_config", {})
    prefix = config.get("env_prefix") or ""
    return (prefix,) if prefix else ()


def _matching_plane(name: str, planes: Iterable[NonSettingPlane]) -> NonSettingPlane | None:
    """Return the registered non-setting plane for ``name``, most specific first."""
    candidates = tuple(planes)
    for plane in candidates:
        if name in plane.keys:
            return plane
    matches: list[tuple[int, NonSettingPlane]] = []
    for plane in candidates:
        for prefix in plane.prefixes:
            if name.startswith(prefix):
                matches.append((len(prefix), plane))
        for suffix in plane.suffixes:
            if name.endswith(suffix):
                matches.append((len(suffix), plane))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def close_candidates(
    name: str, known: Iterable[str], limit: int = _MAX_CANDIDATES
) -> tuple[str, ...]:
    """Return the declared names closest to ``name`` for a spelling suggestion."""
    return tuple(difflib.get_close_matches(name, list(known), n=limit, cutoff=_CANDIDATE_CUTOFF))


def unknown_prefixed_variables(
    settings_cls: type,
    environ: Mapping[str, str] | None = None,
    *,
    planes: Iterable[NonSettingPlane],
) -> tuple[dict[str, tuple[str, ...]], tuple[tuple[str, NonSettingPlane], ...]]:
    """Split unmatched prefixed variables into refused names and warned owners.

    Unrelated process variables are never inspected: only names that start with a
    prefix this surface declares enter the judgement, and a name in the
    deployment plane is dropped silently.
    """
    source = os.environ if environ is None else environ
    prefixes = environment_prefixes(settings_cls)
    known = declared_environment_keys(settings_cls)
    unknown: dict[str, tuple[str, ...]] = {}
    foreign: list[tuple[str, NonSettingPlane]] = []
    for raw in sorted(source):
        name = raw.upper()
        if not name or not any(name.startswith(prefix) for prefix in prefixes):
            continue
        if name in known:
            continue
        plane = _matching_plane(name, planes)
        if plane is not None:
            if plane.runtime:
                foreign.append((raw, plane))
            continue
        unknown[raw] = close_candidates(name, known)
    return unknown, tuple(foreign)


def refuse_unrecognised_prefixed_environment(
    settings_cls: type,
    entrypoint: str,
    environ: Mapping[str, str] | None = None,
    *,
    planes: Iterable[NonSettingPlane],
) -> None:
    """Fail the start on unmatched prefixed variables and warn on foreign owners.

    Raises ``ValueError`` so pydantic reports it like every other startup refusal
    in this repository: the message carries names only, never values.
    """
    unknown, foreign = unknown_prefixed_variables(settings_cls, environ, planes=planes)
    for raw, plane in foreign:
        _LOG.warning(
            "%s is set for %s but is only consumed by %s; it has no effect here. "
            "Unset it or move it to that process.",
            raw,
            entrypoint,
            plane.owner,
        )
    if not unknown:
        return
    details = "; ".join(_describe_unknown(raw, candidates) for raw, candidates in unknown.items())
    raise ValueError(
        f"Refusing to start {entrypoint} with environment variable(s) that match no "
        f"declared setting: {details}. Fix the spelling or unset the variable. Values "
        "without a service prefix (PATH, HOSTNAME, CI variables) are never inspected."
    )


def _describe_unknown(raw: str, candidates: tuple[str, ...]) -> str:
    if not candidates:
        return f"{raw} (no declared setting resembles it)"
    return f"{raw} (did you mean {', '.join(candidates)}?)"
