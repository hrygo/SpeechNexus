"""Read committed content through private ports; never fetch the original speech source."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

DownloadKind = Literal["saved", "export"]


class DownloadError(Exception):
    def __init__(self, code: str, *, size: int | None = None) -> None:
        self.code, self.size = code, size
        super().__init__(code)


@dataclass(frozen=True)
class DownloadContent:
    key: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            not self.key
            or self.size < 1
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.sha256) is None
        ):
            raise ValueError("invalid committed content")


class DownloadStore(Protocol):
    async def content(
        self, *, kind: DownloadKind, ident: str, workspace_id: str
    ) -> DownloadContent: ...


class VerifiedContent(Protocol):
    async def read(self, offset: int, size: int) -> bytes: ...
    async def close(self) -> None: ...


class DownloadReader(Protocol):
    async def prepare(self, content: DownloadContent) -> VerifiedContent: ...


def _decimal(value: str, ceiling: int) -> int:
    value = value.lstrip("0") or "0"
    limit = str(ceiling)
    # Never call int() on attacker-selected unbounded decimal strings.
    return (
        ceiling
        if len(value) > len(limit) or (len(value) == len(limit) and value > limit)
        else int(value)
    )


def select_range(header: str | None, size: int) -> tuple[int, int, int]:
    if size < 1:
        raise ValueError("empty committed content")
    if header is None:
        return 200, 0, size
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", header)
    if match is None or not any(match.groups()):
        raise DownloadError("invalid_request")
    first, last = match.groups()
    if not first:
        length = _decimal(last, size)
        if not length:
            raise DownloadError("range_not_satisfiable", size=size)
        return 206, size - length, length
    # Compare the complete decimals before clamping, so huge reversed ranges stay invalid.
    a, b = first.lstrip("0") or "0", last.lstrip("0") or "0"
    if last and (len(a) > len(b) or (len(a) == len(b) and a > b)):
        raise DownloadError("invalid_request")
    start = _decimal(first, size)
    if start >= size:
        raise DownloadError("range_not_satisfiable", size=size)
    end = size - 1 if not last else _decimal(last, size - 1)
    return 206, start, end - start + 1


@dataclass
class PreparedDownload:
    content: DownloadContent
    body: VerifiedContent
    status: int
    start: int
    length: int
    release: Callable[[], None]
    closed: bool = False

    async def close(self) -> None:
        if not self.closed:
            # Reader.close joins its worker before returning; retain the permit until then.
            try:
                await self.body.close()
            finally:
                self.closed = True
                self.release()


class WorkbenchDownloads:
    def __init__(
        self, store: DownloadStore, reader: DownloadReader, *, max_active: int = 2
    ) -> None:
        if type(max_active) is not int or max_active < 1:
            raise ValueError("invalid download capacity")
        self._store, self._reader, self._max_active = store, reader, max_active
        self._active = 0

    def _release(self) -> None:
        self._active -= 1

    async def prepare(
        self, *, kind: DownloadKind, ident: str, workspace_id: str, range_header: str | None
    ) -> PreparedDownload:
        # One event loop per serving instance; no await separates check and admission.
        # This is a local buffer bound, never a global coordination/inference budget.
        if self._active >= self._max_active:
            raise DownloadError("download_unavailable")
        self._active += 1
        body: VerifiedContent | None = None
        try:
            content = await self._store.content(kind=kind, ident=ident, workspace_id=workspace_id)
            status, start, length = select_range(range_header, content.size)
            body = await self._reader.prepare(content)
            # A deletion during object preparation must not reopen content at headers.
            current = await self._store.content(kind=kind, ident=ident, workspace_id=workspace_id)
            if current != content:
                raise DownloadError("download_unavailable")
            return PreparedDownload(content, body, status, start, length, self._release)
        except BaseException:
            try:
                if body is not None:
                    await body.close()
            finally:
                self._release()
            raise
