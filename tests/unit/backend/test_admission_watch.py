"""The reference deployment's admission poller must not claim a green it did not see.

`job-admission-watch` is the process that actually reads the admission report
(#845). Three of its behaviours are the reason it exists at all, and each is pinned
here against a real HTTP server rather than a stub:

* a degraded report is reported as degraded, with the causes a monitor alerts on;
* an unreadable report is *not* reported as admitting -- "the poller cannot see"
  must never look like "the deployment is fine";
* the healthcheck reads the poller's last observation and goes red when that
  observation is stale, so a dead loop cannot leave an old green check in place.

The poller has no database, no engine and no repair action: it must also be
impossible for it to restart a container or renew a receipt by accident, which the
compose guard in ``tests/ci`` asserts separately.
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from speechnexus_backend import admission_watch
from speechnexus_backend.admission_watch import (
    EXIT_DEGRADED,
    EXIT_INVALID,
    EXIT_UNREACHABLE,
    main,
    observe,
)

ADMITTING: dict[str, object] = {
    "status": "admitting",
    "causes": [],
    "workspace_id": "workspace-default",
    "checked_at": "2026-11-15T00:00:00.000000Z",
    "detail": None,
    "instances": [],
    "capacity": None,
}
DEGRADED: dict[str, object] = {
    **ADMITTING,
    "status": "degraded",
    "causes": ["engine_receipt_expired"],
    "detail": "1 of 1 ready engine instance(s) have no usable readiness receipt (expired).",
}


@contextmanager
def _serve(*, document: object, status: int = 200, raw: bytes | None = None) -> Iterator[str]:
    """One-endpoint server; records whether it was ever asked."""

    class Handler(BaseHTTPRequestHandler):
        served = 0

        def do_GET(self) -> None:
            Handler.served += 1
            body = raw if raw is not None else json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/nexus/jobs/admission-readiness"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _state(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


def _record(state_file: Path, state: str, *, causes: list[str] | None = None) -> None:
    state_file.write_text(
        json.dumps(
            {
                "state": state,
                "causes": causes or [],
                "detail": None,
                "url": "http://api:8000/v1/nexus/jobs/admission-readiness",
                "http_status": 200,
                "observed_at_us": admission_watch._now_us(),
            }
        ),
        encoding="utf-8",
    )


def test_an_admitting_report_is_observed_as_admitting() -> None:
    with _serve(document=ADMITTING) as url:
        observation = observe(url, timeout_seconds=5)

    assert observation["state"] == admission_watch.STATE_ADMITTING
    assert observation["causes"] == []
    assert observation["http_status"] == 200


def test_a_degraded_report_carries_the_machine_readable_causes() -> None:
    with _serve(document=DEGRADED) as url:
        observation = observe(url, timeout_seconds=5)

    assert observation["state"] == admission_watch.STATE_DEGRADED
    # The cause enum travels through untouched: a monitor alerting on
    # `engine_receipt_expired` reads it, it never parses the sentence.
    assert observation["causes"] == ["engine_receipt_expired"]
    assert observation["detail"] == DEGRADED["detail"]


def test_an_unreachable_report_is_not_reported_as_admitting() -> None:
    # A closed port stands in for a stopped API. "I cannot see" must surface as
    # unreachable, never as a healthy deployment.
    with _serve(document=ADMITTING) as url:
        pass
    observation = observe(url, timeout_seconds=2)

    assert observation["state"] == admission_watch.STATE_UNREACHABLE
    assert observation["http_status"] is None
    assert observation["causes"] == []


def test_an_api_without_job_admission_is_its_own_state() -> None:
    with _serve(document={"code": "job_unavailable"}, status=503) as url:
        observation = observe(url, timeout_seconds=5)

    assert observation["state"] == admission_watch.STATE_UNAVAILABLE
    assert observation["http_status"] == 503


def test_a_body_without_a_recognized_verdict_is_unreachable() -> None:
    # A proxy page or a truncated body is not an admission fact.
    with _serve(document=None, raw=b"<html>gateway</html>") as url:
        observation = observe(url, timeout_seconds=5)

    assert observation["state"] == admission_watch.STATE_UNREACHABLE


def test_a_degraded_report_exits_non_zero_from_a_single_poll(tmp_path: Path) -> None:
    state_file = _state(tmp_path)
    with _serve(document=DEGRADED) as url:
        output = io.StringIO()
        code = main(
            ["--once"],
            env={
                "SPEECHNEXUS_ADMISSION_WATCH_URL": url,
                "SPEECHNEXUS_ADMISSION_WATCH_STATE_FILE": str(state_file),
            },
            output=output,
        )

    assert code == EXIT_DEGRADED
    assert json.loads(output.getvalue().strip())["causes"] == ["engine_receipt_expired"]
    # `--once` reports and exits; it must not write a state file that pretends to
    # be a live observation.
    assert not state_file.exists()


def test_a_single_poll_of_a_dead_endpoint_exits_with_the_unreachable_code() -> None:
    with _serve(document=ADMITTING) as url:
        pass
    code = main(
        ["--once"],
        env={"SPEECHNEXUS_ADMISSION_WATCH_URL": url},
        output=io.StringIO(),
    )

    assert code == EXIT_UNREACHABLE


def test_the_healthcheck_is_green_only_for_a_recent_admitting_observation(
    tmp_path: Path,
) -> None:
    state_file = _state(tmp_path)
    _record(state_file, admission_watch.STATE_ADMITTING)

    assert admission_watch.healthcheck(state_file, interval_seconds=30) == 0


def test_the_healthcheck_reports_a_degraded_observation(tmp_path: Path) -> None:
    state_file = _state(tmp_path)
    _record(state_file, admission_watch.STATE_DEGRADED, causes=["capacity_exhausted"])

    assert admission_watch.healthcheck(state_file, interval_seconds=30) == 1


def test_the_healthcheck_goes_red_when_the_poller_stopped(tmp_path: Path) -> None:
    # The defect this surface exists to remove is a stale green. A loop that died
    # leaves its last admitting observation behind; the age check is what turns
    # that into a red health, and it is why the state file carries the timestamp.
    state_file = _state(tmp_path)
    document: dict[str, object] = {
        "state": admission_watch.STATE_ADMITTING,
        "causes": [],
        "detail": None,
        "url": "http://api:8000/v1/nexus/jobs/admission-readiness",
        "http_status": 200,
        "observed_at_us": admission_watch._now_us() - 600_000_000,
    }
    state_file.write_text(json.dumps(document), encoding="utf-8")

    assert admission_watch.healthcheck(state_file, interval_seconds=30) == 1


def test_the_healthcheck_reports_missing_state_as_unhealthy(tmp_path: Path) -> None:
    assert admission_watch.healthcheck(_state(tmp_path), interval_seconds=30) == 1


def test_invalid_settings_stop_the_poller_instead_of_polling_nothing() -> None:
    code = main(["--once"], env={"SPEECHNEXUS_ADMISSION_WATCH_URL": "api:8000/report"})

    assert code == EXIT_INVALID


def test_the_poller_keeps_polling_through_a_degraded_answer(tmp_path: Path) -> None:
    # One degrade must not stop the loop: the report is the only signal, and it has
    # to keep reporting until an operator renews the receipt.
    observations: list[dict[str, object]] = [
        {"state": admission_watch.STATE_ADMITTING, "causes": [], "observed_at_us": 1},
        {"state": admission_watch.STATE_DEGRADED, "causes": ["engine_receipt_expired"]},
        {"state": admission_watch.STATE_ADMITTING, "causes": []},
    ]
    seen: list[str] = []

    def fake_observe(url: str, *, timeout_seconds: float) -> dict[str, object]:
        seen.append(url)
        if len(seen) > len(observations):
            # The fourth poll is where the test stops the loop; three observations
            # have been published by then.
            raise KeyboardInterrupt
        observation: dict[str, object] = dict(observations[len(seen) - 1])
        observation.setdefault("observed_at_us", admission_watch._now_us())
        observation.setdefault("url", url)
        observation.setdefault("detail", None)
        observation.setdefault("http_status", 200)
        return observation

    state_file = _state(tmp_path)
    output = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        admission_watch.watch(
            "http://api:8000/v1/nexus/jobs/admission-readiness",
            interval_seconds=0.001,
            timeout_seconds=1,
            state_file=state_file,
            output=output,
            observe_once=fake_observe,
        )

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    lines = [line for line in records if line["event"] == admission_watch.OBSERVATION_EVENT]
    assert [line["state"] for line in lines] == [
        "admitting",
        "degraded",
        "admitting",
    ]
    # The transition out and back is logged as its own record, so a log-only
    # monitor sees the recovery without diffing observations.
    transitions = [line for line in records if line["event"] == admission_watch.TRANSITION_EVENT]
    assert [(line["previous_state"], line["state"]) for line in transitions] == [
        ("admitting", "degraded"),
        ("degraded", "admitting"),
    ]
    # The published state is the last observation, readable by the healthcheck.
    assert json.loads(state_file.read_text())["state"] == "admitting"


def test_the_state_file_is_replaced_atomically(tmp_path: Path) -> None:
    # A reader must never see half a document: the healthcheck would call that
    # malformed and lose the observation.
    state_file = _state(tmp_path)
    _record(state_file, admission_watch.STATE_ADMITTING)
    admission_watch._write_state(state_file, {"state": "degraded", "observed_at_us": 5})

    assert json.loads(state_file.read_text()) == {"state": "degraded", "observed_at_us": 5}
    assert not (tmp_path / "state.json.tmp").exists()


def test_the_loop_sleeps_between_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tight retry loop against a dead API is a self-inflicted outage; the
    # interval is a hard floor.
    sleeps: list[float] = []

    class _StopError(Exception):
        pass

    def fake_observe(url: str, *, timeout_seconds: float) -> dict[str, object]:
        if sleeps:
            raise _StopError
        return {
            "state": admission_watch.STATE_ADMITTING,
            "causes": [],
            "observed_at_us": admission_watch._now_us(),
            "url": url,
            "http_status": 200,
            "detail": None,
        }

    monkeypatch.setattr(admission_watch, "sleep", sleeps.append)

    with pytest.raises(_StopError):
        admission_watch.watch(
            "http://api:8000/v1/nexus/jobs/admission-readiness",
            interval_seconds=2.5,
            timeout_seconds=1,
            state_file=_state(tmp_path),
            output=io.StringIO(),
            observe_once=fake_observe,
        )

    assert sleeps == [2.5]
