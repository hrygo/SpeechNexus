"""Poll the public Job admission report so a degraded deployment is observed.

An elapsed engine readiness receipt refuses every public Job create with
``503 engine_not_ready`` while ``/readyz``, the container healthcheck and
``docker ps`` all stay green (#845). The report at
``GET /v1/nexus/jobs/admission-readiness`` is the surface that answers the missing
question; this process is the reference deployment's poller, so a deployment
started from ``deploy/compose.yaml`` has something actually reading it instead of a
runbook instruction to build a timer.

It is a report consumer only. It never renews a receipt, never touches the
database, never calls an engine and never changes admission: re-signing is
``speechnexus-register-engine --renew-readiness``, and this process only names it.

Two properties matter as much as the polling:

* it never asks Docker to restart anything. The container runs this loop with
  ``restart: "no"``, and no other service waits on its health, because restarting
  cannot renew a receipt -- a restart loop would only hide the degradation;
* it distinguishes "degraded" from "this process is not observing". Every
  observation is written to a state file whose ``observed_at_us`` the healthcheck
  re-reads, so a dead loop, an unreachable API and a degraded deployment are three
  different answers instead of one green check.

Standard library only: the healthcheck runs this same entry point inside the same
container, and a probe must not need the application's dependency tree to report
the truth.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://api:8000/v1/nexus/jobs/admission-readiness"
DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_STATE_FILE = "/run/admission-watch/state.json"

EXIT_ADMITTING = 0
EXIT_DEGRADED = 1
EXIT_UNREACHABLE = 2
EXIT_UNAVAILABLE = 3
EXIT_INVALID = 4

# One indirection so the interval is observable from a test without patching the
# `time` module: a tight retry loop against a dead API is a self-inflicted outage.
sleep: Callable[[float], None] = time.sleep

OBSERVATION_EVENT = "job_admission_observation"
TRANSITION_EVENT = "job_admission_transition"

# The states a poll can end in. ``degraded`` is the report telling us admission is
# refused; ``unreachable`` is us failing to read the report; ``unavailable`` is the
# API answering that it has no Job admission assembled at all.
STATE_ADMITTING = "admitting"
STATE_DEGRADED = "degraded"
STATE_UNREACHABLE = "unreachable"
STATE_UNAVAILABLE = "unavailable"

_EXIT_BY_STATE = {
    STATE_ADMITTING: EXIT_ADMITTING,
    STATE_DEGRADED: EXIT_DEGRADED,
    STATE_UNREACHABLE: EXIT_UNREACHABLE,
    STATE_UNAVAILABLE: EXIT_UNAVAILABLE,
}

DEGRADED_STATUS = "degraded"
ADMITTING_STATUS = "admitting"


def _settings(env: Mapping[str, str]) -> tuple[str, float, float, Path]:
    url = env.get("SPEECHNEXUS_ADMISSION_WATCH_URL", DEFAULT_URL).strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("SPEECHNEXUS_ADMISSION_WATCH_URL must be an http(s) URL")
    raw_interval = env.get(
        "SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS", str(int(DEFAULT_INTERVAL_SECONDS))
    ).strip()
    interval = float(raw_interval)
    if not 1 <= interval <= 3600:
        raise ValueError("SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS must be 1..3600")
    raw_timeout = env.get(
        "SPEECHNEXUS_ADMISSION_WATCH_TIMEOUT_SECONDS", str(int(DEFAULT_TIMEOUT_SECONDS))
    ).strip()
    timeout = float(raw_timeout)
    if not 0.1 <= timeout <= 60:
        raise ValueError("SPEECHNEXUS_ADMISSION_WATCH_TIMEOUT_SECONDS must be 0.1..60")
    state_file = Path(env.get("SPEECHNEXUS_ADMISSION_WATCH_STATE_FILE", DEFAULT_STATE_FILE).strip())
    if not state_file.is_absolute():
        raise ValueError("SPEECHNEXUS_ADMISSION_WATCH_STATE_FILE must be an absolute path")
    return url, interval, timeout, state_file


def _now_us() -> int:
    return time.time_ns() // 1_000


def observe(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    """Read the report once and classify what was observed.

    The report always answers ``200`` with the verdict in the body, so a non-200 is
    a fact about the surface, not about admission: ``503`` means this deployment has
    no Job admission assembled, and anything else is an unexpected answer that must
    not be reported as healthy.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status_code = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        return _observation(
            STATE_UNAVAILABLE,
            url=url,
            http_status=error.code,
            causes=[],
            detail=f"the API answered HTTP {error.code}; it does not assemble Job admission",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return _observation(
            STATE_UNREACHABLE,
            url=url,
            http_status=None,
            causes=[],
            detail=f"the report could not be read ({type(error).__name__})",
        )
    try:
        document = json.loads(raw)
    except ValueError:
        return _observation(
            STATE_UNREACHABLE,
            url=url,
            http_status=status_code,
            causes=[],
            detail="the report was not JSON",
        )
    if not isinstance(document, dict):
        return _observation(
            STATE_UNREACHABLE,
            url=url,
            http_status=status_code,
            causes=[],
            detail="the report was not a JSON object",
        )
    status = document.get("status")
    causes = document.get("causes")
    detail = document.get("detail")
    if status == ADMITTING_STATUS:
        return _observation(
            STATE_ADMITTING, url=url, http_status=status_code, causes=[], detail=detail
        )
    if status == DEGRADED_STATUS and isinstance(causes, list):
        return _observation(
            STATE_DEGRADED,
            url=url,
            http_status=status_code,
            causes=[cause for cause in causes if isinstance(cause, str)],
            detail=detail,
        )
    # An unreadable verdict is not an admission fact: report that this process is
    # not observing instead of inventing a healthy deployment.
    return _observation(
        STATE_UNREACHABLE,
        url=url,
        http_status=status_code,
        causes=[],
        detail="the report carried no recognized status/causes pair",
    )


def _observation(
    state: str,
    *,
    url: str,
    http_status: int | None,
    causes: Sequence[str],
    detail: object,
) -> dict[str, Any]:
    return {
        "state": state,
        "url": url,
        "http_status": http_status,
        "causes": sorted(causes),
        "detail": detail if isinstance(detail, str) else None,
        "observed_at_us": _now_us(),
    }


def _write_state(path: Path, observation: Mapping[str, Any]) -> None:
    """Publish the observation atomically; the healthcheck reads this file.

    A partial write would be read as a malformed observation, so the file is
    replaced whole. A state file that cannot be written is fatal: the healthcheck
    could only ever see stale data, and a stale green is the defect this exists to
    remove.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(dict(observation), sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return document if isinstance(document, dict) else None


def _line(observation: Mapping[str, Any], *, event: str, **extra: object) -> str:
    return json.dumps({"event": event, **observation, **extra}, sort_keys=True)


def healthcheck(path: Path, *, interval_seconds: float) -> int:
    """Answer whether the last observation was admitting and recent.

    Docker restarts nothing because of this answer: the container's restart policy
    is ``no`` and no service waits on this health. The point is that ``docker ps``
    states the degradation, and that a stopped loop reads unhealthy instead of
    leaving the last green observation standing.
    """
    document = _read_state(path)
    if document is None:
        print(json.dumps({"health": "unhealthy", "reason": "no readable observation"}))
        return 1
    observed_at_us = document.get("observed_at_us")
    if type(observed_at_us) is not int:
        print(json.dumps({"health": "unhealthy", "reason": "observation has no timestamp"}))
        return 1
    # Two intervals of slack: one slow poll must not flap, a dead loop must show.
    stale_after_seconds = max(2 * interval_seconds, 5)
    age_seconds = (_now_us() - observed_at_us) / 1_000_000
    state = document.get("state")
    report = {
        "health": "healthy" if state == STATE_ADMITTING else "unhealthy",
        "state": state,
        "causes": document.get("causes", []),
        "detail": document.get("detail"),
        "age_seconds": round(age_seconds, 3),
        "url": document.get("url"),
    }
    if age_seconds > stale_after_seconds:
        report["health"] = "unhealthy"
        report["reason"] = f"the last observation is older than {stale_after_seconds:.0f}s"
    elif state != STATE_ADMITTING:
        report["reason"] = f"admission state is {state}"
    print(json.dumps(report, sort_keys=True))
    return 0 if report["health"] == "healthy" else 1


def watch(
    url: str,
    *,
    interval_seconds: float,
    timeout_seconds: float,
    state_file: Path,
    output: Any,
    observe_once: Callable[..., dict[str, Any]] = observe,
) -> int:
    """Poll forever, printing one JSON line per observation plus each transition.

    The loop is the delivery mechanism, so it does not stop on a degraded or
    unreachable answer: the point is to keep reporting until an operator fixes the
    receipt. It returns only when it is terminated.
    """
    previous: str | None = None
    while True:
        observation = observe_once(url, timeout_seconds=timeout_seconds)
        _write_state(state_file, observation)
        if previous is not None and observation["state"] != previous:
            print(
                _line(observation, event=TRANSITION_EVENT, previous_state=previous),
                file=output,
                flush=True,
            )
        print(_line(observation, event=OBSERVATION_EVENT), file=output, flush=True)
        previous = str(observation["state"])
        sleep(interval_seconds)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    output: Any = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Poll GET /v1/nexus/jobs/admission-readiness and report whether a public "
            "Job create would be admitted"
        )
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Answer for a container healthcheck from the last observation, then exit",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Read the report once, print it, and exit (0 admitting, 1 degraded, "
        "2 unreachable, 3 no Job admission assembled)",
    )
    options = parser.parse_args(argv)
    source = os.environ if env is None else env
    stream = sys.stdout if output is None else output
    try:
        url, interval, timeout, state_file = _settings(source)
    except (TypeError, ValueError) as invalid:
        print(f"error: invalid_settings: {invalid}", file=sys.stderr)
        return EXIT_INVALID
    if options.healthcheck:
        return healthcheck(state_file, interval_seconds=interval)
    if options.once:
        observation = observe(url, timeout_seconds=timeout)
        print(_line(observation, event=OBSERVATION_EVENT), file=stream, flush=True)
        return _EXIT_BY_STATE.get(str(observation["state"]), EXIT_UNREACHABLE)
    # Compose stops containers with SIGTERM; treat it as a clean stop so the log
    # ends with a reason instead of a traceback.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(EXIT_ADMITTING))
    print(
        json.dumps(
            {
                "event": "job_admission_watch_started",
                "url": url,
                "interval_seconds": interval,
                "timeout_seconds": timeout,
                "state_file": str(state_file),
            },
            sort_keys=True,
        ),
        file=stream,
        flush=True,
    )
    try:
        return watch(
            url,
            interval_seconds=interval,
            timeout_seconds=timeout,
            state_file=state_file,
            output=stream,
        )
    except KeyboardInterrupt:
        return EXIT_ADMITTING


if __name__ == "__main__":
    sys.exit(main())
