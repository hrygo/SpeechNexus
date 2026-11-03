"""The authenticated evidence is tied to the started file worker's frozen snapshot."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.file_tts_runtime import load_file_tts_configuration
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.fake.file_tts_configuration import write_tts_configuration
from tests.unit.gateway.test_barrier_http import HEADERS, settings_for
from tests.unit.gateway.test_file_tts_runtime import configured

PATH = "/internal/v1/tts-configuration-evidence"


async def test_evidence_requires_running_snapshot_and_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = write_tts_configuration(tmp_path / "deployment")
    settings = configured(tmp_path, path)
    expected = load_file_tts_configuration(settings)
    assert expected is not None
    ingress, ledger = fake_ingress(), InMemorySubmissionLedger()

    def build(_: Any) -> tuple[Any, ...]:
        return (
            GatewayBarrierService(ingress=ingress, ledger=ledger),
            ExecutionQueryService(ingress=ingress, ledger=ledger),
            InstanceDrainService(ingress=ingress, ledger=ledger),
            SimpleNamespace(close=lambda: None),
            SimpleNamespace(close=lambda: None),
            None,
            ingress,
            ledger,
        )

    monkeypatch.setattr("speechnexus_gateway.app._build_control", build)
    app = create_app(settings=settings)
    path.write_text("changed after startup snapshot")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(PATH, headers=HEADERS)).status_code == 503
        assert (await client.get(PATH)).status_code == 401
        async with app.router.lifespan_context(app):
            assert (
                await client.get(PATH, headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            response = await client.get(PATH, headers=HEADERS)
            assert response.status_code == 200
            body = response.json()
            assert body == {
                "gateway_policy_digest": expected.policy.digest,
                "policy_revision_id": "policy-tts",
                "voice_id": "voice-demo",
                "voice_revision": "1",
                "voice_revision_id": "voice-rev-1",
                "native_speaker_id": "Vivian",
                "binding_revision_id": "binding-tts",
                "node_id": "node-1",
                "gateway_id": "gateway-1",
                "instance_id": "instance-tts",
                "instance_generation": 1,
                "observed_at": body["observed_at"],
            }
            assert "synthetic-secret" not in response.text and "instructions" not in response.text
            runtime = app.state.file_execution_runtime
            runtime._accepting = False
            assert (await client.get(PATH, headers=HEADERS)).status_code == 503
            runtime._accepting = True
        assert (await client.get(PATH, headers=HEADERS)).status_code == 503


async def test_no_tts_is_unavailable(tmp_path: Path) -> None:
    app = create_app(settings=settings_for(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(PATH, headers=HEADERS)).status_code == 503
