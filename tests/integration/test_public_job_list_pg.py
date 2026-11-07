"""Public listJobs keyset pages and the actual query plan on isolated PG."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import pytest
from alembic import command
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts import parse
from tests.integration.test_public_job_progress_migration_pg import _isolated_schema, _url
from tests.integration.test_public_jobs_http import DIGEST, PREFIX, JobSandbox, _app, _body
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs

pytestmark = pytest.mark.integration


def _seed_rows(db: JobSandbox, source: str, *, count: int, workspace: str) -> None:
    """Synthetic committed read facts, including equal creation timestamps."""
    db.query(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, created_at_us, updated_at_us) "
        "SELECT 'job-list-' || %s || '-' || lpad(n::text, 8, '0'), %s, "
        "CASE WHEN n %% 2 = 0 THEN 'asr' ELSE 'tts' END, model_id, request_digest, "
        "CASE WHEN n %% 3 = 0 THEN 'running' ELSE 'pending' END, 1, n / 3, n / 3 "
        "FROM speech_jobs CROSS JOIN generate_series(1, %s) AS n WHERE job_id = %s",
        (workspace, workspace, count, source),
    )
    db.query(
        "INSERT INTO speech_job_submissions (job_id, request_id, client_reference, "
        "effective_configuration_json, execution_revision, progress_unit, progress_total, "
        "submitted_at_us) SELECT j.job_id, s.request_id, s.client_reference, "
        "s.effective_configuration_json, s.execution_revision, s.progress_unit, s.progress_total, "
        "j.created_at_us FROM speech_jobs AS j CROSS JOIN speech_job_submissions AS s "
        "WHERE s.job_id = %s AND j.job_id LIKE %s",
        (source, "job-list-" + workspace + "-%"),
    )


async def test_pages_and_filters_use_committed_facts(jobs_db: JobSandbox) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(jobs_db)), base_url="http://test"
    ) as client:
        created = await client.post(PREFIX, json=_body())
        assert created.status_code == 202, created.text
        assert created.json()["workspace_id"] == "workspace-default"
        source = created.json()["id"]
        _seed_rows(jobs_db, source, count=17, workspace="workspace-default")
        jobs_db.query(
            "INSERT INTO speech_workspaces "
            "(workspace_id, display_name, workspace_state, created_at_us) "
            "VALUES ('workspace-other', 'Synthetic other workspace', 'active', 1)"
        )
        other = await client.post(PREFIX, json=_body(workspace_id="workspace-other"))
        assert other.status_code == 202, other.text

        expected = jobs_db.query(
            "SELECT job_id, kind, state FROM speech_jobs WHERE workspace_id = 'workspace-default' "
            "ORDER BY created_at_us, job_id"
        )
        query: dict[str, str] = {"limit": "4"}
        seen: list[str] = []
        while True:
            response = await client.get(PREFIX, params=query)
            assert response.status_code == 200, response.text
            body = parse("JobList", response.json())
            repeated = await client.get(PREFIX, params=query)
            assert repeated.json() == body
            seen.extend(job["id"] for job in body["data"])
            if not body["has_more"]:
                assert body["next_cursor"] is None
                break
            assert body["next_cursor"] is not None
            query["cursor"] = body["next_cursor"]
        assert seen == [row["job_id"] for row in expected]
        assert len(set(seen)) == 18

        for status, kind in (("queued", "asr"), ("running", "tts"), (None, "asr")):
            filters = {"kind": kind, "limit": "2"}
            if status is not None:
                filters["status"] = status
            filtered: list[str] = []
            while True:
                response = await client.get(PREFIX, params=filters)
                assert response.status_code == 200, response.text
                filtered.extend(job["id"] for job in response.json()["data"])
                if not response.json()["has_more"]:
                    break
                filters["cursor"] = response.json()["next_cursor"]
            assert filtered == [
                row["job_id"]
                for row in expected
                if row["kind"] == kind
                and (
                    status is None or row["state"] == ("pending" if status == "queued" else status)
                )
            ]
        for workspace in ("workspace-other", "workspace-missing"):
            response = await client.get(PREFIX, params={"workspace_id": workspace})
            assert response.status_code == 200
            assert [job["id"] for job in response.json()["data"]] == (
                [other.json()["id"]] if workspace == "workspace-other" else []
            )
        for invalid in (
            {"status": "pending"},
            {"kind": "unknown"},
            {"limit": "0"},
            {"limit": "-1"},
            {"limit": "101"},
            {"cursor": "invalid"},
        ):
            response = await client.get(PREFIX, params=invalid)
            assert response.status_code == 400, response.text
            assert parse("ErrorResponse", response.json())["error"]["code"] == "invalid_job_request"


class _ObservedJobs(PostgresPublicJobs):
    """Counts the READ ONLY transactions a request starts, one call each."""

    calls: list[tuple[str, tuple[object, ...]]]

    def __init__(self, pool: Any) -> None:
        super().__init__(pool)
        self.calls = []

    def _read(
        self,
        query: str,
        parameters: tuple[object, ...],
        *,
        submission_key: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((query, parameters))
        return super()._read(query, parameters, submission_key=submission_key)


def _nodes(plan: dict[str, Any]) -> Iterable[dict[str, Any]]:
    pending = [plan]
    while pending:
        node = pending.pop()
        pending.extend(node.get("Plans", []))
        yield node


def _assert_keyset_plan(result: list[dict[str, Any]], *, keyset: bool) -> None:
    """The composite index must serve the order; a Sort or Seq Scan is a failure.

    Checking only that the index name appears somewhere would also accept a plan
    that reads the index and then sorts it, or a page whose keyset predicate was
    dropped from the index condition.
    """
    nodes = list(_nodes(result[0]["Plan"]))
    assert not [node for node in nodes if node["Node Type"] in ("Sort", "Incremental Sort")], (
        "ORDER BY must be served by the index order, not a Sort"
    )
    scans = [node for node in nodes if node.get("Relation Name") == "speech_jobs"]
    assert len(scans) == 1, "the page must read speech_jobs exactly once"
    scan = scans[0]
    assert scan["Node Type"] == "Index Scan", "speech_jobs must not be read by a Seq Scan"
    assert scan["Index Name"] == "speech_jobs_workspace_created_id"
    assert scan["Scan Direction"] == "Forward", "ascending keyset pages scan the index forward"
    if keyset:
        assert "ROW(" in scan.get("Index Cond", ""), (
            "the keyset predicate must be an index condition, not a post-filter"
        )


async def test_actual_listing_query_uses_composite_index(
    jobs_db: JobSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(jobs_db)), base_url="http://test"
    ) as client:
        created = await client.post(PREFIX, json=_body())
        assert created.status_code == 202, created.text
    _seed_rows(jobs_db, created.json()["id"], count=10_000, workspace="workspace-default")
    jobs_db.query("ANALYZE speech_jobs")
    jobs_db.query("ANALYZE speech_job_submissions")
    adapter = _ObservedJobs(jobs_db.pool)
    for after in (None, (1000, "job-list-workspace-default-00003000")):
        page = adapter.list_jobs(
            workspace_id="workspace-default",
            state=None,
            kind=None,
            after=after,
            limit=51,
        )
        assert len(page) == 51
        query, parameters = adapter.calls[-1]
        # Explain precisely the SQL executed by the runtime adapter, without
        # disabling seqscan or substituting a simplified demonstration query.
        result = jobs_db.query("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, parameters)[0][
            "QUERY PLAN"
        ]
        # Keep successful plans in normal CI logs too; all inputs are synthetic.
        with capsys.disabled():
            print(
                "listJobs EXPLAIN "
                + json.dumps(
                    {
                        "query": query,
                        "parameters": parameters,
                        "plan": result,
                    },
                    sort_keys=True,
                )
            )
        _assert_keyset_plan(result, keyset=after is not None)


BATCH_WORKSPACE = "workspace-batch"
BATCH_PAGE = 100


def _seed_succeeded_page(db: JobSandbox, source: str, *, count: int, workspace: str) -> None:
    """Succeeded Jobs whose committed results each name one ready Artifact."""
    db.query(
        "INSERT INTO speech_workspaces "
        "(workspace_id, display_name, workspace_state, created_at_us) "
        "VALUES (%s, 'Synthetic batch workspace', 'active', 1)",
        (workspace,),
    )
    db.query(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, result_json, created_at_us, updated_at_us) "
        "SELECT 'job-batch-' || lpad(n::text, 3, '0'), %s, 'tts', model_id, request_digest, "
        "'succeeded', 1, json_build_object("
        "'kind', 'tts', "
        "'audio_artifact_id', 'artifact-batch-' || lpad(n::text, 3, '0'), "
        "'receipt_id', 'receipt-batch-' || lpad(n::text, 3, '0'))::text, "
        "n, n FROM speech_jobs CROSS JOIN generate_series(1, %s) AS n WHERE job_id = %s",
        (workspace, count, source),
    )
    db.query(
        "INSERT INTO speech_job_submissions (job_id, request_id, client_reference, "
        "effective_configuration_json, execution_revision, progress_unit, progress_total, "
        "submitted_at_us) SELECT j.job_id, s.request_id, s.client_reference, "
        "s.effective_configuration_json, s.execution_revision, s.progress_unit, s.progress_total, "
        "j.created_at_us FROM speech_jobs AS j CROSS JOIN speech_job_submissions AS s "
        "WHERE s.job_id = %s AND j.workspace_id = %s",
        (source, workspace),
    )
    db.query(
        "INSERT INTO speech_artifacts (artifact_id, workspace_id, artifact_kind, artifact_state, "
        "media_type, bytes, sha256, receipt_id, source_operation_id, object_key, "
        "storage_version_id, created_at_us) "
        "SELECT 'artifact-batch-' || lpad(n::text, 3, '0'), %s, 'audio', 'ready', 'audio/wav', "
        "1024, %s, 'receipt-batch-' || lpad(n::text, 3, '0'), NULL, "
        "'private/artifacts/' || %s || '/artifact-' || lpad(n::text, 3, '0'), "
        "'synthetic-version', n FROM generate_series(1, %s) AS n",
        (workspace, DIGEST, workspace, count),
    )


async def test_one_page_reads_artifacts_in_one_transaction(jobs_db: JobSandbox) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(jobs_db)), base_url="http://test"
    ) as client:
        created = await client.post(PREFIX, json=_body())
        assert created.status_code == 202, created.text
    _seed_succeeded_page(jobs_db, created.json()["id"], count=BATCH_PAGE, workspace=BATCH_WORKSPACE)
    adapter = _ObservedJobs(jobs_db.pool)
    service = PublicJobs(adapter, adapter)
    app = _app(replace(jobs_db, jobs=service))
    params = {"workspace_id": BATCH_WORKSPACE, "limit": str(BATCH_PAGE)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["data"]) == BATCH_PAGE
        assert [len(job["artifacts"]) for job in body["data"]] == [1] * BATCH_PAGE
        # The listing read plus one Artifact read; never one read per Job.
        assert len(adapter.calls) == 2, [query for query, _ in adapter.calls]
        assert "ORDER BY j.created_at_us ASC" in adapter.calls[0][0]
        assert "artifact_id = ANY(" in adapter.calls[1][0]

        # A page that cannot prove one committed Artifact still fails closed.
        jobs_db.query("DELETE FROM speech_artifacts WHERE artifact_id = 'artifact-batch-001'")
        adapter.calls.clear()
        unavailable = await client.get(PREFIX, params=params)
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["error"]["code"] == "job_unavailable"
        assert len(adapter.calls) == 2


def _created_indexes(query: Any) -> dict[str, str]:
    rows = query.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname LIKE 'speech_jobs_workspace_created%' ORDER BY indexname"
    ).fetchall()
    return {row["indexname"]: row["indexdef"] for row in rows}


def test_0042_drops_only_the_superseded_job_index() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "0041_voice_drafts")
        query.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "created_at_us, updated_at_us) VALUES ('job-preserved-0042', 'workspace-default', "
            "'tts', 'model-synthetic', %s, 7, 7)",
            ("sha256:" + "b" * 64,),
        )
        before = query.execute("SELECT * FROM speech_jobs").fetchall()
        # Both overlapping indexes exist while 0041 is the head.
        assert set(_created_indexes(query)) == {
            "speech_jobs_workspace_created",
            "speech_jobs_workspace_created_id",
        }

        command.upgrade(config, "head")
        remaining = _created_indexes(query)
        # 0042 removes only the superseded index; the keyset index keeps its shape.
        assert set(remaining) == {"speech_jobs_workspace_created_id"}
        assert (
            "(workspace_id, created_at_us, job_id)" in remaining["speech_jobs_workspace_created_id"]
        )
        assert query.execute("SELECT * FROM speech_jobs").fetchall() == before

        command.downgrade(config, "0041_voice_drafts")
        restored = _created_indexes(query)
        assert set(restored) == {
            "speech_jobs_workspace_created",
            "speech_jobs_workspace_created_id",
        }
        assert "(workspace_id, created_at_us DESC)" in restored["speech_jobs_workspace_created"]
        assert query.execute("SELECT * FROM speech_jobs").fetchall() == before

        command.upgrade(config, "head")
        assert set(_created_indexes(query)) == {"speech_jobs_workspace_created_id"}
        assert query.execute("SELECT * FROM speech_jobs").fetchall() == before


def test_migration_preserves_jobs_and_reverses_only_the_new_index() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "0039_public_job_progress")
        query.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "created_at_us, updated_at_us) VALUES ('job-preserved', 'workspace-default', 'tts', "
            "'model-synthetic', %s, 1, 1)",
            ("sha256:" + "a" * 64,),
        )
        before = query.execute("SELECT * FROM speech_jobs").fetchall()
        for operation in (command.upgrade, command.downgrade, command.upgrade):
            target = (
                "0039_public_job_progress"
                if operation == command.downgrade
                else "0040_public_job_listing"
            )
            operation(config, target)
            index = query.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                "AND indexname = 'speech_jobs_workspace_created_id'"
            ).fetchall()
            assert bool(index) is (operation != command.downgrade)
            if index:
                assert "(workspace_id, created_at_us, job_id)" in index[0]["indexdef"]
            assert query.execute("SELECT * FROM speech_jobs").fetchall() == before
