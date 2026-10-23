"""Actual TCP HTTP, lifespan wiring and restricted-role PostgreSQL; no inference model."""

import asyncio
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from speechnexus_contracts.execution import parse
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_barrier import REQUEST, takeover
from tests.integration.test_gateway_stop_evidence import counters
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.app import create_app
from speechnexus_gateway.domain.fencing import IngressUnavailableError
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.settings import Settings

pytestmark = pytest.mark.integration
TOKEN = "synthetic-http-gateway-component-token"


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_real_http_wires_persistent_fencing_and_exclusive_process_lifetime(
    foundation: Foundation, tmp_path: Path
) -> None:
    settings = Settings(
        control_database_url=SecretStr(
            make_conninfo(
                foundation.database.url,
                user=foundation.gateway_role,
                password="synthetic_foundation_test",
            )
        ),
        control_schema=foundation.database.schema,
        node_id="host-test",
        gateway_id="gateway-test",
        component_token=SecretStr(TOKEN),
        ingress_lock_directory=tmp_path,
    )
    app = create_app(settings=settings)  # Real composition root, no injected service/readiness.
    takeover(foundation)
    before = counters(foundation)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    path = "/internal/v1/gateways/gateway-test/fencing-barrier"
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "synthetic-live-http"}
    body = {
        "pool_id": REQUEST.pool_id,
        "new_owner_scheduler_id": REQUEST.new_owner_scheduler_id,
        "min_accepted_fencing_token": REQUEST.min_accepted_fencing_token,
        "abort_in_flight": False,
    }
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            serving = workers.submit(server.run, sockets=[sock])
            try:
                deadline = time.monotonic() + 5
                while not server.started and not serving.done() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert server.started, "the real gateway did not complete startup"
                # A second app cannot replace this live gateway identity.
                second_app = create_app(settings=settings)

                async def start_replacement() -> None:
                    async with second_app.router.lifespan_context(second_app):
                        pytest.fail("a live gateway identity was replaced")

                with pytest.raises(IngressUnavailableError):
                    asyncio.run(start_replacement())
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                    ready = client.get("/readyz")
                    assert ready.status_code == 200
                    assert {row["name"] for row in ready.json()["checks"]} == {
                        "control_postgresql",
                        "gateway_ingress",
                    }
                    unauthorized = client.post(path, json=body)
                    assert unauthorized.status_code == 401
                    assert not foundation.database.query("SELECT * FROM gateway_fencing_barriers")
                    sealed = client.post(path, json=body, headers=headers)
                    assert sealed.status_code == 200
                    parse("GatewayFencingBarrierResponse", sealed.json())
                    assert sealed.json()["submitting_intent_ids"] == ["submit-test"]
                    assert sealed.headers["x-request-id"] == "synthetic-live-http"
                    persisted = foundation.database.query("SELECT * FROM gateway_fencing_barriers")
                    assert persisted[0]["min_fencing_token"] == 2
                    assert client.post(path, json=body, headers=headers).status_code == 200
                    assert (
                        foundation.database.query("SELECT * FROM gateway_fencing_barriers")
                        == persisted
                    )
                    takeover(foundation, token=3)
                    stale = client.post(path, json=body, headers=headers)
                    assert stale.status_code == 409
                    assert stale.json()["error"]["code"] == "stale_fencing_token"
                    advanced = client.post(
                        path, json=body | {"min_accepted_fencing_token": 3}, headers=headers
                    )
                    assert advanced.status_code == 200
                    assert advanced.json()["enforced_min_fencing_token"] == 3
                    assert counters(foundation) == before
            finally:
                server.should_exit = True
                serving.result(5)
    finally:
        sock.close()
    assert app.state.barrier_control is None
    replacement = ProcessIngressOwnership(tmp_path, node_id="host-test", gateway_id="gateway-test")
    replacement.close()  # Lifespan released its lease after its requests settled.
