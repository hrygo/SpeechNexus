"""Readiness probe doubles that never contact real infrastructure."""

from __future__ import annotations


class StaticProbe:
    def __init__(self, name: str, *, failure: str | None = None) -> None:
        self._name = name
        self._failure = failure

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> None:
        if self._failure is not None:
            raise RuntimeError(self._failure)
