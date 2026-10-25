"""Await one cancellable I/O operation, then join its scoped cleanup."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


async def run_bounded_io[Result](
    operation: Callable[[], Coroutine[Any, Any, Result]],
    *,
    deadline: float,
    check: Callable[[], None],
) -> Result:
    check()

    async def watch() -> None:
        while True:
            check()
            await asyncio.sleep(0.01)

    pending = asyncio.create_task(operation())
    guard = asyncio.create_task(watch())
    try:
        async with asyncio.timeout_at(deadline):
            done, _ = await asyncio.wait({pending, guard}, return_when=asyncio.FIRST_COMPLETED)
            if guard in done:
                guard.result()
            result = pending.result()
            check()
            return result
    finally:
        pending.cancel()
        guard.cancel()
        cleanup = asyncio.gather(pending, guard, return_exceptions=True)
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError
