"""Worker settings and stopping never select implicit storage or release resources."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from speechnexus_backend.runtime_delivery_worker import RuntimeDeliverySettings, run_delivery
from speechnexus_backend.speech.application.runtime_delivery import RuntimeDeliveryService
from speechnexus_backend.speech.domain.runtime_delivery import DeliveryBatch
from tests.unit.test_runtime_delivery import CANDIDATE, CLAIM, setup


def configuration(**changes: object) -> RuntimeDeliverySettings:
    values: dict[str, object] = {
        "process_role": "job-worker",
        "node_id": "worker-host",
        "instance_id": "worker-instance",
        "database_url": SecretStr(
            "postgresql://delivery:synthetic-private-password@127.0.0.1:55432/test"
        ),
        "database_schema": "speech_test",
        "target_node_id": "host-test",
        "controller_address": "127.0.0.1",
        "controller_port": 8443,
        "controller_token": SecretStr("synthetic-private-node-token"),
    }
    return RuntimeDeliverySettings.model_validate(values | changes)


def test_configuration_keeps_endpoint_and_role_explicit_and_secrets_out_of_repr() -> None:
    settings = configuration()
    assert settings.endpoint().node_id == "host-test"
    assert settings.endpoint().base_url == "https://127.0.0.1:8443"
    assert settings.holder_id == "worker-host:worker-instance"
    text = repr(settings)
    assert "synthetic-private-password" not in text and "synthetic-private-node-token" not in text
    assert "127.0.0.1" not in text


@pytest.mark.parametrize(
    "change",
    [
        {"process_role": "api"},
        {"node_id": "../worker"},
        {"controller_address": "controller.local"},
        {"controller_address": "203.0.113.10"},
        {"controller_port": 0},
        {"controller_token": SecretStr("short")},
        {"lease_seconds": 3},
        {"call_timeout_seconds": float("inf")},
        {"storage_timeout_seconds": float("nan")},
        {"batch_size": 33},
        {"retry_seconds": 0.001},
        {"database_schema": "public; SELECT 1"},
        {"database_url": SecretStr("")},
        {"database_url": SecretStr("postgresql://")},
        {"database_url": SecretStr("dbname=test user=delivery")},
        {"database_url": SecretStr("postgresql://127.0.0.1/test")},
    ],
)
def test_invalid_or_implicit_operating_configuration_fails_before_io(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        configuration(**change)


async def test_stopping_finishes_only_the_current_original_and_never_claims_the_next() -> None:
    service, transactions, controller = setup()
    stop = asyncio.Event()

    async def response(original: object) -> object:
        stop.set()
        return CANDIDATE

    controller.submit.side_effect = response
    result = await service.run_once(CLAIM, limit=32, should_stop=stop.is_set)
    assert result.claimed == result.acknowledged == 1
    transactions.claim.assert_called_once_with(CLAIM)
    controller.submit.assert_awaited_once()
    controller.query.assert_not_awaited()


async def test_loop_stops_between_passes_without_a_remote_stop_or_extra_poll() -> None:
    stop = asyncio.Event()
    service = AsyncMock(spec=RuntimeDeliveryService)

    async def once(*args: object, **kwargs: object) -> DeliveryBatch:
        stop.set()
        return DeliveryBatch()

    service.run_once.side_effect = once
    await run_delivery(service, CLAIM, stop, interval_seconds=1, batch_size=8)
    service.run_once.assert_awaited_once_with(CLAIM, limit=8, should_stop=stop.is_set)
    service.run_once.reset_mock()
    await run_delivery(
        service, replace(CLAIM, holder_id="worker-stopped"), stop, interval_seconds=1, batch_size=8
    )
    service.run_once.assert_not_awaited()
