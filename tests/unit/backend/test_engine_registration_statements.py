"""`host_nodes` 的列清单是字面量,必须与 `_DIMENSIONS` 逐项对齐.

列清单之所以不拼,是因为受限方言不允许动态语句,而拼出来的占位符还会混用 `?` 与 `%s`。
代价是字面量可能与 `_DIMENSIONS` 漂移 —— 这条用例就是那笔代价的对价。
"""

from __future__ import annotations

from speechnexus_backend.engine_registration import _DIMENSIONS, HOST_INSERT, POOL_INSERT


def test_host_insert_lists_every_allocatable_dimension_in_order() -> None:
    assert HOST_INSERT.startswith("INSERT INTO host_nodes (")
    listed = HOST_INSERT.split("(", 1)[1].split(")", 1)[0]
    columns = [column.strip() for column in listed.split(",")]
    expected = [
        "host_node_id",
        "memory_architecture",
        "node_state",
        "topology_digest",
        *("allocatable_" + name for name in _DIMENSIONS),
    ]
    assert columns == expected


def test_host_insert_binds_one_marker_per_value() -> None:
    assert HOST_INSERT.count("?") == 3 + len(_DIMENSIONS)
    assert "%s" not in HOST_INSERT


def test_pool_insert_binds_the_two_identity_columns() -> None:
    assert POOL_INSERT.startswith("INSERT INTO resource_pools (")
    assert POOL_INSERT.count("?") == 2
    assert "ON CONFLICT" not in POOL_INSERT
