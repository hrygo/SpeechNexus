"""启动权限探针:身份分支是两条完整语句,两两必须只差身份那一处."""

from __future__ import annotations

from speechnexus_backend import startup_privileges as module

_PAIRS = (
    ("_SCHEMA_USAGE_CURRENT", "_SCHEMA_USAGE_NAMED"),
    ("_SCHEMA_CREATE_CURRENT", "_SCHEMA_CREATE_NAMED"),
    ("_TABLE_PRIVILEGE_CURRENT", "_TABLE_PRIVILEGE_NAMED"),
    ("_COLUMN_PRIVILEGE_CURRENT", "_COLUMN_PRIVILEGE_NAMED"),
    ("_SEQUENCE_STRAY_CURRENT", "_SEQUENCE_STRAY_NAMED"),
    ("_STRAY_TABLE_CURRENT", "_STRAY_TABLE_NAMED"),
)

_IDENTITY = "CURRENT_USER"
_NAMED = "CAST(? AS name)"


def test_each_pair_differs_only_in_the_identity_argument() -> None:
    for current_name, named_name in _PAIRS:
        current = getattr(module, current_name)
        named = getattr(module, named_name)
        assert _IDENTITY in current, current_name
        assert _NAMED in named, named_name
        assert current.replace(_IDENTITY, _NAMED) == named, named_name


def test_no_probe_builds_its_statement_at_call_time() -> None:
    for current_name, named_name in _PAIRS:
        for name in (current_name, named_name):
            statement = getattr(module, name)
            assert "%s" not in statement, name
            assert "{" not in statement, name


def test_the_stray_scan_bounds_itself_by_fetchone_not_by_a_limit() -> None:
    # ``LIMIT 1`` 是可移植子集里的受限写法;调用方本来就用 fetchone 只取一行。
    for current_name, named_name in _PAIRS:
        for name in (current_name, named_name):
            assert "LIMIT" not in getattr(module, name).upper(), name


def test_the_declared_names_travel_as_one_list_marker() -> None:
    for name in ("_SEQUENCE_STRAY_CURRENT", "_SEQUENCE_STRAY_NAMED", "_STRAY_TABLE_CURRENT"):
        assert "NOT IN (?)" in getattr(module, name), name
        assert "ALL(" not in getattr(module, name), name


def test_the_role_interpolation_helper_is_gone() -> None:
    assert not hasattr(module, "_role_argument")
    assert not hasattr(module, "_TABLE_SURFACE")
