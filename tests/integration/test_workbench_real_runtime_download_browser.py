"""Real production bundle saves exact synthetic media through public download routes.

The injected producer assembles a decodable synthetic ZIP. No real inference, speech
source, physical topology or capacity/quality qualification is claimed.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr
from tests.fake.export_media import synthetic_wav
from tests.integration.test_workbench_exports_http import prepared
from tests.integration.test_workbench_runtime import Database
from tests.integration.test_workbench_runtime import database as database
from tests.integration.test_workbench_runtime_export_dispatch import BUCKET, CONTENT, _Bundle
from tests.integration.test_workbench_runtime_export_dispatch import objects as objects
from tests.integration.test_workbench_runtime_saved_assets_http import (
    ARTIFACT,
    RECEIPT,
    WORKSPACE,
    _Base,
)
from tests.integration.workbench_real_runtime import ROOT, Tier

pytestmark = pytest.mark.integration


async def test_public_export_download_in_the_production_browser(
    database: Database, objects: object
) -> None:
    tier = Tier(
        database,
        export_producer=_Bundle(),
        export_interval_seconds=0.1,
        speech_base_url="http://127.0.0.1:59998",
        object_endpoint=os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"],
        object_bucket=BUCKET,
        object_prefix="exports",
        object_access_key=SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"]),
        object_secret_key=SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"]),
    )
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-download"
    )
    output.mkdir(parents=True, exist_ok=True)
    async with tier.running(), httpx.AsyncClient(base_url=tier.origin, timeout=30) as client:
        await prepared(client, suffix="browser-download")
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-download-probe.mjs",
            "--url",
            tier.origin,
            "--output",
            str(output),
            "--book-title",
            "合成有声书 browser-download",
            "--sha256",
            "sha256:" + hashlib.sha256(CONTENT).hexdigest(),
            "--bytes",
            str(len(CONTENT)),
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 75)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        assert process.returncode == 0, stderr.decode()[-3000:]
        result = json.loads(stdout.decode().splitlines()[-1])
        assert result["downloadedViaProductionUi"] is True
        assert result["downloadActivatedByKeyboard"] is True
        assert result["bytes"] == len(CONTENT)
        assert result["downloadRequests"] == 1 and result["outbound"] == result["pageErrors"] == 0


async def test_saved_copy_download_in_the_production_browser_with_the_source_offline(
    database: Database, objects: object
) -> None:
    source = _Base(content=synthetic_wav())
    server = await asyncio.start_server(source.connection, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    tier = Tier(
        database,
        workspace_id=WORKSPACE,
        speech_base_url=f"http://{host}:{port}",
        object_endpoint=os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"],
        object_bucket=BUCKET,
        object_prefix="saved-browser",
        object_access_key=SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"]),
        object_secret_key=SecretStr(os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"]),
    )
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-saved-download"
    )
    output.mkdir(parents=True, exist_ok=True)
    try:
        async with tier.running(), httpx.AsyncClient(base_url=tier.origin, timeout=30) as client:
            book_id, unit_id, _ = await prepared(client, suffix="browser-saved-download")
            book = await client.get(f"/v1/workbench/books/{book_id}")
            assert book.status_code == 200, book.text
            created = await client.post(
                f"/v1/workbench/projects/{book.json()['project_id']}/saved-assets",
                json={"source_artifact_id": ARTIFACT, "source_receipt_id": RECEIPT},
                headers={"Idempotency-Key": "browser-saved-download-copy"},
            )
            assert created.status_code == 201, created.text
            saved = created.json()
            # The future render-result producer is not delivered. Seed only its labelled
            # synthetic result; public copy creation, join, read and downloads are real.
            async with await psycopg.AsyncConnection.connect(database.owner_dsn) as owner:
                await owner.execute(
                    sql.SQL(
                        "INSERT INTO {}.render_unit_generations"
                        " (id, unit_id, job_id, dependencies_sha256, revision, created_at)"
                        " SELECT %s, id, %s, dependencies_sha256, revision, now()"
                        " FROM {}.render_units WHERE id=%s"
                    ).format(sql.Identifier(database.schema), sql.Identifier(database.schema)),
                    (f"generation-{unit_id}", f"job-{unit_id}", unit_id),
                )
                await owner.execute(
                    sql.SQL(
                        "UPDATE {}.render_units SET status='ready', speech_job_id=%s,"
                        " source_artifact_id=%s, source_receipt_id=%s WHERE id=%s"
                    ).format(sql.Identifier(database.schema)),
                    (f"job-{unit_id}", ARTIFACT, RECEIPT, unit_id),
                )
            unit = await client.get(f"/v2/workbench/render-units/{unit_id}")
            assert unit.status_code == 200, unit.text
            assert unit.json()["saved_asset_id"] == saved["id"]
            reads_after_save = source.reads
            server.close()
            await server.wait_closed()
            process = await asyncio.create_subprocess_exec(
                "node",
                "scripts/dev-env/workbench-saved-download-probe.mjs",
                "--url",
                tier.origin,
                "--output",
                str(output),
                "--book-title",
                "合成有声书 browser-saved-download",
                "--asset-id",
                saved["id"],
                "--sha256",
                saved["sha256"],
                "--bytes",
                str(saved["bytes"]),
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 75)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            assert process.returncode == 0, stderr.decode()[-3000:]
            result = json.loads(stdout.decode().splitlines()[-1])
            assert result["downloadedViaProductionUi"] is True
            assert result["metadataAndDownloadActivatedByKeyboard"] is True
            assert result["bytes"] == len(source.content)
            assert result["metadataRequests"] == result["downloadRequests"] == 1
            assert result["outbound"] == result["pageErrors"] == 0
            assert source.reads == reads_after_save
    finally:
        server.close()
        await server.wait_closed()
        if source.tasks:
            await asyncio.wait_for(asyncio.gather(*source.tasks), 2)
        assert not source.errors
