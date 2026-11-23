"""artifact 读回投影与 runtime 预留容量更新:两条语句都是字面量."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import artifacts_postgres, runtime_sql
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS


def _projection(statement: str) -> tuple[str, ...]:
    body = statement.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
    return tuple(column.strip() for column in body.split(","))


def test_the_artifact_read_projects_every_read_column_in_order() -> None:
    statement = artifacts_postgres._SELECT_ARTIFACT
    assert _projection(statement) == tuple(
        column.strip() for column in artifacts_postgres._READ_COLUMNS.split(",")
    )
    assert statement.endswith("WHERE artifact_id = ?")


def test_the_runtime_host_update_reserves_every_dimension_and_keeps_the_cas() -> None:
    statement = runtime_sql._UPDATE_HOST_NODE
    body = statement.split("SET ", 1)[1].split(" WHERE ", 1)[0]
    assigned = [
        assignment.split(" = ", 1)[0].strip()
        for assignment in body.split(",")
        if "version" not in assignment
    ]
    assert assigned == [f"held_{dimension}" for dimension in DIMENSIONS]
    assert statement.endswith("WHERE host_node_id = ? AND version = ?")
    assert statement.count("?") == len(DIMENSIONS) + 2


def test_neither_statement_is_built_at_call_time() -> None:
    for statement in (artifacts_postgres._SELECT_ARTIFACT, runtime_sql._UPDATE_HOST_NODE):
        assert "{" not in statement and "%s" not in statement
