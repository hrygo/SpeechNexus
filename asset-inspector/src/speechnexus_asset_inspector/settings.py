"""Deployment-owned inspection limits; every value is a hard server-side ceiling.

The service never accepts a client command, path, endpoint or environment. A
request may only tighten a budget; each header is clamped to the value parsed
here. The bearer token is stored as bytes with ``repr=False`` so an accidental
f-string or dataclass dump cannot print it.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

_ENV_PREFIX = "SPEECHNEXUS_INSPECTION_"


class ConfigurationError(ValueError):
    """Startup configuration is unusable; the message names variables only."""


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, default)
    if not value or len(value) > 255 or not value.isprintable():
        raise ConfigurationError(f"{name} must be a non-empty printable value")
    return value


def _integer(env: Mapping[str, str], name: str, default: int, *, low: int, high: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    if not raw.isascii() or not raw.isdecimal():
        raise ConfigurationError(f"{name} must be a decimal integer")
    value = int(raw)
    if not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low} and {high}")
    return value


def _seconds(
    env: Mapping[str, str], name: str, default: float, *, low: float, high: float
) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be a number of seconds") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low} and {high}")
    return value


def _token(env: Mapping[str, str], name: str) -> bytes:
    raw = env.get(name)
    if not raw or len(raw) > 4096:
        raise ConfigurationError(f"{name} must be a non-empty bearer token")
    # The value travels in an HTTP header; reject anything that cannot be
    # compared byte-for-byte with an ASCII header value.
    if not all("\x21" <= character <= "\x7e" for character in raw):
        raise ConfigurationError(f"{name} must contain ASCII graphic characters only")
    return raw.encode("ascii")


@dataclass(frozen=True, slots=True)
class InspectionSettings:
    """One deployment's fixed inspection ceilings and slot count."""

    token: bytes = field(repr=False)
    host: str = "0.0.0.0"
    port: int = 8080
    concurrency: int = 2
    max_encoded_bytes: int = 4 * 1024 * 1024
    max_duration_ms: int = 120_000
    max_wall_seconds: float = 20.0
    memory_bytes: int = 256 * 1024 * 1024
    cpu_seconds: int = 5
    output_bytes: int = 4096
    max_receipt_bytes: int = 4096

    def __post_init__(self) -> None:
        checks: tuple[tuple[object, int, int, str], ...] = (
            (self.port, 1, 65535, "port"),
            (self.concurrency, 1, 4, "concurrency"),
            (self.max_encoded_bytes, 1, 1024**3, "max encoded bytes"),
            (self.max_duration_ms, 1, 86_400_000, "max duration"),
            (self.memory_bytes, 64 * 1024**2, 1024**3, "memory limit"),
            (self.cpu_seconds, 1, 30, "CPU seconds"),
            (self.output_bytes, 1, 65536, "output bytes"),
            (self.max_receipt_bytes, 1, 65536, "max receipt bytes"),
        )
        for value, low, high, label in checks:
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid inspection {label}")
        if (
            type(self.max_wall_seconds) is not float
            or not math.isfinite(self.max_wall_seconds)
            or not 0 < self.max_wall_seconds <= 60
        ):
            raise ValueError("Invalid inspection wall deadline")
        if not isinstance(self.token, bytes) or not self.token:
            raise ValueError("Invalid inspection bearer token")
        if not self.host or not self.host.isprintable():
            raise ValueError("Invalid inspection host")

    @property
    def stdout_limit(self) -> int:
        """Largest child stdout the runner keeps, bounded by the receipt cap."""
        return min(self.output_bytes, self.max_receipt_bytes)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> InspectionSettings:
        source = os.environ if env is None else env
        token = _token(source, _ENV_PREFIX + "TOKEN")
        return cls(
            token=token,
            host=_text(source, _ENV_PREFIX + "HOST", "0.0.0.0"),
            port=_integer(source, _ENV_PREFIX + "PORT", 8080, low=1, high=65535),
            concurrency=_integer(source, _ENV_PREFIX + "CONCURRENCY", 2, low=1, high=4),
            max_encoded_bytes=_integer(
                source, _ENV_PREFIX + "MAX_ENCODED_BYTES", 4 * 1024 * 1024, low=1, high=1024**3
            ),
            max_duration_ms=_integer(
                source, _ENV_PREFIX + "MAX_DURATION_MS", 120_000, low=1, high=86_400_000
            ),
            max_wall_seconds=_seconds(
                source, _ENV_PREFIX + "MAX_WALL_SECONDS", 20.0, low=0.1, high=60.0
            ),
            memory_bytes=_integer(
                source,
                _ENV_PREFIX + "MEMORY_BYTES",
                256 * 1024 * 1024,
                low=64 * 1024**2,
                high=1024**3,
            ),
            cpu_seconds=_integer(source, _ENV_PREFIX + "CPU_SECONDS", 5, low=1, high=30),
            output_bytes=_integer(source, _ENV_PREFIX + "OUTPUT_BYTES", 4096, low=1, high=65536),
            max_receipt_bytes=_integer(
                source, _ENV_PREFIX + "MAX_RECEIPT_BYTES", 4096, low=1, high=65536
            ),
        )
