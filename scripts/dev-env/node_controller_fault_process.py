"""Test-only crash barriers around production daemon/store calls.

This is a probe child, never a deployment entry point or production fault flag.
The parent observes the marker, SIGKILLs this process, and starts the ordinary
controller against the same store. The real factory, HTTP routes and lifespan
run unchanged. No request/response body or private log is printed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from threading import Event
from unittest.mock import patch

from speechnexus_node_controller.domain.intents import (
    RuntimeActionKind,
    RuntimeInstanceIntent,
    RuntimeIntentState,
)
from speechnexus_node_controller.infrastructure.docker_api import DockerApi
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore
from speechnexus_node_controller.main import main as controller_main

PHASES = ("before_stop", "after_exit", "before_remove", "after_remove", "before_receipt")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--marker", required=True, type=Path)
    args = parser.parse_args()

    def pause(phase: str) -> None:
        if phase == args.phase:
            args.marker.write_text(phase)
            Event().wait(60)
            raise RuntimeError("the probe parent did not terminate the paused controller")

    original_stop = DockerApi.stop_container
    original_wait = DockerApi.wait_container
    original_remove = DockerApi.remove_container
    original_save = SqliteIntentStore.save

    def stop(api: DockerApi, container_id: str, *, timeout_seconds: float) -> None:
        pause("before_stop")
        original_stop(api, container_id, timeout_seconds=timeout_seconds)

    def wait(api: DockerApi, container_id: str, *, timeout_seconds: float) -> int:
        result = original_wait(api, container_id, timeout_seconds=timeout_seconds)
        pause("after_exit")
        return result

    def remove(api: DockerApi, container_id: str) -> None:
        pause("before_remove")
        original_remove(api, container_id)
        pause("after_remove")

    def save(
        store: SqliteIntentStore, intent: RuntimeInstanceIntent, *, expected_version: int
    ) -> None:
        if (
            intent.action is RuntimeActionKind.STOP
            and intent.intent_state is RuntimeIntentState.SUCCEEDED
        ):
            pause("before_receipt")
        original_save(store, intent, expected_version=expected_version)

    with (
        patch.object(DockerApi, "stop_container", stop),
        patch.object(DockerApi, "wait_container", wait),
        patch.object(DockerApi, "remove_container", remove),
        patch.object(SqliteIntentStore, "save", save),
    ):
        controller_main()


if __name__ == "__main__":
    main()
