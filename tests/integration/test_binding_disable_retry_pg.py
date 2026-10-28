"""Recover a real committed abort intent whose acknowledgement was lost."""

from typing import Any, cast

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_binding_disable_intent_pg import _command
from tests.integration.test_binding_lifecycle_pg import Sandbox, _seed
from tests.integration.test_binding_lifecycle_pg import sandbox as sandbox
from tests.integration.test_coordination_foundation import seed
from tests.integration.test_production_jobs import JobDatabase

from speechnexus_backend.speech.application.binding_disable_coordinator import (
    BindingDisableCoordinator,
)
from speechnexus_backend.speech.infrastructure.binding_disable_coordinator_postgres import (
    PostgresDisableCoordinatorStore,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_coordinator_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_lost_abort_intent_commit_response_queries_original_before_send(
    sandbox: Sandbox,
) -> None:
    _seed(sandbox)

    class SeedDatabase:
        def query(self, sql: str, params: tuple[object, ...] = ()) -> Any:
            return sandbox.admin.execute(sql, params)

    seed(cast(JobDatabase, SeedDatabase()))
    sandbox.admin.execute("UPDATE admission_attempts SET binding_revision_id='binding-rev-2'")
    sandbox.lifecycle.retire(_command())
    role = sandbox.schema + "_retry"
    sandbox.admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_retry_test'")
    pool = speech_pool(
        make_conninfo(sandbox.url, user=role, password="synthetic_retry_test"),
        schema=sandbox.schema,
    )

    class Store(PostgresDisableCoordinatorStore):
        lose_reply = True

        def begin_abort(self, event_id: str, submit_intent_id: str) -> bool:
            first = super().begin_abort(event_id, submit_intent_id)
            if self.lose_reply:
                self.lose_reply = False
                raise psycopg.OperationalError("synthetic lost commit reply")
            return first

    class Gateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def abort_idempotent(self, target: Any) -> object:
            self.calls.append(("abort", target.submit_intent_id))
            return object()

        async def query_disable_target(self, target: Any) -> object:
            self.calls.append(("query", target.submit_intent_id))
            return object()

    try:
        for statement in disable_coordinator_grants(sandbox.schema, role):
            sandbox.admin.execute(statement)
        pool.open()
        gateway = Gateway()
        coordinator = BindingDisableCoordinator(
            Store(pool, timeout_seconds=3),
            gateway,
            gateway_id="gateway-test",
            node_id="host-test",
            batch_size=5,
            wait_timeout_ms=1000,
        )
        first = await coordinator.run_pass()
        assert first.unavailable == 1
        assert gateway.calls == []
        assert sandbox.admin.execute(
            "SELECT count(*) FROM speech_binding_disable_abort_intents"
        ).fetchone() == (1,)
        restarted = BindingDisableCoordinator(
            PostgresDisableCoordinatorStore(pool, timeout_seconds=3),
            gateway,
            gateway_id="gateway-test",
            node_id="host-test",
            batch_size=5,
            wait_timeout_ms=1000,
        )
        result = await restarted.run_pass()
        assert (result.queried, result.attempted) == (1, 1)
        assert gateway.calls == [("query", "submit-test"), ("abort", "submit-test")]
        assert sandbox.admin.execute(
            "SELECT count(*) FROM speech_binding_disable_abort_intents"
        ).fetchone() == (1,)
    finally:
        pool.close()
        sandbox.admin.execute(f'DROP OWNED BY "{role}"')
        sandbox.admin.execute(f'DROP ROLE "{role}"')
