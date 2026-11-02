"""Activation HTTP and actual-commit recovery on explicitly isolated PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_binding_lifecycle_pg import (
    ACTIVATE_1,
    ACTIVATE_2,
    RETIRE_URL,
    Sandbox,
    _app,
    _seed,
    scalar,
)
from tests.integration.test_binding_lifecycle_pg import sandbox as sandbox
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresBindingLifecycle,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, SpeechPool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def snapshot(sandbox: Sandbox) -> tuple[object, object, object]:
    return (
        sandbox.admin.execute("SELECT * FROM speech_publication_heads").fetchall(),
        sandbox.admin.execute("SELECT * FROM speech_catalog_state").fetchall(),
        sandbox.admin.execute(
            "SELECT * FROM speech_publication_lifecycle_events ORDER BY event_id"
        ).fetchall(),
    )


async def test_rejected_key_is_free_then_committed_result_precedes_cas(sandbox: Sandbox) -> None:
    _seed(sandbox)
    before = snapshot(sandbox)
    headers = {"Idempotency-Key": "synthetic-original-key"}
    async with AsyncClient(
        transport=ASGITransport(app=_app(sandbox)), base_url="http://test"
    ) as http:
        rejected = await http.post(
            ACTIVATE_1, json={"expected_current_revision": "3"}, headers=headers
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "configuration_changed"
        assert snapshot(sandbox) == before
        accepted = await http.post(
            ACTIVATE_1, json={"expected_current_revision": "2"}, headers=headers
        )
        assert accepted.status_code == 200, accepted.text
        assert (
            scalar(sandbox.admin, "SELECT count(*) FROM speech_publication_lifecycle_events") == 1
        )
        conflict = await http.post(
            ACTIVATE_1, json={"expected_current_revision": "3"}, headers=headers
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        changed = await http.post(
            ACTIVATE_2,
            json={"expected_current_revision": "1"},
            headers={"Idempotency-Key": "synthetic-other-key"},
        )
        assert changed.status_code == 200, changed.text
        before_replay = snapshot(sandbox)
        replay = await http.post(
            ACTIVATE_1, json={"expected_current_revision": "2"}, headers=headers
        )
        assert replay.status_code == 200 and replay.json() == accepted.json()
        assert snapshot(sandbox) == before_replay


async def test_original_rejected_request_can_succeed_after_state_changes(sandbox: Sandbox) -> None:
    _seed(sandbox)
    headers = {"Idempotency-Key": "synthetic-rejected-key"}
    async with AsyncClient(
        transport=ASGITransport(app=_app(sandbox)), base_url="http://test"
    ) as http:
        rejected = await http.post(
            ACTIVATE_2, json={"expected_current_revision": "1"}, headers=headers
        )
        assert rejected.status_code == 409, rejected.text
        changed = await http.post(ACTIVATE_1, json={"expected_current_revision": "2"})
        assert changed.status_code == 200, changed.text
        accepted = await http.post(
            ACTIVATE_2, json={"expected_current_revision": "1"}, headers=headers
        )
        assert accepted.status_code == 200, accepted.text
        assert (
            scalar(sandbox.admin, "SELECT count(*) FROM speech_publication_lifecycle_events") == 2
        )


async def test_lost_reply_after_actual_commit_replays_original_without_changing_head(
    sandbox: Sandbox,
) -> None:
    _seed(sandbox)

    class LostReplyPool:
        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[SpeechConnection]:
            with sandbox.pool.connection(timeout=timeout) as connection:
                yield connection
            # The real pool context committed and returned its connection first.
            raise psycopg.OperationalError("synthetic lost reply after actual commit")

    provider = PostgresBindingLifecycle(cast(SpeechPool, LostReplyPool()))
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("test",)}),
        binding_lifecycle=provider,
    )
    headers = {"Idempotency-Key": "synthetic-lost-reply-key"}
    body = {"expected_current_revision": "2"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        unknown = await http.post(ACTIVATE_1, json=body, headers=headers)
        assert unknown.status_code == 503, unknown.text
        assert unknown.json()["error"]["code"] == "binding_result_unknown"
        assert unknown.json()["error"]["retryable"] is False
        assert unknown.headers["x-should-retry"] == "false"
    assert scalar(sandbox.admin, "SELECT count(*) FROM speech_publication_lifecycle_events") == 1
    assert sandbox.admin.execute(
        "SELECT revision FROM speech_publication_heads WHERE object_id='tts-quality'"
    ).fetchone() == ("1",)
    async with AsyncClient(
        transport=ASGITransport(app=_app(sandbox)), base_url="http://test"
    ) as http:
        changed = await http.post(ACTIVATE_2, json={"expected_current_revision": "1"})
        assert changed.status_code == 200, changed.text
        before = snapshot(sandbox)
        recovered = await http.post(ACTIVATE_1, json=body, headers=headers)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["revision"] == "1"
        assert snapshot(sandbox) == before


@pytest.mark.parametrize(
    "first,second",
    [
        ("activate", "retire"),
        ("activate", "disable"),
        ("retire", "activate"),
        ("disable", "activate"),
        ("retire", "disable"),
        ("disable", "retire"),
    ],
)
async def test_committed_lifecycle_key_conflicts_across_operations(
    sandbox: Sandbox, first: str, second: str
) -> None:
    _seed(sandbox)
    headers = {"Idempotency-Key": "synthetic-shared-lifecycle-key"}

    def request(action: str, *, second_request: bool = False) -> tuple[str, dict[str, object]]:
        if action == "activate":
            return (
                ACTIVATE_1 if second_request else ACTIVATE_2,
                {"expected_current_revision": None if second_request else "2"},
            )
        return RETIRE_URL, {"mode": action, "reason": "synthetic cross-operation reason"}

    path, body = request(first)
    other_path, other_body = request(second, second_request=True)
    async with AsyncClient(
        transport=ASGITransport(app=_app(sandbox)), base_url="http://test"
    ) as http:
        accepted = await http.post(path, json=body, headers=headers)
        assert accepted.status_code == 200, accepted.text
        before = snapshot(sandbox)
        retirements = sandbox.admin.execute(
            "SELECT * FROM speech_publication_retirements"
        ).fetchall()
        disables = sandbox.admin.execute("SELECT * FROM speech_binding_disable_intents").fetchall()
        conflict = await http.post(other_path, json=other_body, headers=headers)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert conflict.json()["error"]["retryable"] is False
        assert conflict.headers["x-should-retry"] == "false"
        assert snapshot(sandbox) == before
        assert (
            sandbox.admin.execute("SELECT * FROM speech_publication_retirements").fetchall()
            == retirements
        )
        assert (
            sandbox.admin.execute("SELECT * FROM speech_binding_disable_intents").fetchall()
            == disables
        )
        replay = await http.post(path, json=body, headers=headers)
        assert replay.status_code == 200 and replay.json() == accepted.json()
        assert snapshot(sandbox) == before
