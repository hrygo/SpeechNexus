"""Every copied gateway ``_PrivateConfiguration`` keeps the same five safety switches.

Four runtime modules under ``execution-gateway/src`` load a private deployment document and
each declares its own ``class _PrivateConfiguration(BaseModel)`` as the base of the config
classes. The ``ConfigDict`` keys are boundaries, not style:

* ``hide_input_in_errors`` keeps rejected values out of pydantic's rendered
  ``ValidationError`` -- and the values here include ``endpoint`` URLs that can embed
  credentials and absolute file paths;
* ``extra="forbid"`` turns a misspelt option into a failure instead of an option that is
  silently ignored and never takes effect;
* ``frozen=True`` makes a validated startup snapshot immutable at runtime;
* ``strict=True`` refuses implicit coercion such as ``"3"`` for a float timeout;
* ``allow_inf_nan=False`` refuses ``inf``/``nan`` timeouts.

Nothing asserted any of them before: ``grep -rn "_PrivateConfiguration|hide_input_in_errors"
tests/`` had no hits, and deleting ``hide_input_in_errors`` or relaxing ``extra`` to
``"ignore"`` in any copy left the whole suite green. This file pins the five keys one by one
*and* proves the observable behaviour, so a copy that keeps a key but loses its effect still
fails.

The census is rooted at ``execution-gateway/src``: a fifth declaration without an entry in
``COPIES`` fails the last test. The rendered-message assertions below cover ``str(error)``
(the pydantic surface that reaches logs); ``ValidationError.errors()``/``.json()`` still
carry ``input`` by pydantic design and are not used as the leak boundary here.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

GATEWAY_ROOT = Path(__file__).resolve().parents[3] / "execution-gateway"
SOURCE_ROOT = GATEWAY_ROOT / "src"
DECLARATION = re.compile(r"^\s*class _PrivateConfiguration\b", re.MULTILINE)

BOUNDARIES: tuple[tuple[str, object], ...] = (
    ("extra", "forbid"),
    ("frozen", True),
    ("strict", True),
    ("allow_inf_nan", False),
    ("hide_input_in_errors", True),
)

UNKNOWN_KEY = "unexpected_private_option"
UNKNOWN_VALUE = "s3cr3t-extra-value-4f1d2c"
CREDENTIAL = "s3cr3t@tts.internal/v1"


@dataclass(frozen=True, slots=True)
class Copy:
    """One copied base class, the concrete ``_Objects`` used to probe it, and its float."""

    module: str
    #: Minimal valid ``_Objects`` payload; must name exactly the model's own fields.
    objects: dict[str, object]
    #: Float field used for the ``strict`` and ``allow_inf_nan`` behaviour probes.
    timeout_field: str


# One entry per declaration. ``_Objects`` is the concrete subclass every copy shares, so the
# behaviour probes exercise the real inherited configuration rather than a synthetic model.
COPIES = (
    Copy(
        "speechnexus_gateway.file_asr_runtime",
        {
            "endpoint": "https://asr.invalid/v1",
            "input_bucket": "speech-inputs",
            "result_bucket": "speech-results",
            "spool_directory": "/var/lib/speechnexus/spool",
            "tls_ca_file": None,
            "result_timeout_seconds": 3.0,
        },
        "result_timeout_seconds",
    ),
    Copy(
        "speechnexus_gateway.file_tts_runtime",
        {
            "endpoint": "https://tts.invalid/v1",
            "result_bucket": "speech-results",
            "tls_ca_file": None,
            "result_timeout_seconds": 3.0,
        },
        "result_timeout_seconds",
    ),
    Copy(
        "speechnexus_gateway.file_voice_clone_runtime",
        {
            "endpoint": "https://voice-clone.invalid/v1",
            "input_bucket": "speech-inputs",
            "result_bucket": "speech-results",
            "spool_directory": "/var/lib/speechnexus/spool",
            "tls_ca_file": None,
            "result_timeout_seconds": 3.0,
        },
        "result_timeout_seconds",
    ),
    Copy(
        "speechnexus_gateway.file_voice_design_runtime",
        {
            "endpoint": "https://voice-design.invalid/v1",
            "result_bucket": "speech-results",
            "tls_ca_file": None,
            "result_timeout_seconds": 3.0,
        },
        "result_timeout_seconds",
    ),
)


def copy_id(entry: Copy) -> str:
    return entry.module.rsplit(".", 1)[-1]


def module(entry: Copy) -> Any:
    return importlib.import_module(entry.module)


def private_configuration(entry: Copy) -> Any:
    return module(entry)._PrivateConfiguration


def objects_class(entry: Copy) -> Any:
    return module(entry)._Objects


def build(entry: Copy, **overrides: object) -> Any:
    """Validate one real ``_Objects``; overrides replace a single registered field."""

    return objects_class(entry).model_validate({**entry.objects, **overrides})


def discover_declaration_modules() -> set[str]:
    """Dotted module paths of every ``class _PrivateConfiguration`` declaration in src."""

    discovered: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        if not DECLARATION.search(path.read_text(encoding="utf-8")):
            continue
        parts = list(path.relative_to(SOURCE_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        discovered.add(".".join(parts))
    return discovered


@pytest.mark.parametrize("entry", COPIES, ids=copy_id)
@pytest.mark.parametrize(
    ("key", "expected"),
    BOUNDARIES,
    ids=[key for key, _expected in BOUNDARIES],
)
def test_each_copy_declares_the_boundary_key(entry: Copy, key: str, expected: object) -> None:
    config = private_configuration(entry).model_config

    assert key in config, (
        f"{entry.module}._PrivateConfiguration drops {key!r}; pydantic then falls back to "
        f"its permissive default instead of the boundary"
    )
    observed = config[key]
    if isinstance(expected, bool):
        assert observed is expected, (
            f"{entry.module}._PrivateConfiguration sets {key}={observed!r}, expected {expected!r}"
        )
    else:
        assert observed == expected, (
            f"{entry.module}._PrivateConfiguration sets {key}={observed!r}, expected {expected!r}"
        )


@pytest.mark.parametrize("entry", COPIES, ids=copy_id)
def test_registered_objects_payload_matches_the_model(entry: Copy) -> None:
    """A new required field makes the registry stale, so fail here with the field names."""

    model = objects_class(entry)
    registered = set(entry.objects)
    declared = set(model.model_fields)

    assert registered == declared, (
        f"{entry.module}._Objects fields changed; update the registry.\n"
        f"unregistered: {sorted(declared - registered)}\n"
        f"stale: {sorted(registered - declared)}"
    )
    annotation = model.model_fields[entry.timeout_field].annotation
    assert annotation is float, (
        f"{entry.module}._Objects.{entry.timeout_field} is {annotation!r}, not float; the "
        "non-finite probe needs a float field or a documented substitute"
    )


@pytest.mark.parametrize("entry", COPIES, ids=copy_id)
def test_extra_key_is_rejected_and_its_value_is_not_rendered(entry: Copy) -> None:
    """``extra="forbid"`` refuses the key; ``hide_input_in_errors`` hides its value."""

    with pytest.raises(ValidationError) as raised:
        build(entry, **{UNKNOWN_KEY: UNKNOWN_VALUE})

    message = str(raised.value)
    assert "extra_forbidden" in message, f"{entry.module} accepted an unknown configuration key"
    assert UNKNOWN_KEY in message, f"{entry.module} refused the wrong key: {message!r}"
    # Without hide_input_in_errors pydantic appends input_value='...' and this fails, while
    # the key is still refused -- so the two switches are pinned by different assertions.
    assert UNKNOWN_VALUE not in message, (
        f"{entry.module} rendered the rejected extra value in its validation error"
    )


@pytest.mark.parametrize("entry", COPIES, ids=copy_id)
def test_rejected_endpoint_does_not_render_its_credentials(entry: Copy) -> None:
    """The sensitive field these models hold must not be echoed either."""

    with pytest.raises(ValidationError) as raised:
        build(entry, endpoint=[CREDENTIAL])

    message = str(raised.value)
    assert "string_type" in message, f"{entry.module} accepted a list endpoint: {message!r}"
    assert CREDENTIAL not in message, (
        f"{entry.module} rendered endpoint credentials in its validation error"
    )


@pytest.mark.parametrize("entry", COPIES, ids=copy_id)
def test_timeout_string_is_not_coerced(entry: Copy) -> None:
    """``strict=True``: ``"3"`` must not silently become ``3.0`` seconds."""

    with pytest.raises(ValidationError) as raised:
        build(entry, **{entry.timeout_field: "3"})

    message = str(raised.value)
    assert "float_type" in message, (
        f"{entry.module} coerced a string timeout instead of refusing it: {message!r}"
    )


@pytest.mark.parametrize("entry", COPIES, ids=copy_id)
@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan")],
    ids=["positive-infinity", "negative-infinity", "nan"],
)
def test_non_finite_timeout_is_refused(entry: Copy, value: float) -> None:
    """``allow_inf_nan=False``: an unbounded timeout must not validate."""

    with pytest.raises(ValidationError) as raised:
        build(entry, **{entry.timeout_field: value})

    assert "finite_number" in str(raised.value), (
        f"{entry.module} accepted {value!r} as a timeout: {raised.value!r}"
    )


@pytest.mark.parametrize("entry", COPIES, ids=copy_id)
def test_validated_objects_are_frozen(entry: Copy) -> None:
    """``frozen=True``: the startup snapshot cannot be rewritten after validation."""

    instance = build(entry)

    with pytest.raises(ValidationError) as raised:
        instance.endpoint = "https://mutated.invalid/v1"

    assert "frozen_instance" in str(raised.value)


def test_table_covers_every_private_configuration_declaration() -> None:
    discovered = discover_declaration_modules()
    tabled = {entry.module for entry in COPIES}

    assert len(COPIES) == 4, "the table is not one entry per runtime copy"
    assert discovered == tabled, (
        "An execution-gateway _PrivateConfiguration declaration is not classified here.\n"
        f"untested: {sorted(discovered - tabled)}\n"
        f"stale: {sorted(tabled - discovered)}"
    )
