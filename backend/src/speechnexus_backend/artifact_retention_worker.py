"""Standalone bounded Artifact retention worker with an explicit decision log.

Every pass writes one JSON line per decision plus a pass summary. A line carries
the Artifact identity, the recorded deadline and the database instant the
decision was taken at, so an operator can re-check afterwards that only rows
whose retention elapsed were touched. The durable evidence is the row itself:
``artifact_state`` moves and ``expires_at_us`` / ``created_at_us`` stay.

The entry point is ``speechnexus-artifact-retention``; ``--once`` runs one
bounded pass and exits, which is what the reference deployment's on-demand path
uses. Without ``--once`` the process repeats the same bounded pass on the
configured interval until it receives SIGTERM/SIGINT.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
from collections.abc import Callable
from threading import Event

from speechnexus_backend.artifact_retention_runtime import ArtifactRetentionRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.artifact_retention import (
    ArtifactRetention,
    RetentionOutcome,
)
from speechnexus_backend.speech.domain.artifacts import ArtifactError

_LOG = logging.getLogger(__name__)


class _StopRequested(BaseException):
    """Unwind short transactions/object calls without taking a lock in a handler."""


def run_retention(
    retention: ArtifactRetention,
    stop: Event,
    *,
    interval_seconds: float,
    once: bool = False,
    on_outcome: Callable[[RetentionOutcome], None] | None = None,
    on_error: Callable[[], None] | None = None,
) -> None:
    if not 0 < interval_seconds <= 3600:
        raise ValueError("Artifact retention must be bounded")
    while not stop.is_set():
        try:
            outcomes = retention.sweep()
        except ArtifactError:
            # Deliberately exclude SQL/SDK diagnostics and any object identity.
            if on_error is not None:
                on_error()
            else:
                _LOG.warning("Artifact retention unavailable; expired rows retained")
            if once:
                raise
            if stop.wait(interval_seconds):
                return
            continue
        for outcome in outcomes:
            if on_outcome is not None:
                on_outcome(outcome)
            else:
                _log_outcome(outcome)
        _log_pass(outcomes, batch_size=retention.batch_size)
        if once or stop.wait(interval_seconds):
            return


def _log_outcome(outcome: RetentionOutcome) -> None:
    """One reviewable line: which Artifact, which deadline, which instants."""
    _LOG.info(
        json.dumps(
            {
                "event": "artifact_retention",
                "artifact_id": outcome.artifact_id,
                "decision": outcome.decision,
                "expires_at_us": outcome.expires_at_us,
                "observed_at_us": outcome.observed_at_us,
                "reason": outcome.reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _log_pass(outcomes: tuple[RetentionOutcome, ...], *, batch_size: int) -> None:
    counts = {"expired": 0, "deleted": 0, "blocked": 0}
    for outcome in outcomes:
        counts[outcome.decision] += 1
    _LOG.info(
        json.dumps(
            {
                "event": "artifact_retention_pass",
                "batch_size": batch_size,
                "decisions": len(outcomes),
                **counts,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expire elapsed Artifacts and remove their content in bounded passes"
    )
    parser.add_argument("--once", action="store_true", help="Run one bounded pass, then exit")
    options = parser.parse_args()
    settings = Settings()
    if settings.process_role != "scheduler":
        parser.error("Set SPEECHNEXUS_PROCESS_ROLE=scheduler")
    if not {"node_id", "instance_id"} <= settings.model_fields_set:
        parser.error("Set explicit SPEECHNEXUS_NODE_ID and SPEECHNEXUS_INSTANCE_ID")
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop = Event()

    def request_stop(signum: int, frame: object) -> None:
        # Event.set() can deadlock when a signal interrupts Event.wait() while it
        # holds the same non-reentrant lock. Raising unwinds the owned contexts;
        # an interrupted transition keeps its durable identity.
        raise _StopRequested

    runtime = ArtifactRetentionRuntime(settings)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        runtime.open()
        if runtime.retention is None:
            raise ValueError("Artifact retention runtime did not open")
        run_retention(
            runtime.retention,
            stop,
            interval_seconds=settings.artifact_retention_interval_seconds,
            once=options.once,
        )
    except _StopRequested:
        pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        runtime.close()


if __name__ == "__main__":
    main()
