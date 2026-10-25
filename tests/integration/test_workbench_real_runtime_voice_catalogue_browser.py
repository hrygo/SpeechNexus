"""Formal UI reads the complete public voice revision through actual C HTTP/PG.

The upstream is synthetic native HTTP; this does not qualify publication or inference.
"""

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from tests.integration.test_workbench_runtime import Database, serve
from tests.integration.test_workbench_runtime import database as database
from tests.integration.test_workbench_runtime_voice_catalogue import (
    EXAMPLES,
    catalogue_settings,
    catalogue_source,
)
from tests.integration.workbench_real_runtime import ROOT, Tier

pytestmark = pytest.mark.integration


async def test_canonical_disabled_voice_and_identity_failure_in_the_production_browser(
    database: Database,
) -> None:
    source, reads = catalogue_source(deepcopy(EXAMPLES["disabled-reason-2000"]))
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-voice-catalogue"
    )
    async with serve(source) as upstream:
        tier = Tier(database, **catalogue_settings(str(upstream.base_url)))
        async with tier.running(), httpx.AsyncClient(base_url=tier.origin) as client:
            created = await client.post("/v1/workbench/projects", json={"name": "合成音色目录项目"})
            assert created.status_code == 201, created.text
            process = await asyncio.create_subprocess_exec(
                "node",
                "scripts/dev-env/workbench-voice-catalogue-probe.mjs",
                "--url",
                tier.origin,
                "--output",
                str(output),
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
            assert result["keyboardRead"] and result["retirementVisible"]
            assert result["disabledReasonLength"] == 2000
            assert result["mismatchedIdentityRefused"] and result["staleRecordCleared"]
            assert result["revisionReads"] == 2
            assert result["outbound"] == result["pageErrors"] == 0
    revision_reads = [read for read in reads if "/revisions/" in read["path"]]
    assert revision_reads == [
        {"path": "/v1/nexus/voices/voice-demo-1/revisions/2", "query": {}},
        {"path": "/v1/nexus/voices/voice-other-1/revisions/2", "query": {}},
    ]
