"""host_nodes 容量更新:两条语句都是字面量,列集合与 `DIMENSIONS` 一致."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import lease_release_sql, leases_sql
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS

_EXPECTED = tuple(
    f"{prefix}_{dimension}" for prefix in ("held", "quarantined") for dimension in DIMENSIONS
)


def _assigned(statement: str) -> tuple[str, ...]:
    body = statement.split("SET ", 1)[1].split(" WHERE ", 1)[0]
    return tuple(
        assignment.split(" = ", 1)[0].strip()
        for assignment in body.split(",")
        if not assignment.strip().startswith(("version", "node_state"))
    )


def test_both_updates_assign_every_capacity_column_in_the_same_order() -> None:
    leases = _assigned(leases_sql._UPDATE_HOST_NODE)
    release = _assigned(lease_release_sql._UPDATE_HOST_NODE)
    assert leases == _EXPECTED
    assert release == _EXPECTED


def test_both_updates_are_literal_cas_writes() -> None:
    for statement in (leases_sql._UPDATE_HOST_NODE, lease_release_sql._UPDATE_HOST_NODE):
        assert statement.startswith("UPDATE host_nodes SET ")
        assert "{" not in statement and "%s" not in statement
        assert statement.endswith("WHERE host_node_id = ? AND version = ?")
