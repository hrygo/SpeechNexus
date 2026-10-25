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
import pytest
from pydantic import SecretStr
from tests.integration.test_workbench_exports_http import prepared
from tests.integration.test_workbench_runtime import Database
from tests.integration.test_workbench_runtime import database as database
from tests.integration.test_workbench_runtime_export_dispatch import BUCKET, CONTENT, _Bundle
from tests.integration.test_workbench_runtime_export_dispatch import objects as objects
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
