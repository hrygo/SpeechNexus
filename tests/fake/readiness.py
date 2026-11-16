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


class DetailProbe:
    """A probe that also reports a bounded observation without failing readiness."""

    def __init__(self, name: str, *, detail: str | None = None) -> None:
        self._name, self._detail = name, detail

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> str | None:
        return self._detail
