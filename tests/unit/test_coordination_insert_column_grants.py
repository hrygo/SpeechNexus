"""The coordination INSERT surfaces have one definition point (#1181).

``job_writer_columns.COORDINATION_JOB_INSERT_COLUMNS`` is the only place that
lists which columns a Job writer may insert. Three consumers used to restate it
(the writer's own GRANT, the admission role's GRANT, the coordination runtime
role's GRANT) and the preflight kept a fourth copy. A missed copy did not fail to
compile: the column-level GRANT stayed one column short and the first real create
failed at runtime with ``42501``, which PostgreSQL reports against the table, not
the column.

These assertions check both directions. The grants must name exactly the declared
columns, in the declared order, in the statement text provisioning applies (a set
comparison would pass while the GRANT still omitted the column). And the
declaration itself must cover every column the production writers state in their
own INSERT SQL: without that anchor the declaration only confirms itself, and a
column dropped from it would still hand a full install a GRANT that fails on the
first real create.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from speechnexus_backend.speech.infrastructure.coordination_grants import (
    INSERT_COLUMNS as RUNTIME_INSERT_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import job_runtime_grants
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    COORDINATION_JOB_INSERT_COLUMNS,
    COORDINATION_OUTBOX_INSERT_COLUMNS,
    JOB_WRITER_INSERT_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    _INSERT_COLUMNS as ADMISSION_INSERT_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    public_job_admission_grants,
)

ROOT = Path(__file__).resolve().parents[2]
INSERT = re.compile(r'^GRANT INSERT \(([^)]*)\) ON "[^"]+"\."?([A-Za-z0-9_]+)"? TO ')

#: The two production writers that create a coordination Job. Their SQL, not the
#: declaration, is the independent anchor: a column the writer inserts and the
#: declaration forgets is exactly the runtime ``42501`` this file exists to stop.
_WRITER_MODULES = (
    "backend/src/speechnexus_backend/speech/infrastructure/jobs_sql.py",
    "backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py",
)


def writer_insert_columns(relative: str, table: str) -> list[tuple[str, ...]]:
    """Every INSERT column list ``relative`` states for ``table``, read from its SQL.

    Parsed from the strings the writers execute rather than from the declaration,
    so the two can be compared instead of one confirming itself. Adjacent string
    literals fold at parse time, so a statement split over lines arrives whole.
    """

    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    pattern = re.compile(rf"INSERT INTO {table}\s*\(([^)]*)\)")
    found: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in pattern.finditer(node.value):
                found.append(
                    tuple(part.strip() for part in match.group(1).split(",") if part.strip())
                )
    return found


def insert_columns(statements: tuple[str, ...], table: str) -> list[list[str]]:
    """The column list of every INSERT grant on ``table``, split and in order."""

    found: list[list[str]] = []
    for statement in statements:
        match = INSERT.match(statement.strip())
        if match is not None and match.group(2) == table:
            found.append(match.group(1).split(", "))
    return found


def all_insert_statements(schema: str = "speech") -> dict[str, tuple[str, ...]]:
    """Every grant function that used to carry its own copy of the declaration."""

    return {
        "job writer": job_runtime_grants(schema, "sn_jobs_writer"),
        "admission": public_job_admission_grants(schema, "sn_jobs_admission"),
        "coordination runtime": coordination_runtime_grants(
            schema, speech_role="sn_api", gateway_role="sn_gateway"
        ),
    }


@pytest.mark.parametrize(
    ("table", "declared"),
    [
        pytest.param("coordination_jobs", COORDINATION_JOB_INSERT_COLUMNS, id="coordination_jobs"),
        pytest.param(
            "coordination_outbox", COORDINATION_OUTBOX_INSERT_COLUMNS, id="coordination_outbox"
        ),
    ],
)
def test_the_declaration_covers_the_columns_the_writers_insert(
    table: str, declared: tuple[str, ...]
) -> None:
    """Coverage, not order: an INSERT may name its columns in any order.

    The grant text itself is checked for order by the tests below.
    """

    for relative in _WRITER_MODULES:
        statements = writer_insert_columns(relative, table)
        assert statements, f"{relative} no longer states an INSERT into {table}"
        for columns in statements:
            assert set(columns) == set(declared), (
                f"{relative} inserts {sorted(columns)} into {table}, but "
                f"{table} declares {sorted(declared)}; the GRANT is derived from the "
                "declaration, so a column only one side knows about fails the first "
                "real create with 42501 (PostgreSQL names the table, not the column)"
            )


@pytest.mark.parametrize("emitter", ["job writer", "admission", "coordination runtime"])
def test_every_coordination_jobs_grant_lists_the_declaration_in_order(emitter: str) -> None:
    columns = insert_columns(all_insert_statements()[emitter], "coordination_jobs")
    assert columns == [list(COORDINATION_JOB_INSERT_COLUMNS)], (
        f"the {emitter} GRANT no longer names exactly COORDINATION_JOB_INSERT_COLUMNS; "
        "a missing or reordered column is a runtime 42501 on the first create"
    )


def test_the_job_writer_grant_text_is_the_declared_statement() -> None:
    # The whole statement, not just the column list: provisioning applies these
    # strings verbatim, so a formatting change is a different grant document.
    declared = ", ".join(COORDINATION_JOB_INSERT_COLUMNS)
    statements = job_runtime_grants("speech", "sn_jobs_writer")
    assert (
        f'GRANT INSERT ({declared}) ON "speech".coordination_jobs TO "sn_jobs_writer"' in statements
    )
    assert (
        f"GRANT INSERT ({', '.join(COORDINATION_OUTBOX_INSERT_COLUMNS)}) "
        'ON "speech".coordination_outbox TO "sn_jobs_writer"' in statements
    )


def test_the_admission_grant_text_is_the_declared_statement() -> None:
    declared = ", ".join(COORDINATION_JOB_INSERT_COLUMNS)
    statements = public_job_admission_grants("speech", "sn_jobs_admission")
    assert (
        f'GRANT INSERT ({declared}) ON "speech".coordination_jobs TO "sn_jobs_admission"'
        in statements
    )
    assert (
        f"GRANT INSERT ({', '.join(COORDINATION_OUTBOX_INSERT_COLUMNS)}) "
        'ON "speech".coordination_outbox TO "sn_jobs_admission"' in statements
    )


def test_the_coordination_runtime_grant_extends_the_writer_outbox_surface() -> None:
    """The runtime role appends runtime-intent events, so its outbox surface is wider.

    Wider, never reordered or narrowed: the Job columns come first, in the
    declaration's order, followed by the three columns ``runtime_intent_schema``
    adds for the ``event_kind='runtime'`` shape.
    """

    statements = coordination_runtime_grants(
        "speech", speech_role="sn_api", gateway_role="sn_gateway"
    )
    outbox = insert_columns(statements, "coordination_outbox")
    assert len(outbox) == 1
    assert outbox[0][: len(COORDINATION_OUTBOX_INSERT_COLUMNS)] == list(
        COORDINATION_OUTBOX_INSERT_COLUMNS
    )
    assert outbox[0][len(COORDINATION_OUTBOX_INSERT_COLUMNS) :] == [
        "event_kind",
        "target_node_id",
        "intent_id",
    ]


def test_the_grant_modules_hold_the_declaration_itself() -> None:
    # Identity, not equality: a pasted-back literal with the same content today is
    # a second definition point again, and it silently diverges at the next column.
    assert RUNTIME_INSERT_COLUMNS["coordination_jobs"] is COORDINATION_JOB_INSERT_COLUMNS
    assert ADMISSION_INSERT_COLUMNS["coordination_jobs"] is COORDINATION_JOB_INSERT_COLUMNS
    assert ADMISSION_INSERT_COLUMNS["coordination_outbox"] is COORDINATION_OUTBOX_INSERT_COLUMNS


@pytest.mark.parametrize(
    "relative",
    [
        "backend/src/speechnexus_backend/speech/infrastructure/coordination_grants.py",
        "backend/src/speechnexus_backend/speech/infrastructure/public_job_admission_grants.py",
    ],
)
def test_the_grant_modules_do_not_restate_the_column_list(relative: str) -> None:
    # The shorthand these modules used to write is space-joined, so a copy that
    # comes back is caught by its text, whatever the surrounding code looks like.
    source = (ROOT / relative).read_text(encoding="utf-8")
    assert " ".join(COORDINATION_JOB_INSERT_COLUMNS) not in source


def preflight() -> Any:
    """The standalone preflight script, loaded the way its own test loads it."""

    spec = importlib.util.spec_from_file_location(
        "check_async_job_prereqs_for_insert_columns",
        ROOT / "scripts/dev-env/check-async-job-prereqs.py",
    )
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_preflight_reads_the_one_declaration() -> None:
    # It runs as a standalone file, so it prepends backend/src to sys.path; the
    # object it gets must be the declaration itself, not an equal copy.
    assert preflight().WRITER_INSERT_COLUMNS is JOB_WRITER_INSERT_COLUMNS
