"""Public Job progress on isolated PostgreSQL.

Progress is a persisted event fact, not a value recomputed from configuration:
only new generated-audio observations are stored, a terminal Job never receives
one, and a fresh reader replays exactly the committed rows. A skipped database
is a failure.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_public_jobs_http import PREFIX, JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.job_dispatch import ClaimedJob
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs

pytestmark = pytest.mark.integration
EVENTS = PREFIX + "/{job_id}/events"


def _app(db: JobSandbox) -> Any:
    from tests.integration.test_public_jobs_http import _settings_for

    return create_app(settings=_settings_for(db), jobs=db.jobs)


def _body() -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic progress job"},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": "synthetic-progress-reference",
    }


def _frames(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        identifier: str | None = None
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("id: "):
                identifier = line[4:]
            elif line.startswith("data: "):
                data.append(line[6:])
        event = json.loads("\n".join(data))
        assert identifier == event["id"]
        frames.append(event)
    return frames


async def _running_claim(
    db: JobSandbox,
) -> tuple[PostgresDispatchStateStore, ClaimedJob]:
    queue = PostgresDispatchQueue(db.pool)
    states = PostgresDispatchStateStore(db.pool)
    claims = await queue.claim_due("worker-progress-a", 30_000, 1)
    assert len(claims) == 1
    running = await states.mark_running(claims[0].job)
    return states, replace(claims[0], job=running)


async def test_progress_is_persisted_only_when_the_observed_value_advances(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-progress-0001"}
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        assert created.json()["progress"] == {
            "unit": "audio_ms",
            "completed": 0,
            "total": None,
        }

        states, claim = await _running_claim(jobs_db)
        assert await states.record_progress(claim, unit="audio_ms", completed=250, total=None)
        # Re-reading the same durable watermark is not a second fact.
        assert not await states.record_progress(claim, unit="audio_ms", completed=250, total=None)
        assert await states.record_progress(claim, unit="audio_ms", completed=750, total=None)
        # A stale observation must never move the stored value backwards.
        assert not await states.record_progress(claim, unit="audio_ms", completed=500, total=None)

        read = await client.get(PREFIX + "/" + job_id)
        assert read.status_code == 200, read.text
        assert read.json()["progress"] == {
            "unit": "audio_ms",
            "completed": 750,
            "total": None,
        }

        # A fresh store instance (a restarted writer) sees the persisted fact and
        # refuses to append the same watermark again while the Job still runs.
        assert not await PostgresDispatchStateStore(jobs_db.pool).record_progress(
            claim, unit="audio_ms", completed=750, total=None
        )
        # A fact in another unit cannot replace the frozen submission unit.
        assert not await states.record_progress(
            claim, unit="text_codepoints", completed=750, total=None
        )

        # The stream replays only after the Job is terminal: an open SSE
        # response cannot be buffered by the ASGI test transport.
        await states.finish(claim, "failed", result_json=None, error_code="engine_failure")
        stream = await client.get(EVENTS.format(job_id=job_id))
        assert stream.status_code == 200, stream.text
        events = _frames(stream.text)
        assert [event["type"] for event in events] == [
            "job.queued",
            "job.started",
            "job.progress",
            "job.progress",
            "job.failed",
        ]
        assert [event["data"]["progress"]["completed"] for event in events[2:4]] == [
            250,
            750,
        ]
        assert [event["id"] for event in events] == ["1", "2", "3", "4", "5"]

        # A fresh service instance reads the same facts; a restart cannot replay
        # a duplicate progress value because the stored fact already advanced.
        adapter = PostgresPublicJobs(jobs_db.pool)
        restarted = PublicJobs(adapter, adapter)
        restarted_job = restarted.get(job_id)
        assert restarted_job.progress is not None
        assert restarted_job.progress.completed == 750
        replayed = restarted.events(job_id, after_seq=-1, limit=100)
        assert [event.progress.completed for event in replayed if event.progress is not None] == [
            250,
            750,
        ]
        assert not await states.record_progress(claim, unit="audio_ms", completed=750, total=None)


async def test_terminal_job_stops_progress_and_a_restart_keeps_the_last_value(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-progress-0002"}
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]

        states, claim = await _running_claim(jobs_db)
        assert await states.record_progress(claim, unit="audio_ms", completed=1_000, total=None)
        terminal = await states.finish(
            claim, "failed", result_json=None, error_code="engine_failure"
        )
        assert terminal.state == "failed"
        before = jobs_db.count("speech_job_events")

        # The public Job is terminal, so no later watermark can become progress.
        assert not await states.record_progress(claim, unit="audio_ms", completed=2_000, total=None)
        assert jobs_db.count("speech_job_events") == before

        with pytest.raises(psycopg.errors.CheckViolation):
            jobs_db.admin.execute(
                "INSERT INTO speech_job_events "
                "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                "progress_completed) VALUES (%s, %s, 'running', 20, 'progress', "
                "'audio_ms', %s)",
                (job_id, before, 2_000),
            )

        adapter = PostgresPublicJobs(jobs_db.pool)
        restarted = PublicJobs(adapter, adapter)
        restarted_job = restarted.get(job_id)
        assert restarted_job.progress is not None
        assert restarted_job.progress.completed == 1_000
        assert restarted_job.state == "failed"

        stream = await client.get(EVENTS.format(job_id=job_id))
        events = _frames(stream.text)
        assert [event["type"] for event in events] == [
            "job.queued",
            "job.started",
            "job.progress",
            "job.failed",
        ]
        assert events[-1]["data"]["error"]["code"] == "engine_failure"
        # Resuming after the terminal fact emits nothing new.
        resumed = await client.get(
            EVENTS.format(job_id=job_id), headers={"Last-Event-ID": events[-1]["id"]}
        )
        assert _frames(resumed.text) == []
