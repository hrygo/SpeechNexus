"""engine_instances 插入:33 列写死,列序与维度顺序必须一致."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import runtime_sql
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS

_BASE = (
    "instance_id",
    "instance_generation",
    "host_node_id",
    "pool_id",
    "gateway_id",
    "binding_revision_id",
    "manifest_id",
    "approved_template_id",
    "management_mode",
    "loading_policy",
    "config_digest",
    "instance_state",
    "max_slots",
    "realtime_reserved_slots",
    "last_fencing_token",
)


def _columns(statement: str) -> tuple[str, ...]:
    body = statement.split("(", 1)[1].split(")", 1)[0]
    return tuple(column.strip() for column in body.split(","))


def test_the_insert_names_the_base_then_each_vector_in_dimension_order() -> None:
    expected = list(_BASE)
    for prefix in ("peak_", "steady_", "per_slot_"):
        expected.extend(prefix + dimension for dimension in DIMENSIONS)
    assert _columns(runtime_sql._INSERT_ENGINE_INSTANCE) == tuple(expected)


def test_one_placeholder_per_column_and_no_legacy_marker() -> None:
    statement = runtime_sql._INSERT_ENGINE_INSTANCE
    assert statement.count("?") == len(_columns(statement)) == 33
    assert "%s" not in statement
    assert statement.endswith(")")


def test_the_insert_does_not_use_a_vendor_upsert() -> None:
    assert "ON CONFLICT" not in runtime_sql._INSERT_ENGINE_INSTANCE.upper()
