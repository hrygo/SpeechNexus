"""A kind no queue can claim is refused on the real store, with zero rows (#1080).

The measured defect was the opposite: ``POST /v1/nexus/jobs`` answered ``202`` for
``voice_design``/``voice_clone``, the legacy branch wrote an ``admission_operations``
anchor and a ``coordination_jobs`` row and never an attempt or a lease -- while the
production claim inner-joins exactly those two, so ``claimed:0`` held forever.

These cases run on a migrated PostgreSQL schema with the real writer, so the "zero
rows" claim is a table count rather than a fake store's bookkeeping. The ``tts``
control asserts that the same request path still creates, and that the dispatcher
still claims what the refusal left alone.

Assembly note: ``jobs_db`` has no admission pool, so this store takes the legacy
insert and the claim below runs the dispatch SQL without the admission filter. The
admission-filtered claim on ``tts`` is covered by
``test_public_job_atomic_admission_pg.py::test_created_job_is_visible_to_the_a3_claim_join``
(dual-database, CI); neither case is claimed here.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_public_jobs_http import JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db
from tests.integration.test_voice_drafts_pg import _body, _provider
from tests.integration.test_voice_drafts_pg import voice_db as voice_db
from tests.integration.test_voice_drafts_pg import voice_sandbox as voice_sandbox
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresDispatchQueue

pytestmark = pytest.mark.integration

VOICE_KINDS = ("voice_design", "voice_clone", "voice_preview")
INPUTS: dict[str, dict[str, Any]] = {
    "voice_design": {"description": "Synthetic voice", "preview_text": "Synthetic preview"},
    "voice_clone": {
        "reference_audio_asset_id": "synthetic-reference",
        "source_use_confirmed": True,
    },
    "voice_preview": {
        "voice_draft_id": "draft-absent",
        "draft_revision": "1",
        "text": "synthetic preview",
    },
}
TABLES = (
    "speech_jobs",
    "speech_job_intents",
    "speech_job_events",
    "speech_job_submissions",
    "admission_operations",
    "admission_attempts",
    "admission_requests",
    "admission_leases",
    "coordination_jobs",
    "coordination_outbox",
    "speech_voice_drafts",
    "speech_voice_draft_revisions",
)


def _counts(db: JobSandbox) -> dict[str, int]:
    return {table: db.count(table) for table in TABLES}


async def _post(db: JobSandbox, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    from tests.integration.test_public_jobs_http import _app

    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="http://localhost"
    ) as http:
        return await http.post("/v1/nexus/jobs", json=body, headers=headers)


@pytest.mark.parametrize("kind", VOICE_KINDS)
async def test_an_undispatchable_kind_is_refused_and_writes_no_row(
    jobs_db: JobSandbox, kind: str
) -> None:
    before = _counts(jobs_db)

    response = await _post(
        jobs_db, {"kind": kind, "model": "tts-quality", "input": INPUTS[kind]}, {}
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "job_kind_not_dispatchable"
    assert error["retryable"] is False
    assert "cannot be dispatched" in error["message"]
    # The two tables #1080 measured, and every other row the legacy branch wrote.
    after = _counts(jobs_db)
    assert after == before, {
        table: (before[table], after[table]) for table in before if before[table] != after[table]
    }


@pytest.mark.parametrize("kind", VOICE_KINDS)
async def test_the_refusal_does_not_replay_an_old_key(jobs_db: JobSandbox, kind: str) -> None:
    """The refusal is about the kind, so it precedes the stored-intent lookup."""
    before = _counts(jobs_db)
    headers = {"Idempotency-Key": "old-voice-key-0001"}
    body = {"kind": kind, "model": "tts-quality", "input": INPUTS[kind]}
    for _attempt in range(2):
        response = await _post(jobs_db, body, headers)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "job_kind_not_dispatchable"
    assert _counts(jobs_db) == before


async def test_tts_is_still_accepted_and_still_dispatched(jobs_db: JobSandbox) -> None:
    """The control: the refusal must not close the lane that really dispatches."""
    before = _counts(jobs_db)
    response = await _post(
        jobs_db,
        {
            "kind": "tts",
            "model": "tts-quality",
            "input": {"text": "synthetic hello"},
            "voice": {"id": "voice-vivian", "revision": "2"},
        },
        {},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    # The rows a refused kind never wrote: Job, intent, submission, admission anchor,
    # coordination claim and outbox.
    after = _counts(jobs_db)
    for table in (
        "speech_jobs",
        "speech_job_intents",
        "speech_job_events",  # the accepted create's first event
        "speech_job_submissions",
        "admission_operations",
        "coordination_jobs",
        "coordination_outbox",
    ):
        assert after[table] == before[table] + 1, table

    claims = await PostgresDispatchQueue(jobs_db.pool).claim_due("worker-after-refusal", 30_000, 1)
    assert [claim.job.job_id for claim in claims] == [job_id]
    # The claimed command is the public intent of the accepted Job, not a voice anchor.
    assert json.loads(claims[0].job.request_document)["kind"] == "tts"


async def test_a_retry_of_a_refused_kind_commits_no_replacement(
    voice_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Job an older release left behind cannot be retried into a new one.

    ``retry`` rebuilds the parent's kind from its stored intent, so the guard has to
    sit on that entry point too: without it, cancelling a pre-upgrade ``voice_design``
    Job and retrying it would answer 202 with a Job no worker can claim. The parent is
    created with the refusal emptied for that one call (the pre-upgrade writer), then
    cancelled through the product entry point and settled, so the retry sees a frozen
    terminal parent -- the state the state check asks for.

    The retry itself runs against the real refusal: 409, and every counted row is
    exactly what it was. Whatever the parent's history, no replacement is committed.
    """

    db = voice_db
    monkeypatch.setattr(
        "speechnexus_backend.speech.application.public_jobs.UNDISPATCHABLE_JOB_KINDS", ()
    )
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        created = await http.post("/v1/nexus/voice-designs", json=_body("design"))
        assert created.status_code == 202, created.text
        job_id = created.json()["job"]["id"]
        monkeypatch.undo()
        cancelled = await http.post("/v1/nexus/jobs/" + job_id + "/cancellations")
        # Nothing was executing, so the admitted cancellation settles in the same
        # answer; the contract allows 200 for that and 202 for the pending case.
        assert cancelled.status_code in (200, 202), cancelled.text
        assert cancelled.json()["status"] == "cancelled", cancelled.text
        parent = db.jobs.get(job_id)
        assert parent.state == "cancelled", parent.state

        before = _counts(db)
        response = await http.post("/v1/nexus/jobs/" + job_id + "/retries")

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "job_retry_not_allowed"
    assert error["retryable"] is False
    after = _counts(db)
    assert after == before, {
        table: (before[table], after[table]) for table in before if before[table] != after[table]
    }
    # The parent is untouched as well: a retry never writes the Job it names.
    assert db.query("SELECT state, status_version FROM speech_jobs WHERE job_id=%s", (job_id,)) == [
        {"state": "cancelled", "status_version": parent.status_version}
    ]
