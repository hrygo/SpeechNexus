"""The catalogue reader identity is explicit, API-only and verified at startup."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import SecretStr

from speechnexus_backend.model_catalog_runtime import ModelCatalogRuntime
from tests.unit.backend.test_health import _settings


def _runtime() -> ModelCatalogRuntime:
    return ModelCatalogRuntime(
        _settings().model_copy(
            update={
                "model_catalog_reader_database_url": SecretStr(
                    "postgresql://reader@127.0.0.1/unused"
                )
            }
        )
    )


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.current: Any = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.connection.queries.append((query, parameters))
        if "SELECT SESSION_USER" in query:
            self.current = self.connection.identities
        elif "WITH RECURSIVE reachable" in query:
            self.current = [{"rolname": role} for role in self.connection.roles]
        elif "p.prosecdef" in query:
            role = str(parameters[0])
            self.current = (
                {"signature": "synthetic_definer()"}
                if role in self.connection.definer_roles
                else None
            )
        elif "can_select" in query:
            self.current = self.connection.tables_by_role.get(
                str(parameters[0]), self.connection.default_tables
            )
        elif "aclexplode" in query:
            self.current = [
                {"grantee": grantee, "privilege_type": "TEMPORARY"}
                for grantee in self.connection.temp_roles
            ]
        elif "relkind='S'" in query:
            self.current = [
                {"schema_name": "speech", "relname": relation}
                for relation in self.connection.sequence_roles.get(str(parameters[0]), ())
            ]
        elif "AS forbidden" in query:
            self.current = {"forbidden": str(parameters[-1]) in self.connection.forbidden_roles}
        else:
            raise AssertionError(f"Unexpected audit query: {query}")

    def fetchone(self) -> Any:
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self) -> list[dict[str, Any]]:
        if not isinstance(self.current, list):
            raise AssertionError("Expected multiple audit rows")
        return self.current


class FakeConnection:
    def __init__(
        self,
        *,
        roles: tuple[str, ...] = ("catalogue_reader",),
        forbidden_roles: tuple[str, ...] = (),
        database_create_roles: tuple[str, ...] = (),
        temp_roles: tuple[str, ...] = (),
        sequence_roles: dict[str, tuple[str, ...]] | None = None,
        definer_roles: tuple[str, ...] = (),
        tables_by_role: dict[str, list[dict[str, Any]]] | None = None,
        default_tables: list[dict[str, Any]] | None = None,
    ) -> None:
        self.identities = {"login": "catalogue_reader", "effective": "catalogue_reader"}
        self.roles = roles
        self.forbidden_roles = set(forbidden_roles) | set(database_create_roles)
        self.temp_roles = temp_roles
        self.sequence_roles = sequence_roles or {}
        self.definer_roles = definer_roles
        self.tables_by_role = tables_by_role or {}
        self.default_tables = (
            default_tables
            if default_tables is not None
            else [
                {
                    "schema_name": "speech",
                    "relname": "speech_publications",
                    "can_select": True,
                    "can_select_column": True,
                    "can_write": False,
                    "can_write_column": False,
                },
                {
                    "schema_name": "speech",
                    "relname": "speech_published_binding_revisions",
                    "can_select": True,
                    "can_select_column": True,
                    "can_write": False,
                    "can_write_column": False,
                },
            ]
        )
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> FakeCursor:
        cursor = FakeCursor(self)
        cursor.execute(query, parameters)
        return cursor


def _verify(connection: FakeConnection) -> None:
    _runtime()._verify_read_only(cast(Any, connection))


def test_requires_an_explicit_reader_identity() -> None:
    with pytest.raises(ValueError):
        ModelCatalogRuntime(_settings())


def test_requires_the_api_process_role() -> None:
    settings = _settings().model_copy(
        update={
            "model_catalog_reader_database_url": SecretStr("postgresql://unused@127.0.0.1/unused"),
            "process_role": "worker",
        }
    )
    with pytest.raises(ValueError):
        ModelCatalogRuntime(settings)


def test_unreachable_identity_is_rejected_and_closes() -> None:
    settings = _settings().model_copy(
        update={
            "model_catalog_reader_database_url": SecretStr(
                "postgresql://unused@127.0.0.1:1/unused?connect_timeout=1"
            )
        }
    )
    runtime = ModelCatalogRuntime(settings)
    with pytest.raises(ValueError):
        runtime.open()

    assert runtime.catalogue is None
    runtime.close()


def test_role_audit_checks_the_published_surface_and_privilege_classes() -> None:
    connection = FakeConnection()

    _verify(connection)

    queries = "\n".join(query for query, _ in connection.queries)
    assert "pg_auth_members" in queries
    assert "has_database_privilege" in queries
    assert "aclexplode" in queries
    assert "relkind='S'" in queries
    assert "p.prosecdef" in queries
    assert "SELECT n.nspname AS schema_name" in queries


@pytest.mark.parametrize(
    "predefined",
    [
        "pg_read_all_data",
        "pg_read_server_files",
        "pg_write_server_files",
        "pg_execute_server_program",
        "pg_signal_backend",
        "pg_monitor",
        "pg_read_all_settings",
        "pg_read_all_stats",
    ],
)
def test_predefined_role_membership_is_rejected(predefined: str) -> None:
    with pytest.raises(ValueError, match="predefined PostgreSQL role"):
        _verify(FakeConnection(roles=("catalogue_reader", predefined)))


def test_database_create_privilege_is_rejected() -> None:
    with pytest.raises(ValueError):
        _verify(FakeConnection(database_create_roles=("catalogue_reader",)))


def test_direct_database_temp_grant_is_rejected() -> None:
    with pytest.raises(ValueError, match="TEMP"):
        _verify(FakeConnection(temp_roles=("catalogue_reader",)))


def test_sequence_privilege_is_rejected() -> None:
    with pytest.raises(ValueError, match="sequence"):
        _verify(FakeConnection(sequence_roles={"catalogue_reader": ("catalogue_probe_seq",)}))


def test_every_switchable_role_is_audited() -> None:
    switchable_write = [
        {
            "schema_name": "speech",
            "relname": "speech_publications",
            "can_select": True,
            "can_select_column": True,
            "can_write": True,
            "can_write_column": False,
        }
    ]
    with pytest.raises(ValueError):
        _verify(
            FakeConnection(
                roles=("catalogue_reader", "switchable_owner"),
                tables_by_role={"switchable_owner": switchable_write},
            )
        )


def test_security_definer_execution_surface_is_rejected() -> None:
    with pytest.raises(ValueError):
        _verify(FakeConnection(definer_roles=("catalogue_reader",)))


def test_reader_must_have_select_on_exactly_the_published_surface() -> None:
    with pytest.raises(ValueError):
        _verify(FakeConnection(default_tables=[]))
    with pytest.raises(ValueError):
        _verify(
            FakeConnection(
                default_tables=[
                    {
                        "schema_name": "speech",
                        "relname": "speech_publications",
                        "can_select": True,
                        "can_select_column": True,
                        "can_write": False,
                        "can_write_column": False,
                    },
                    {
                        "schema_name": "speech",
                        "relname": "speech_binding_revisions",
                        "can_select": True,
                        "can_select_column": True,
                        "can_write": False,
                        "can_write_column": False,
                    },
                ]
            )
        )
    with pytest.raises(ValueError):
        _verify(
            FakeConnection(
                default_tables=[
                    {
                        "schema_name": "speech",
                        "relname": "speech_publications",
                        "can_select": True,
                        "can_select_column": True,
                        "can_write": False,
                        "can_write_column": False,
                    },
                    {
                        "schema_name": "speech",
                        "relname": "speech_published_binding_revisions",
                        "can_select": True,
                        "can_select_column": True,
                        "can_write": False,
                        "can_write_column": False,
                    },
                    {
                        "schema_name": "speech",
                        "relname": "speech_private",
                        "can_select": True,
                        "can_select_column": True,
                        "can_write": False,
                        "can_write_column": False,
                    },
                ]
            )
        )


def test_reader_cannot_select_in_another_user_schema() -> None:
    foreign_read = {
        "schema_name": "public",
        "relname": "unrelated_private",
        "can_select": True,
        "can_select_column": True,
        "can_write": False,
        "can_write_column": False,
    }
    with pytest.raises(ValueError):
        _verify(FakeConnection(default_tables=[foreign_read]))


def test_column_level_write_privilege_is_rejected() -> None:
    writable = [
        {
            "schema_name": "speech",
            "relname": "speech_publications",
            "can_select": True,
            "can_select_column": True,
            "can_write": False,
            "can_write_column": True,
        },
        {
            "schema_name": "speech",
            "relname": "speech_published_binding_revisions",
            "can_select": True,
            "can_select_column": True,
            "can_write": False,
            "can_write_column": False,
        },
    ]
    with pytest.raises(ValueError):
        _verify(FakeConnection(default_tables=writable))


def test_column_level_read_privilege_is_rejected() -> None:
    column_read = [
        {
            "schema_name": "speech",
            "relname": "speech_binding_revisions",
            "can_select": False,
            "can_select_column": True,
            "can_write": False,
            "can_write_column": False,
        },
        {
            "schema_name": "speech",
            "relname": "speech_publications",
            "can_select": True,
            "can_select_column": True,
            "can_write": False,
            "can_write_column": False,
        },
        {
            "schema_name": "speech",
            "relname": "speech_published_binding_revisions",
            "can_select": True,
            "can_select_column": True,
            "can_write": False,
            "can_write_column": False,
        },
    ]
    with pytest.raises(ValueError, match="reads beyond"):
        _verify(FakeConnection(default_tables=column_read))
