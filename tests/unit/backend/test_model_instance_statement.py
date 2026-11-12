"""The instance read composes exactly one total-order keyset statement.

The statement is the whole query surface: the instance row, the binding revision
it names, the strict key frontier, the optional exact filters and one ordering
over both key columns. Nothing here selects a mutable column as an ordering term
or a fact the store does not record.
"""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.speech.domain.model_instances import (
    InstancePosition,
    ModelInstanceQuery,
    ModelInstanceUnavailableError,
)
from speechnexus_backend.speech.infrastructure.model_instances_postgres import (
    _FIND,
    statement,
)

BASE = (
    "SELECT instance.instance_id, instance.instance_generation, instance.host_node_id, "
    "instance.pool_id, instance.gateway_id, instance.binding_revision_id, "
    "revision.binding_id, revision.revision, instance.config_digest, "
    "instance.instance_state, instance.ready_at_us, instance.stopped_at_us, "
    "instance.quarantine_reason FROM engine_instances AS instance "
    "LEFT JOIN speech_binding_revisions AS revision "
    "ON revision.revision_id = instance.binding_revision_id"
)
ORDER = " ORDER BY instance.instance_id, instance.instance_generation LIMIT %s"


def test_an_unfiltered_page_starts_before_every_key_and_over_reads_one_row() -> None:
    text, parameters = statement(ModelInstanceQuery(50))
    assert text == BASE + (
        " WHERE (instance.instance_id, instance.instance_generation) > (%s, %s)" + ORDER
    )
    assert parameters == ("", 0, 51)


def test_the_frontier_and_both_filters_are_bound_in_order() -> None:
    query = ModelInstanceQuery(2, InstancePosition("inst-a", 3), "node-a", "ready")
    text, parameters = statement(query)
    assert text == BASE + (
        " WHERE (instance.instance_id, instance.instance_generation) > (%s, %s)"
        " AND instance.host_node_id = %s AND instance.instance_state = %s" + ORDER
    )
    assert parameters == ("inst-a", 3, "node-a", "ready", 3)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (ModelInstanceQuery(1, host_node_id="node-a"), ("", 0, "node-a", 2)),
        (ModelInstanceQuery(1, state="stopped"), ("", 0, "stopped", 2)),
        (
            ModelInstanceQuery(1, InstancePosition("inst-a", 2), state="quarantined"),
            ("inst-a", 2, "quarantined", 2),
        ),
    ],
)
def test_each_filter_combination_keeps_one_statement(
    query: ModelInstanceQuery, expected: tuple[Any, ...]
) -> None:
    text, parameters = statement(query)
    assert text.count(" WHERE ") == 1
    assert text.endswith(ORDER)
    assert parameters == expected


def test_the_ordering_names_both_key_columns_and_no_mutable_column() -> None:
    text, _ = statement(ModelInstanceQuery(10))
    assert " ORDER BY instance.instance_id, instance.instance_generation LIMIT %s" in text
    for mutable in ("ready_at_us", "stopped_at_us", "instance_state", "host_node_id"):
        assert f"ORDER BY instance.{mutable}" not in text
    # Selected once, compared once in the frontier, ordered once.
    assert text.count("instance.instance_generation") == 3


def test_the_single_lookup_reports_presence_and_the_requested_generation_in_one_statement() -> None:
    assert _FIND.startswith(
        "WITH presence AS (SELECT EXISTS (SELECT 1 FROM engine_instances AS listed "
        "WHERE listed.instance_id = %s) AS instance_exists) "
    )
    assert (
        "LEFT JOIN engine_instances AS instance ON instance.instance_id = %s "
        "AND instance.instance_generation = %s" in _FIND
    )
    # The one-row presence CTE answers "does the id exist" and "does this
    # generation exist" together, so an absent generation still reports presence.
    assert _FIND.count("FROM engine_instances") == 1
    assert _FIND.count("LEFT JOIN") == 2


def test_a_value_outside_the_query_contract_composes_nothing() -> None:
    with pytest.raises(ModelInstanceUnavailableError):
        statement(object())  # type: ignore[arg-type]
