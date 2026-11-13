"""Refuse service-prefixed environment variables that name no declared setting.

Every settings surface in this repository loads with pydantic ``extra="ignore"``
or an equivalent hand-written ``from_env``: a variable whose name carries the
service prefix but does not match a declared field is dropped without a word.
The operator sees a clean start and believes the capability is enabled. The
canonical sample is a one-letter typo -- ``SPEECHNEXUS_XXX_DATABASE_URLL`` -- but
the same hole swallows a key that belongs to *another* process, for example
``SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL`` in the API container (only the
job-dispatch worker reads it), and a key retired by an earlier release.

``extra="forbid"`` cannot close the hole: the process environment also holds
``PATH``, ``HOSTNAME``, CI-injected variables and operator tooling, and refusing
those would break every deployment. The rule is therefore prefix-scoped, not
process-wide: only a name that starts with the prefix this surface declares can
be "a setting", so only such a name is judged. Two outcomes follow, and the
split is deliberate:

* **Refusal** -- the name matches no declared field and no registered non-setting
  plane. Nothing can consume it, so a start that hides it is always a lie. The
  message names the variable, the closest declared names and the repair.
* **Named warning** -- the name is registered as belonging to another process
  (another service, another entrypoint of this package, the startup gate, the
  GPU node bootstrap). The value is still inert *here*, but one ``.env`` shared
  by several containers or a single Kubernetes ConfigMap is a legitimate
  deployment shape, so refusing would break working deployments. The warning
  names the owner so the operator can move or drop the value.

Deployment-plane names that no runtime process reads at all (Compose
interpolation inputs such as ``SPEECHNEXUS_API_DATABASE_URL`` and
``*_ROLE_PASSWORD``, the test harness, port mappings) are registered as
non-runtime and stay silent: they reach a container only when an operator
forwards a whole env file, and they are not evidence of a misconfiguration.

The registries below are mirrored by ``tests/ci/service_environment_keys.py``,
which re-derives every settings surface from the sources and fails when a
declared key, a service prefix or a registered plane drifts out of sync. The
scan engine is duplicated verbatim in the gateway and node-controller packages
because those packages may not import the backend; ``tests/ci`` compares the
three copies by AST so the duplication cannot rot.
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


# The backend declares `SPEECHNEXUS_`, which contains every other surface's
# namespace, so the API scan must recognise names it does not consume.
PLANES: tuple[NonSettingPlane, ...] = (
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_GATEWAY_",),
        keys=(
            "SPEECHNEXUS_GATEWAY_ID",
            "SPEECHNEXUS_GATEWAY_LOCK_DIR",
            "SPEECHNEXUS_GATEWAY_SPOOL_DIR",
        ),
        owner="the execution gateway service",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_WORKBENCH_",),
        keys=(
            "SPEECHNEXUS_WORKBENCH_TEST_DSN",
            "SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT",
        ),
        owner="the workbench service",
        runtime=True,
    ),
    # `SPEECHNEXUS_CONTROLLER_NODEID` is a real typo this repository's own suite
    # used to tolerate for production identity; it is deliberately unregistered
    # so the controller's own scan refuses it instead of warning about it.
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_CONTROLLER_",),
        owner="the node controller service",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_INSPECTION_",),
        owner="the asset-inspector service",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_JOB_DISPATCH_",),
        keys=("SPEECHNEXUS_EXECUTION_RESULT_BUCKET",),
        owner="the job-dispatch worker entrypoint",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_JOB_GATE_",),
        owner="the job-dispatch startup gate",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_DISABLE_",),
        owner="the binding-disable worker entrypoint",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_RUNTIME_DELIVERY_",),
        owner="the runtime-delivery worker entrypoint",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_",),
        owner="the job-cancellation worker entrypoint",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_VOICE_EVIDENCE_",),
        owner="the voice-evidence worker entrypoint",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_TTS_SETTLEMENT_",),
        keys=("SPEECHNEXUS_TTS_GATEWAY_ID", "SPEECHNEXUS_TTS_HOST_NODE_ID"),
        owner="the TTS settlement worker entrypoint",
        runtime=True,
    ),
    NonSettingPlane(
        keys=(
            "SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL",
            "SPEECHNEXUS_PUBLICATION_TRANSACTION_TIMEOUT_SECONDS",
        ),
        owner="the binding publication command line",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_QUALIFICATION_",),
        owner="the builtin-voice qualification command line",
        runtime=True,
    ),
    NonSettingPlane(
        keys=("SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL",),
        owner="the source-approval command line",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_ENGINE_",),
        owner="the engine-registration command line and the engine deployment",
        runtime=True,
    ),
    NonSettingPlane(
        prefixes=(
            "SPEECHNEXUS_ASR_",
            "SPEECHNEXUS_TTS_",
            "SPEECHNEXUS_APPROVED_ASR_TEMPLATES",
            "SPEECHNEXUS_VAD_MODEL_FILE",
            "SPEECHNEXUS_AUDIO_CORPUS_ROOT",
        ),
        owner="the GPU node deployment, which runs no Python settings surface",
        runtime=True,
    ),
    # Deployment plane: no runtime process reads these. Compose interpolates them
    # on the host, the runbooks read them outside a container, and the test
    # harness owns its own SPEECHNEXUS_TEST_* names.
    NonSettingPlane(
        prefixes=("SPEECHNEXUS_TEST_",),
        owner="the test harness",
    ),
    NonSettingPlane(
        suffixes=("_ROLE_PASSWORD", "_HOST_PORT"),
        owner="Compose interpolation and the role-provisioning runbook",
    ),
    NonSettingPlane(
        keys=(
            "SPEECHNEXUS_ADMIN_PROFILES_OUTPUT",
            "SPEECHNEXUS_ADMIN_WORKSPACES_OUTPUT",
            "SPEECHNEXUS_ADMISSION_MAX_ATTEMPTS",
            "SPEECHNEXUS_ADMISSION_WORKSPACE",
            "SPEECHNEXUS_API_DATABASE_URL",
            "SPEECHNEXUS_B_HANDOFF_ROOT",
            "SPEECHNEXUS_B_RESTORE_DIR",
            "SPEECHNEXUS_BUILD_CA_FILE",
            "SPEECHNEXUS_MIGRATION_DATABASE_URL",
            "SPEECHNEXUS_MYSQL_ROOT_PASSWORD",
            "SPEECHNEXUS_PRIVATE_CONFIG_DIR",
            "SPEECHNEXUS_REGISTRATION_DATABASE_URL",
            "SPEECHNEXUS_REQUIRED_BY_COMPOSE",
            "SPEECHNEXUS_RUNTIME_GID",
            "SPEECHNEXUS_RUNTIME_UID",
            "SPEECHNEXUS_TOOLCHAIN_ENV",
        ),
        owner="Compose interpolation, the runbooks and the host tooling",
    ),
)

# The workbench owns `SPEECHNEXUS_WORKBENCH_`, so it needs only the host-side and
# test names inside that namespace; everything else must be a declared field.
WORKBENCH_PLANES: tuple[NonSettingPlane, ...] = (
    NonSettingPlane(
        suffixes=("_HOST_PORT",),
        keys=(
            "SPEECHNEXUS_WORKBENCH_TEST_DSN",
            "SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT",
        ),
        owner="Compose interpolation, the browser probes and the test harness",
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
