"""AssetRuntime shutdown and failed-startup release of the audio inspector.

Inspector *selection* is covered by ``test_asset_inspector_wiring``; this module
covers the lifetime the composition root owns: ``close()`` must close the
inspector before releasing the transports, the ``open()`` failure path must do
the same, and an unconfirmed stop must not read as a clean shutdown. Keeping the
module separate keeps the wiring fixtures focused on configuration validation.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import BinaryIO

import pytest

from speechnexus_backend import asset_runtime as asset_runtime_module
from speechnexus_backend.asset_runtime import AssetRuntime
from speechnexus_backend.speech.domain.assets import AssetPolicy, AssetPurpose, Inspection
from tests.unit.backend.test_asset_inspector_wiring import (
    URL,
    _audio_settings,
    _runtime_settings,
)

_NAMESPACE_DIGEST = "sha256:" + "0" * 64


class RecordingInspector:
    """Text-shaped inspector whose only real behaviour is a recorded stop."""

    def __init__(self, events: list[str], *, stop_confirmed: bool = True) -> None:
        self.events = events
        self.stop_confirmed = stop_confirmed
        self.close_calls = 0

    def inspect(self, purpose: AssetPurpose, body: BinaryIO, policy: AssetPolicy) -> Inspection:
        return Inspection(available=False)

    def close(self) -> bool:
        self.close_calls += 1
        self.events.append("inspector")
        return self.stop_confirmed


class RecordingPool:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = 0
        self.fail_open = False
        self.fail_connection = False

    def open(self) -> None:
        if self.fail_open:
            raise RuntimeError("pool startup failed")

    @contextmanager
    def connection(self) -> Iterator[object]:
        if self.fail_connection:
            raise RuntimeError("pool connection failed")
        yield object()

    def close(self) -> None:
        self.closed += 1
        self.events.append("pool")


class RecordingObjects:
    namespace_digest = _NAMESPACE_DIGEST

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
        self.events.append("objects")


def _injected_runtime(
    monkeypatch: pytest.MonkeyPatch,
    inspector: RecordingInspector,
    events: list[str],
) -> tuple[AssetRuntime, RecordingPool, RecordingObjects]:
    pool = RecordingPool(events)
    objects = RecordingObjects(events)
    monkeypatch.setattr(asset_runtime_module, "speech_pool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(asset_runtime_module, "MinioAssetObjects", lambda *args, **kwargs: objects)
    monkeypatch.setattr(
        asset_runtime_module,
        "_select_asset_inspector",
        lambda settings: (inspector, inspector.close),
    )
    return AssetRuntime(_runtime_settings()), pool, objects


def test_close_stops_the_inspector_before_releasing_the_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    inspector = RecordingInspector(events)
    runtime, pool, objects = _injected_runtime(monkeypatch, inspector, events)

    assert runtime.close() is True

    assert inspector.close_calls == 1
    assert (objects.closed, pool.closed) == (1, 1)
    assert events == ["inspector", "objects", "pool"]


@pytest.mark.parametrize("failure", ["pool_start", "pool_connection"])
def test_failed_open_closes_the_inspector_and_the_transports(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    events: list[str] = []
    inspector = RecordingInspector(events)
    runtime, pool, objects = _injected_runtime(monkeypatch, inspector, events)
    if failure == "pool_start":
        pool.fail_open = True
    else:
        pool.fail_connection = True

    with pytest.raises(RuntimeError, match="pool"):
        runtime.open()

    assert inspector.close_calls == 1
    assert (objects.closed, pool.closed) == (1, 1)
    assert events == ["inspector", "objects", "pool"]


def test_unconfirmed_inspector_stop_is_reported_and_never_reads_as_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[str] = []
    inspector = RecordingInspector(events, stop_confirmed=False)
    runtime, pool, objects = _injected_runtime(monkeypatch, inspector, events)

    with caplog.at_level(logging.WARNING, logger="speechnexus_backend.asset_runtime"):
        stopped = runtime.close()

    assert stopped is False
    assert "did not confirm it stopped" in caplog.text
    # The transports are still released; only the confirmation is missing.
    assert (objects.closed, pool.closed) == (1, 1)
    assert events == ["inspector", "objects", "pool"]


def test_text_inspector_owns_no_process_and_closes_cleanly() -> None:
    runtime = AssetRuntime(_runtime_settings())

    assert runtime.close() is True


class RecordingCloseableInspector:
    """HTTP-shaped inspector whose only real behaviour is a recorded close."""

    def __init__(self, url: str, token: object, **limits: object) -> None:
        self.url = url
        self.token = token
        self.limits = limits
        self.close_calls = 0

    def inspect(self, purpose: AssetPurpose, body: BinaryIO, policy: AssetPolicy) -> Inspection:
        return Inspection(available=False)

    def close(self) -> bool:
        self.close_calls += 1
        return True


def test_composition_root_hands_the_inspector_close_to_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = RecordingCloseableInspector(URL, None)
    events: list[str] = []
    pool = RecordingPool(events)
    objects = RecordingObjects(events)
    monkeypatch.setattr(
        asset_runtime_module,
        "HttpAudioAssetInspector",
        lambda *args, **kwargs: inspector,
    )
    monkeypatch.setattr(asset_runtime_module, "speech_pool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(asset_runtime_module, "MinioAssetObjects", lambda *args, **kwargs: objects)

    runtime = AssetRuntime(_audio_settings())

    assert runtime.close() is True
    assert inspector.close_calls == 1
    assert events == ["objects", "pool"]
