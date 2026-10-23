"""Maintenance runs bounded passes and stops without inventing release evidence."""

from __future__ import annotations

from threading import Event
from typing import cast
from unittest.mock import Mock

import pytest

from speechnexus_backend.asset_worker import run_maintenance
from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.domain.assets import AssetError


def test_once_is_one_bounded_pass_and_already_stopped_does_not_run() -> None:
    service = Mock(spec=AssetService)
    stop = Event()
    run_maintenance(cast(AssetService, service), stop, interval_seconds=1, batch_size=3, once=True)
    service.maintain.assert_called_once_with(3)
    stop.set()
    run_maintenance(cast(AssetService, service), stop, interval_seconds=1, batch_size=3)
    assert service.maintain.call_count == 1


def test_stop_after_a_completed_pass_starts_no_other_work() -> None:
    service = Mock(spec=AssetService)
    stop = Event()
    service.maintain.side_effect = lambda size: stop.set()
    run_maintenance(cast(AssetService, service), stop, interval_seconds=1, batch_size=3)
    service.maintain.assert_called_once_with(3)


def test_unavailable_authority_retains_originals_and_resumes_only_on_next_pass() -> None:
    service = Mock(spec=AssetService)
    service.maintain.side_effect = AssetError("asset_metadata_unknown")
    stop = Event()
    failed = Mock(side_effect=stop.set)
    run_maintenance(
        cast(AssetService, service), stop, interval_seconds=1, batch_size=3, on_error=failed
    )
    failed.assert_called_once()
    service.maintain.assert_called_once_with(3)
    with pytest.raises(AssetError, match="asset_metadata_unknown"):
        run_maintenance(
            cast(AssetService, service), Event(), interval_seconds=1, batch_size=3, once=True
        )


@pytest.mark.parametrize("interval,batch", [(0, 1), (301, 1), (1, 0), (1, 101)])
def test_maintenance_limits_are_explicit(interval: float, batch: int) -> None:
    with pytest.raises(ValueError, match="bounded"):
        run_maintenance(
            cast(AssetService, Mock()), Event(), interval_seconds=interval, batch_size=batch
        )
