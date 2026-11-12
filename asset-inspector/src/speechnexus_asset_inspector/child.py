"""One decode child per request: read a private descriptor and print one receipt.

The service process never imports this module, so it never loads PyAV: a codec
crash cannot take the service down. The child reads only the inherited input
descriptor, writes one bounded JSON object to stdout, and keeps stderr empty.
The parent supplies every limit through ``prlimit`` before this code starts, and
passes a minimal environment without the inspection bearer token.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
from collections.abc import Mapping

from speechnexus_media.domain import (
    MEDIA_RECIPE_VERSION,
    AudioDecodeLimits,
    AudioSourceFormat,
    MediaError,
)
from speechnexus_media.inspection import inspect_audio

_SCHEMA_VERSION = 1


def _emit(payload: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    sys.stdout.flush()


def _failure_status(code: str) -> str:
    # Only the two permanent content verdicts cross the process boundary. A
    # cancellation or timeline fault is not a public status; it stays invalid.
    if code == "media_limit_exceeded":
        return "media_limit_exceeded"
    return "invalid_media"


def _limits() -> dict[str, int]:
    def value(identifier: int) -> int:
        soft, _hard = resource.getrlimit(identifier)
        return -1 if soft == resource.RLIM_INFINITY else int(soft)

    return {
        "as": value(resource.RLIMIT_AS),
        "cpu": value(resource.RLIMIT_CPU),
        "fsize": value(resource.RLIMIT_FSIZE),
        "nofile": value(resource.RLIMIT_NOFILE),
        "core": value(resource.RLIMIT_CORE),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input-fd", type=int)
    parser.add_argument("--max-encoded-bytes", type=int)
    parser.add_argument("--max-duration-ms", type=int)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--report-limits", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.report_limits:
        _emit({"limits": _limits()})
        return 0
    if (
        args.input_fd is None
        or args.max_encoded_bytes is None
        or args.max_duration_ms is None
        or args.timeout_seconds is None
        or args.input_fd < 0
        or args.max_encoded_bytes < 1
        or args.max_duration_ms < 1
        or not 0 < args.timeout_seconds <= 60
    ):
        _emit({"schema_version": _SCHEMA_VERSION, "status": "unavailable"})
        return 2
    try:
        # Set single-threaded BLAS before PyAV/NumPy load; the sandbox itself
        # still prohibits additional processes and unbounded address space.
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        os.environ["OMP_NUM_THREADS"] = "1"
        import av

        av.logging.set_level(None)
        with os.fdopen(args.input_fd, "rb", closefd=False) as source:
            report = inspect_audio(
                source,
                source_format=AudioSourceFormat("audio/wav"),
                limits=AudioDecodeLimits(
                    max_encoded_bytes=args.max_encoded_bytes,
                    max_duration_ms=args.max_duration_ms,
                    timeout_seconds=args.timeout_seconds,
                ),
                cancelled=lambda: False,
            )
    except MediaError as error:
        # No vendor message, private path or input byte enters the receipt.
        _emit({"schema_version": _SCHEMA_VERSION, "status": _failure_status(error.code)})
        return 1
    except BaseException:
        _emit({"schema_version": _SCHEMA_VERSION, "status": "unavailable"})
        return 2
    if report.get("recipe_version") != MEDIA_RECIPE_VERSION:
        _emit({"schema_version": _SCHEMA_VERSION, "status": "unavailable"})
        return 2
    _emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
