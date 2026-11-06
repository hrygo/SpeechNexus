"""The started fact follows every committed event, not the status version.

A pending Job used to carry exactly one event, which made ``status_version``
coincide with the next stored sequence. This pins the derived sequence so one
extra pending fact cannot collide with the started event's primary key.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_public_jobs_http import PREFIX, JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
)

pytestmark = pytest.mark.integration


def _body() -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic started sequence job"},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": "synthetic-started-sequence",
    }


def _app(db: JobSandbox) -> Any:
    from tests.integration.test_public_jobs_http import _settings_for

    return create_app(settings=_settings_for(db), jobs=db.jobs)


def _sequences(db: JobSandbox, job_id: str) -> list[tuple[int, str]]:
    rows = db.query(
        "SELECT event_seq, state FROM speech_job_events WHERE job_id = %s ORDER BY event_seq",
        (job_id,),
    )
    return [(int(row["event_seq"]), str(row["state"])) for row in rows]


async def test_started_event_uses_the_next_stored_sequence(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-started-sequence-0001"}
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]

        # A second pending fact must not be mistaken for the started event.
        jobs_db.admin.execute(
            "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
            "VALUES (%s, 1, 'pending', 2)",
            (job_id,),
        )

        states = PostgresDispatchStateStore(jobs_db.pool)
        claims = await PostgresDispatchQueue(jobs_db.pool).claim_due("worker-sequence-a", 30_000, 1)
        assert len(claims) == 1
        running = await states.mark_running(claims[0].job)
        assert running.state == "running"
        assert _sequences(jobs_db, job_id) == [(0, "pending"), (1, "pending"), (2, "running")]

        claim = replace(claims[0], job=running)
        assert await states.record_progress(claim, unit="audio_ms", completed=10, total=None)
        await states.finish(claim, "failed", result_json=None, error_code="engine_failure")
        assert _sequences(jobs_db, job_id) == [
            (0, "pending"),
            (1, "pending"),
            (2, "running"),
            (3, "running"),
            (4, "failed"),
        ]
