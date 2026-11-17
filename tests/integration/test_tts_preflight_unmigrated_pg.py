"""The settled-capacity preflight names a missing migration on real PostgreSQL.

#877 item 3 was recorded on a real deployment: with the coordinated schema absent,
the API process died on an uncaught driver exception that named one table
(``relation "admission_leases" does not exist``) and no repair. The preflight now
refuses with its own type and the migration command, and
``tests/unit/backend/test_lease_settlement.py`` pins that mapping against a stub
connection that raises ``UndefinedTable`` directly.

A stub proves the branch, not that a real server's refusal lands in it: the class
of the exception, its SQLSTATE and whether the pool re-raises it unchanged are all
driver facts. This module re-runs the same refusal against an isolated schema that
never ran the migration profile, so a change that stops catching what PostgreSQL
actually raises fails here instead of in a container restart loop.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from speechnexus_backend.speech.domain.job_prerequisites import MIGRATION_COMMAND
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
    SettlementPreflightUnavailableError,
    require_complete_tts_roots,
)

pytestmark = pytest.mark.integration
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"


@pytest.fixture
def unmigrated_schema() -> Iterator[tuple[str, str]]:
    """One schema that never ran the migration profile, dropped afterwards."""
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    schema = "sn_tts_preflight_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(url, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        yield url, schema
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _pool(url: str, schema: str) -> Any:
    pool = speech_pool(
        url, schema=schema, max_size=2, name="sn-tts-preflight-" + uuid.uuid4().hex[:8]
    )
    pool.open()
    return pool


def test_an_unmigrated_database_names_the_migration_instead_of_the_driver(
    unmigrated_schema: tuple[str, str],
) -> None:
    """The refusal is the preflight's own type, carries the repair, and stays bounded."""
    url, schema = unmigrated_schema
    pool = _pool(url, schema)
    try:
        with pytest.raises(SettlementPreflightUnavailableError) as raised:
            require_complete_tts_roots(pool, timeout_seconds=3)
    finally:
        pool.close()

    message = str(raised.value)
    # The one repair, shared with every other surface that names this condition.
    assert MIGRATION_COMMAND in message
    assert "migration profile" in message
    # The driver's exception did not escape: an uncaught UndefinedTable is what made
    # the API container restart on a traceback with no instruction.
    assert not isinstance(raised.value, psycopg.Error)
    # The relation name survives only as bounded context; the driver's whole sentence
    # (server, port, database, role) is not what an operator acts on.
    assert "admission_leases" in message
    assert len(message) < 400
