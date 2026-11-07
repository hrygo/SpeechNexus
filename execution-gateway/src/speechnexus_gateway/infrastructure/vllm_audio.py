"""Shared vLLM-Omni speech response fact: one bounded plain RIFF/WAVE body.

Both native speech adapters normalize exactly one WAV response; keeping the rule
here prevents a second, subtly different copy in each adapter. Language values
live in :mod:`speechnexus_gateway.domain.languages`.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

_MAX_WAV_CHUNKS = 32


def wav_pcm(
    payload: bytes,
    *,
    sample_rate_hz: int,
    max_pcm_bytes: int,
    on_invalid: Callable[[int], Exception],
    on_limit: Callable[[int], Exception],
) -> bytes:
    """Extract mono PCM16LE from a plain RIFF/WAVE body, rejecting anything else.

    ``on_invalid``/``on_limit`` receive the received body length so each adapter
    keeps its own failure domain without duplicating the structural checks.
    """

    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise on_invalid(len(payload))
    declared = struct.unpack_from("<I", payload, 4)[0]
    end_limit = declared + 8
    if end_limit < 44 or end_limit > len(payload):
        raise on_invalid(len(payload))
    offset = 12
    chunks = 0
    format_seen = False
    data: bytes | None = None
    while offset < end_limit:
        if offset + 8 > end_limit:
            raise on_invalid(len(payload))
        chunk_id = payload[offset : offset + 4]
        size = struct.unpack_from("<I", payload, offset + 4)[0]
        body = offset + 8
        end = body + size
        if end > end_limit:
            raise on_invalid(len(payload))
        chunks += 1
        if chunks > _MAX_WAV_CHUNKS:
            raise on_invalid(len(payload))
        if chunk_id == b"fmt ":
            if format_seen or size < 16:
                raise on_invalid(len(payload))
            format_seen = True
            fields = struct.unpack_from("<HHIIHH", payload, body)
            if fields != (1, 1, sample_rate_hz, sample_rate_hz * 2, 2, 16):
                raise on_invalid(len(payload))
        elif chunk_id == b"data":
            if data is not None or size == 0 or size % 2:
                raise on_invalid(len(payload))
            data = payload[body:end]
        offset = end + (size & 1)
    if not format_seen or data is None or offset != end_limit:
        raise on_invalid(len(payload))
    if len(data) > max_pcm_bytes:
        raise on_limit(len(payload))
    return data
