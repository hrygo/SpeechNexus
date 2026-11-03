"""Default composition owns a separate query pool and refuses unsafe reader authority."""

from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import SecretStr

from speechnexus_gateway.app import _build_control, _close_control
from speechnexus_gateway.infrastructure.execution_reader import PostgresExecutionReader
from speechnexus_gateway.infrastructure.reader_authority import ExecutionReaderTarget
from speechnexus_gateway.settings import Settings
from tests.unit.gateway.test_barrier_http import settings_for


class Pool:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name, self.events = name, events

    def open(self) -> None:
        self.events.append(self.name + ":open")

    def close(self) -> None:
        self.events.append(self.name + ":close")


def test_default_control_requires_explicit_reader_before_opening_ownership(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="independent query database"):
        _build_control(settings_for(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("authorized", [True, False])
@pytest.mark.parametrize("target_matches", [True, False])
def test_default_control_routes_queries_to_separate_verified_pool_and_closes_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authorized: bool, target_matches: bool
) -> None:
    events: list[str] = []
    pools: list[Pool] = []
    reader_dsn = "postgresql://query-only@127.0.0.1/unused"
    settings = settings_for(tmp_path, control_query_database_url=SecretStr(reader_dsn))

    def build_pool(dsn: str, **kwargs: Any) -> Pool:
        assert kwargs["schema"] == settings.control_schema
        pool = Pool("reader" if dsn == reader_dsn else "writer", events)
        pools.append(pool)
        return pool

    def verify(pool: Pool, schema: str) -> bool:
        assert pool.name == "reader" and schema == settings.control_schema
        events.append("reader:verify")
        return authorized

    target = ExecutionReaderTarget("123456789", "speech", 1, "speech", 2, UUID(int=1))

    def verify_target(writer: Pool, reader: Pool, schema: str) -> ExecutionReaderTarget | None:
        assert (writer.name, reader.name, schema) == ("writer", "reader", settings.control_schema)
        events.append("target:verify")
        return target if target_matches else None

    monkeypatch.setattr("speechnexus_gateway.app.gateway_pool", build_pool)
    monkeypatch.setattr("speechnexus_gateway.app.verify_abort_authority", lambda *args: True)
    monkeypatch.setattr("speechnexus_gateway.app.verify_execution_reader_authority", verify)
    monkeypatch.setattr("speechnexus_gateway.app.verify_execution_reader_target", verify_target)
    if authorized and target_matches:
        _, query, _, ownership, resources, worker, _, ledger = _build_control(settings)
        assert cast(object, ledger._pool) is pools[0]
        assert isinstance(query._ledger, PostgresExecutionReader)
        assert cast(object, query._ledger._pool) is pools[1]
        assert query._ledger._expected_target == target
        _close_control(worker, resources, ownership)
    else:
        with pytest.raises(ValueError, match=r"query reader (authority|target)") as caught:
            _build_control(settings)
        assert reader_dsn not in str(caught.value)
    assert events == ["writer:open", "reader:open", "reader:verify"] + (
        ["target:verify"] if authorized else []
    ) + ["reader:close", "writer:close"]


def test_query_credential_is_hidden_from_settings_repr(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        control_query_database_url=SecretStr("postgresql://reader:synthetic-secret@localhost/db"),
    )
    assert "synthetic-secret" not in repr(settings)
    assert isinstance(settings, Settings)
