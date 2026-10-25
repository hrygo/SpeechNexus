"""The synthetic source server tolerates a client disconnect during close."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from tests.integration.test_workbench_transcript_import_postgres import PublicService


@pytest.mark.parametrize("disconnect", [BrokenPipeError, ConnectionResetError])
async def test_synthetic_source_close_disconnect_releases_its_task(
    disconnect: type[ConnectionError],
) -> None:
    state = PublicService()
    reader = asyncio.StreamReader()
    reader.feed_data(b"GET /v1/nexus/artifacts/artifact-source HTTP/1.1\r\n\r\n")
    reader.feed_eof()
    writer = Mock(spec=asyncio.StreamWriter)
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock(side_effect=disconnect("synthetic client disconnected"))
    await state.connection(reader, writer)
    assert not state.tasks and not state.errors
    writer.close.assert_called_once()
