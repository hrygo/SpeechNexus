"""Drift guards for the declared release surface and the storage refusal mapping."""

from __future__ import annotations

import psycopg
import pytest

from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.quarantine_release_postgres import (
    _surface_defect,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    UPDATE_COLUMNS,
)


def test_the_host_node_update_surface_is_derived_from_the_declared_dimensions() -> None:
    """A new dimension must widen the release grant surface automatically.

    ``host_nodes`` carries one ``held_*``/``quarantined_*`` pair per
    ``DIMENSIONS`` entry, and ``release_in_transaction`` updates every one of
    them. A hand-written list drifts silently: the transaction writes the new
    column, the release role does not hold it, and the failure surfaces only at
    request time while the startup equality still passes because the role really
    does not hold the column. Deriving the declaration from ``DIMENSIONS`` makes
    the missing grant fail startup instead.
    """
    expected = {
        f"{prefix}_{dimension}" for prefix in ("held", "quarantined") for dimension in DIMENSIONS
    } | {"version"}
    assert UPDATE_COLUMNS["host_nodes"] == frozenset(expected)


@pytest.mark.parametrize(
    "error,code",
    [
        (psycopg.errors.InsufficientPrivilege(), "quarantine_release_privilege_missing"),
        (psycopg.errors.UndefinedTable(), "quarantine_release_schema_not_migrated"),
        (psycopg.errors.UndefinedColumn(), "quarantine_release_schema_not_migrated"),
        (psycopg.errors.UndefinedObject(), "quarantine_release_schema_not_migrated"),
        (psycopg.errors.UndefinedFunction(), "quarantine_release_schema_not_migrated"),
        (psycopg.errors.InvalidSchemaName(), "quarantine_release_schema_not_migrated"),
        (psycopg.errors.CheckViolation(), "quarantine_release_failed"),
        (ValueError("incoherent ledger row"), "quarantine_release_failed"),
    ],
)
def test_a_storage_refusal_names_the_repair_action(error: BaseException, code: str) -> None:
    """Every non-business refusal maps to a code whose repair is not "new evidence".

    The mapping lives in the adapter so a database diagnosis never reaches the
    response body; this table pins the three repair actions an operator can take:
    re-grant, migrate, or treat it as an unexpected defect.
    """
    assert _surface_defect(error) == code


def test_no_storage_refusal_is_reported_as_a_business_conflict() -> None:
    """'conflict' must stay reachable only from a real state mismatch."""
    for error in (
        psycopg.errors.InsufficientPrivilege(),
        psycopg.errors.UndefinedTable(),
        psycopg.errors.CheckViolation(),
        ValueError("incoherent ledger row"),
    ):
        assert _surface_defect(error) != "quarantine_release_conflict"
