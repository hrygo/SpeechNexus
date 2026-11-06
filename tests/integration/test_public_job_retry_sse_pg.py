"""Public Job retry and durable event streaming on isolated PostgreSQL.

Every assertion reads the authoritative rows; a skipped database is a failure.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_public_jobs_http import PREFIX, JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.app import create_app

pytestmark = pytest.mark.integration
RETRY = PREFIX + "/{job_id}/retries"
EVENTS = PREFIX + "/{job_id}/events"
RESULT_DIGEST = "sha256:" + "b" * 64


def _app(db: JobSandbox) -> Any:
    from tests.integration.test_public_jobs_http import _settings_for

    return create_app(settings=_settings_for(db), jobs=db.jobs)


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic retry job"},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": "synthetic-retry-reference",
    }
    body.update(changes)
    return body


async def _create(client: AsyncClient, *, key: str | None = None) -> dict[str, Any]:
    headers = {} if key is None else {"Idempotency-Key": key}
    response = await client.post(PREFIX, json=_body(), headers=headers)
    assert response.status_code == 202, response.text
    return cast(dict[str, Any], response.json())


def _fail(db: JobSandbox, job_id: str, code: str = "engine_failure") -> None:
    db.jobs.advance(job_id, "running")
    db.jobs.advance(job_id, "failed", error_code=code)


def _frames(body: str) -> list[dict[str, Any]]:
    """Parse the emitted SSE frames without trusting any client library."""
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
        assert identifier is not None and data, block
        event = json.loads("\n".join(data))
        assert event["id"] == identifier, "SSE id must equal the event identity"
        frames.append(event)
    return frames


async def test_retry_creates_a_linked_replacement_without_touching_the_parent(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        parent = await _create(client, key="synthetic-retry-parent-1")
        _fail(jobs_db, parent["id"])
        before = jobs_db.query(
            "SELECT state, status_version, result_json FROM speech_jobs WHERE job_id = %s",
            (parent["id"],),
        )[0]

        created = await client.post(
            RETRY.format(job_id=parent["id"]), headers={"Idempotency-Key": "synthetic-retry-1"}
        )
        assert created.status_code == 202, created.text
        child = created.json()
        assert child["id"] != parent["id"]
        assert child["status"] == "queued"
        assert "retry_of_job_id" not in child
        assert jobs_db.count("speech_jobs") == 2
        assert jobs_db.count("speech_job_submissions") == 2
        assert jobs_db.count("admission_operations") == 2
        assert jobs_db.count("coordination_jobs") == 2
        assert jobs_db.count("coordination_outbox") == 2

        lineage = jobs_db.query(
            "SELECT job_id, retry_of_job_id, request_digest FROM speech_jobs ORDER BY job_id"
        )
        by_id = {row["job_id"]: row for row in lineage}
        assert by_id[child["id"]]["retry_of_job_id"] == parent["id"]
        assert by_id[parent["id"]]["retry_of_job_id"] is None
        assert by_id[child["id"]]["request_digest"] == by_id[parent["id"]]["request_digest"]

        # The parent keeps its frozen terminal facts; a retry is never a revival.
        after = jobs_db.query(
            "SELECT state, status_version, result_json FROM speech_jobs WHERE job_id = %s",
            (parent["id"],),
        )[0]
        assert after == before
        assert after["state"] == "failed" and after["status_version"] == 3

        # The replacement re-sends the byte-identical canonical intent.
        documents = jobs_db.query(
            "SELECT operation_id, request_document FROM admission_operations"
        )
        assert len(documents) == 2
        assert len({row["request_document"] for row in documents}) == 1
        assert any(row["operation_id"] == child["id"] for row in documents)

        replay = await client.post(
            RETRY.format(job_id=parent["id"]), headers={"Idempotency-Key": "synthetic-retry-1"}
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == child["id"]
        assert jobs_db.count("speech_jobs") == 2
        assert jobs_db.count("coordination_jobs") == 2

        read = await client.get(PREFIX + "/" + child["id"])
        assert read.status_code == 200, read.text
        assert read.json() == child


async def test_retry_key_cannot_move_to_another_parent(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await _create(client, key="synthetic-retry-parent-a")
        second = await _create(client, key="synthetic-retry-parent-b")
        _fail(jobs_db, first["id"])
        _fail(jobs_db, second["id"])
        headers = {"Idempotency-Key": "synthetic-retry-shared"}
        created = await client.post(RETRY.format(job_id=first["id"]), headers=headers)
        assert created.status_code == 202, created.text

        conflicted = await client.post(RETRY.format(job_id=second["id"]), headers=headers)
        assert conflicted.status_code == 409, conflicted.text
        assert conflicted.json()["error"]["code"] == "idempotency_conflict"
        assert jobs_db.count("speech_jobs") == 3

        # The same key must not replay a replacement for a caller-created Job either.
        conflict = await client.post(
            PREFIX,
            json=_body(),
            headers={"Idempotency-Key": "synthetic-retry-shared"},
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize(
    "setup,code",
    [
        ("pending", "job_retry_not_allowed"),
        ("running", "job_retry_not_allowed"),
        ("succeeded", "job_retry_not_allowed"),
        ("unconfirmed", "execution_cleanup_pending"),
    ],
)
async def test_retry_rejects_states_that_cannot_be_replaced(
    jobs_db: JobSandbox, setup: str, code: str
) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job = await _create(client)
        job_id = job["id"]
        if setup == "running":
            jobs_db.jobs.advance(job_id, "running")
        elif setup == "succeeded":
            jobs_db.jobs.advance(job_id, "running")
            jobs_db.jobs.advance(
                job_id,
                "succeeded",
                result_json=json.dumps(
                    {
                        "kind": "tts",
                        "audio_artifact_id": "artifact-synthetic-missing",
                        "receipt_id": "receipt-synthetic-missing",
                    }
                ),
            )
        elif setup == "unconfirmed":
            _fail(jobs_db, job_id, "cancellation_unconfirmed")

        response = await client.post(RETRY.format(job_id=job_id))
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == code
        assert jobs_db.count("speech_jobs") == 1

        missing = await client.post(RETRY.format(job_id="job-00000000000000000000000000000001"))
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "job_not_found"


async def test_event_stream_is_ordered_resumable_and_terminal_once(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job = await _create(client)
        job_id = job["id"]
        _fail(jobs_db, job_id)

        first = await client.get(EVENTS.format(job_id=job_id))
        assert first.status_code == 200, first.text
        assert first.headers["content-type"].startswith("text/event-stream")
        assert first.headers["cache-control"] == "no-cache"
        events = _frames(first.text)
        assert [event["id"] for event in events] == ["1", "2", "3"]
        assert [event["type"] for event in events] == ["job.queued", "job.started", "job.failed"]
        assert all(event["job_id"] == job_id for event in events)
        assert "job.progress" not in {event["type"] for event in events}
        terminal = [event for event in events if event["data"]["status"] == "failed"]
        assert len(terminal) == 1
        assert terminal[0]["data"]["error"]["code"] == "engine_failure"
        assert terminal[0]["data"]["cleanup_state"] == "not_required"

        resume = await client.get(
            EVENTS.format(job_id=job_id), headers={"Last-Event-ID": "2"}
        )
        assert resume.status_code == 200, resume.text
        resumed = _frames(resume.text)
        assert [event["id"] for event in resumed] == ["3"]
        assert [event["type"] for event in resumed] == ["job.failed"]

        # The terminal fact was already delivered; a reconnect must not repeat it.
        settled = await client.get(
            EVENTS.format(job_id=job_id), headers={"Last-Event-ID": "3"}
        )
        assert settled.status_code == 200, settled.text
        assert _frames(settled.text) == []

        missing = await client.get(EVENTS.format(job_id="job-00000000000000000000000000000002"))
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "job_not_found"
        for invalid in ("0", "not-a-sequence", "-1"):
            rejected = await client.get(
                EVENTS.format(job_id=job_id), headers={"Last-Event-ID": invalid}
            )
            assert rejected.status_code == 400, invalid
            assert rejected.json()["error"]["code"] == "invalid_job_request"


async def test_succeeded_terminal_carries_the_committed_artifact(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job = await _create(client)
        job_id = job["id"]
        art = "artifact-synthetic-result"
        receipt = "receipt-synthetic-result"
        jobs_db.admin.execute(
            "INSERT INTO speech_artifacts "
            "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, "
            "sha256, receipt_id, source_operation_id, object_key, storage_version_id, "
            "created_at_us) "
            "VALUES (%s, 'workspace-default', 'audio', 'ready', 'audio/wav', 1024, %s, %s, "
            "%s, %s, 'synthetic-version', 10)",
            (art, RESULT_DIGEST, receipt, job_id, "private/artifacts/workspace-default/" + art),
        )
        jobs_db.jobs.advance(job_id, "running")
        jobs_db.jobs.advance(
            job_id,
            "succeeded",
            result_json=json.dumps(
                {"kind": "tts", "audio_artifact_id": art, "receipt_id": receipt}
            ),
        )

        read = await client.get(PREFIX + "/" + job_id)
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["status"] == "succeeded"
        assert body["cleanup_state"] == "complete"
        assert [item["artifact_id"] for item in body["artifacts"]] == [art]
        assert body["artifacts"][0]["receipt_id"] == receipt
        assert body["result"]["audio_artifact_id"] == art

        stream = await client.get(EVENTS.format(job_id=job_id))
        assert stream.status_code == 200, stream.text
        events = _frames(stream.text)
        assert [event["type"] for event in events] == [
            "job.queued",
            "job.started",
            "job.succeeded",
        ]
        terminal = events[-1]["data"]
        assert terminal["cleanup_state"] == "complete"
        assert [item["artifact_id"] for item in terminal["artifacts"]] == [art]
        assert terminal["result"]["receipt_id"] == receipt

        # A succeeded Job whose committed Artifact row is absent cannot prove success.
        jobs_db.admin.execute("DELETE FROM speech_artifacts WHERE artifact_id = %s", (art,))
        unavailable = await client.get(PREFIX + "/" + job_id)
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "job_unavailable"


async def test_retry_refuses_a_redacted_intent(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job = await _create(client)
        job_id = job["id"]
        _fail(jobs_db, job_id)
        jobs_db.admin.execute(
            "UPDATE admission_operations SET request_document = NULL, "
            "command_redacted_at_us = recorded_at_us, command_deletion_job_id = %s, "
            "command_deletion_evidence_digest = %s WHERE operation_id = %s",
            ("job-deletion-synthetic", RESULT_DIGEST, job_id),
        )
        response = await client.post(RETRY.format(job_id=job_id))
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "job_retry_unavailable"
        assert jobs_db.count("speech_jobs") == 1


async def test_replacement_lineage_is_immutable_in_postgres(jobs_db: JobSandbox) -> None:
    import psycopg

    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        parent = await _create(client)
        _fail(jobs_db, parent["id"])
        child = (
            await client.post(RETRY.format(job_id=parent["id"]))
        ).json()
        with pytest.raises(psycopg.errors.CheckViolation):
            jobs_db.admin.execute(
                "UPDATE speech_jobs SET retry_of_job_id = NULL, "
                "status_version = status_version + 1 WHERE job_id = %s",
                (child["id"],),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            jobs_db.admin.execute(
                "UPDATE speech_jobs SET retry_of_job_id = job_id, "
                "status_version = status_version + 1 WHERE job_id = %s",
                (child["id"],),
            )
