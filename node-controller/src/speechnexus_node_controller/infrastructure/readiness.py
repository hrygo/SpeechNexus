"""Readiness probes for the node controller's own node-local dependencies."""

from __future__ import annotations

import asyncio

from speechnexus_node_controller.infrastructure.docker_api import DockerEngineApi
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore


class DockerDaemonReadinessProbe:
    """Verify that the local container daemon answers before serving intents."""

    def __init__(self, api: DockerEngineApi) -> None:
        self._api = api

    @property
    def name(self) -> str:
        return "container_daemon"

    async def check(self) -> None:
        await asyncio.to_thread(self._api.ping)


class IntentStoreReadinessProbe:
    """Verify that the durable intent store accepts a read."""

    def __init__(self, store: SqliteIntentStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "runtime_intent_store"

    async def check(self) -> None:
        await asyncio.to_thread(self._store.probe)
