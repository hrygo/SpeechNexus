"""Every copied ``_PoolDiagnostics`` redacts its pool name before logging.

Seventeen copies of ``class _PoolDiagnostics`` live under ``backend/src`` and repeat one
security-relevant shape: when a log line mentions the connection pool, replace the whole
message with a fixed sentence, drop the recorded arguments, and drop the exception. A
``psycopg_pool`` warning names its pool -- usually a DSN with host and credentials -- and
carries the driver traceback, so a copy that stops redacting leaks connection details into
the log.

Only five copies were pinned at all by their own functional suites (``test_profile_http.py``,
``test_pronunciation_http.py``, ``test_binding_http.py``, ``test_audit_reader_runtime.py``,
``test_model_instance_runtime.py``), and none of those asserts that ``exc_text`` and
``stack_info`` are cleared, that ``args`` is emptied, or that ``filter()`` keeps returning
``True``. Deleting the exception-clearing line, mistyping a hardcoded pool name, or returning
``False`` from any of the other twelve copies left every pre-existing suite green.

The copies differ in one way that is preserved rather than flattened: fourteen take the pool
name through ``__init__``, while ``tts_settlement_worker``, ``voice_evidence_worker`` and
``recover_tts`` hardcode a literal and define no constructor at all. The table records which
is which, and the matrix asserts the matching behaviour each shape actually has.

One shared matrix runs against every copy. The discovery census keeps the table complete: a
new ``class _PoolDiagnostics`` declaration without an entry here fails. The two copies that
already had partial coverage are still included -- duplicate coverage is harmless and keeps
them from drifting away from the matrix.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3] / "backend"
SOURCE_ROOT = BACKEND_ROOT / "src"
DIAGNOSTICS_DECLARATION = re.compile(r"^\s*class _PoolDiagnostics\b", re.MULTILINE)

#: Marker for connection detail that must never survive redaction.
SECRET = "synthetic-secret"


@dataclass(frozen=True, slots=True)
class Implementation:
    """One copied diagnostics filter, its fixed sentence, and where its pool name lives."""

    module: str
    #: The sentence the copy substitutes for a matching message.
    redacted: str
    #: ``None`` means the pool name is a constructor argument; otherwise it is this literal.
    hardcoded_pool: str | None


IMPLEMENTATIONS = (
    Implementation(
        "speechnexus_backend.approve_builtin_voice_source",
        "Source approval database unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.attest_builtin_voice",
        "Operator attestation database unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.audit_reader_runtime",
        "Audit database connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.binding_disable_worker",
        "Disable coordinator database unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.binding_list_runtime",
        "Binding list database connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.binding_runtime",
        "Binding database connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.job_runtime",
        "Public Job connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.model_catalog_runtime",
        "Model catalogue connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.model_instance_runtime",
        "Model instance database connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.profile_runtime",
        "Profile database connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.pronunciation_runtime",
        "Pronunciation database connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.quarantine_release_runtime",
        "Quarantine release database unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.recover_tts",
        "TTS recovery database unavailable",
        "tts-original-recovery",
    ),
    Implementation(
        "speechnexus_backend.resource_pool_runtime",
        "Resource pool database connection unavailable",
        None,
    ),
    Implementation(
        "speechnexus_backend.tts_settlement_worker",
        "TTS settlement database unavailable",
        "tts-settlement-",
    ),
    Implementation(
        "speechnexus_backend.voice_evidence_worker",
        "Voice evidence database unavailable",
        "voice-evidence-",
    ),
    Implementation(
        "speechnexus_backend.workspace_writer_runtime",
        "Workspace writer database connection unavailable",
        None,
    ),
)


def short_name(entry: Implementation) -> str:
    return entry.module.rsplit(".", 1)[-1]


def module(entry: Implementation) -> Any:
    return importlib.import_module(entry.module)


def factory(entry: Implementation) -> Any:
    return module(entry)._PoolDiagnostics


def configured_pool(entry: Implementation) -> str:
    """A DSN-shaped pool name, the way ``psycopg_pool`` names pools in production."""

    stem = short_name(entry)
    return f"postgresql://diag-{stem}:{SECRET}@pool-{stem}.internal:5432/speechnexus"


def pool_name(entry: Implementation) -> str:
    """The exact text this copy must treat as its own pool name."""

    if entry.hardcoded_pool is not None:
        return entry.hardcoded_pool
    return configured_pool(entry)


def build(entry: Implementation) -> Any:
    cls = factory(entry)
    if entry.hardcoded_pool is not None:
        return cls()
    return cls(configured_pool(entry))


def hit_record(entry: Implementation) -> logging.LogRecord:
    """A warning whose message and arguments both carry the pool name and a password."""

    return logging.LogRecord(
        "psycopg.pool",
        logging.WARNING,
        __file__,
        1,
        "pool %s reported a failure: password=%s",
        (pool_name(entry), SECRET),
        None,
    )


def unrelated_record() -> logging.LogRecord:
    """A warning about some other pool, carrying the same sensitive marker."""

    return logging.LogRecord(
        "psycopg.pool",
        logging.WARNING,
        __file__,
        1,
        "pool %s is warming up: password=%s",
        ("unrelated-pool", SECRET),
        None,
    )


def discover_diagnostic_modules() -> set[str]:
    """Dotted module paths of every ``class _PoolDiagnostics`` declaration in backend/src."""

    discovered: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        if not DIAGNOSTICS_DECLARATION.search(path.read_text(encoding="utf-8")):
            continue
        parts = list(path.relative_to(SOURCE_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        discovered.add(".".join(parts))
    return discovered


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=short_name)
def test_a_matching_message_is_replaced_by_the_fixed_sentence(entry: Implementation) -> None:
    record = hit_record(entry)
    assert pool_name(entry) in record.getMessage()

    assert build(entry).filter(record) is True

    assert record.getMessage() == entry.redacted, (
        f"{entry.module} did not substitute its fixed sentence"
    )
    # The original arguments would otherwise smuggle the pool name back into the log.
    assert record.args == (), f"{entry.module} kept the original log arguments: {record.args!r}"
    assert pool_name(entry) not in record.getMessage()
    assert SECRET not in record.getMessage()


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=short_name)
def test_a_matching_message_drops_every_recorded_exception_detail(
    entry: Implementation,
) -> None:
    # Typed ``Any`` so mypy does not narrow the attributes this test deliberately clears.
    record: Any = hit_record(entry)
    record.exc_info = (ValueError, ValueError(f"connection string {SECRET} rejected"), None)
    record.exc_text = f"Traceback (most recent call last):\nValueError: {SECRET}"
    record.stack_info = f"Stack (most recent call last):\n  password={SECRET}"
    assert record.exc_info is not None
    assert record.exc_text is not None
    assert record.stack_info is not None

    assert build(entry).filter(record) is True

    assert record.exc_info is None, (
        f"{entry.module} kept exc_info, which carries the driver traceback"
    )
    assert record.exc_text is None, f"{entry.module} kept the cached formatted traceback"
    assert record.stack_info is None, f"{entry.module} kept the recorded stack"


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=short_name)
def test_an_unrelated_message_passes_through_unchanged(entry: Implementation) -> None:
    record = unrelated_record()
    arguments = record.args
    exc_info = (ValueError, ValueError("unrelated failure"), None)
    record.exc_info = exc_info
    record.exc_text = "Traceback (most recent call last):\nValueError: unrelated failure"
    record.stack_info = "Stack (most recent call last):\n  at unrelated"
    rendered = record.getMessage()
    assert pool_name(entry) not in rendered

    assert build(entry).filter(record) is True

    assert record.getMessage() == rendered, (
        f"{entry.module} rewrote an unrelated message: {record.getMessage()!r}"
    )
    assert record.args == arguments
    assert record.msg == "pool %s is warming up: password=%s"
    assert record.exc_info is exc_info
    assert record.exc_text == "Traceback (most recent call last):\nValueError: unrelated failure"
    assert record.stack_info == "Stack (most recent call last):\n  at unrelated"


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=short_name)
def test_filter_never_suppresses_the_record_it_redacted(entry: Implementation) -> None:
    """Returning ``False`` would make logging drop the warning instead of cleaning it."""

    matching = hit_record(entry)
    unrelated = unrelated_record()

    assert build(entry).filter(matching) is True, (
        f"{entry.module} returned False for a redacted record, discarding the warning"
    )
    assert build(entry).filter(unrelated) is True, (
        f"{entry.module} returned False for an unrelated record, discarding it"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=short_name)
def test_each_copy_follows_the_source_the_table_records(entry: Implementation) -> None:
    cls = factory(entry)
    assert issubclass(cls, logging.Filter), f"{entry.module}._PoolDiagnostics is not a filter"

    if entry.hardcoded_pool is None:
        assert "__init__" in cls.__dict__, (
            f"{entry.module} is tabled as taking its pool name from a constructor"
        )
        parameters = list(inspect.signature(cls).parameters.values())
        assert len(parameters) == 1, (
            f"{entry.module} must take exactly one pool name: {parameters!r}"
        )
        assert parameters[0].default is inspect.Parameter.empty, (
            f"{entry.module} must require the pool name instead of defaulting it"
        )
        # A DSN-shaped name that shares no substring with the configured one.
        other = f"postgresql://unalike-{short_name(entry)}:irrelevant@elsewhere:5432/other"
        record = logging.LogRecord(
            "psycopg.pool", logging.WARNING, __file__, 1, "pool %s failed", (other,), None
        )
        build(entry).filter(record)
        assert record.getMessage() == f"pool {other} failed", (
            f"{entry.module} redacted a pool name it was not configured with"
        )
    else:
        assert "__init__" not in cls.__dict__, (
            f"{entry.module} is tabled as hardcoding {entry.hardcoded_pool!r}"
        )
        # The inherited Filter name is not what drives matching, so passing one changes
        # nothing: the literal in the table is the only pool this copy redacts.
        record = logging.LogRecord(
            "psycopg.pool", logging.WARNING, __file__, 1, "pool %s failed", ("other-pool",), None
        )
        assert cls("other-pool").filter(record) is True
        assert record.getMessage() == "pool other-pool failed", (
            f"{entry.module} matched a name that a caller passed to the inherited constructor"
        )


def test_table_covers_every_diagnostics_declaration_in_the_backend() -> None:
    discovered = discover_diagnostic_modules()
    tabled = {entry.module for entry in IMPLEMENTATIONS}

    assert len(IMPLEMENTATIONS) == 17
    assert discovered == tabled, (
        "A backend _PoolDiagnostics declaration is not classified here.\n"
        f"untested: {sorted(discovered - tabled)}\n"
        f"stale: {sorted(tabled - discovered)}"
    )
