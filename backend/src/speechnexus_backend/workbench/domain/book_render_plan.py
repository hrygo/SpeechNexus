"""A segmented book snapshot with local invalidation, not an export manifest or empty draft."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Self

from speechnexus_backend.workbench.domain.render_units import (
    RenderDependencies,
    RenderUnit,
    RevisionPin,
    render_cas,
    render_time,
)
from speechnexus_backend.workbench.domain.versions import validate_revision_number


@dataclass(frozen=True, slots=True)
class BookRenderPlan:
    book_id: str
    revision: int
    casting_revision: int
    units: tuple[RenderUnit, ...]

    def __post_init__(self) -> None:
        RevisionPin(self.book_id, self.revision)
        validate_revision_number(self.casting_revision)
        if type(self.units) is not tuple or not self.units:
            raise ValueError("render plan requires an immutable nonempty unit snapshot")
        ids: set[str] = set()
        positions: set[tuple[str, int]] = set()
        voices: dict[str, RevisionPin] = {}
        for unit in self.units:
            if type(unit) is not RenderUnit:
                raise ValueError("render plan requires render units")
            position = (unit.chapter_id, unit.ordinal)
            if (
                unit.book_id != self.book_id
                or unit.id in ids
                or position in positions
                or (unit.role_id in voices and voices[unit.role_id] != unit.dependencies.voice)
            ):
                raise ValueError("render plan has conflicting unit, chapter or role provenance")
            ids.add(unit.id)
            positions.add(position)
            voices[unit.role_id] = unit.dependencies.voice

    @property
    def stale_ids(self) -> tuple[str, ...]:
        return tuple(unit.id for unit in self.units if unit.status == "stale")

    def _check_edit(self, expected_revision: int, now: datetime) -> datetime:
        render_cas(expected_revision, self.revision)
        return render_time(now, max(unit.updated_at for unit in self.units))

    def revise_unit(
        self,
        unit_id: str,
        *,
        dependencies: RenderDependencies,
        expected_revision: int,
        now: datetime,
    ) -> Self:
        fixed_time = self._check_edit(expected_revision, now)
        if type(unit_id) is not str:
            raise ValueError("unknown render unit")
        for index, unit in enumerate(self.units):
            if unit.id != unit_id:
                continue
            updated = unit.revise(
                expected_revision=unit.revision, dependencies=dependencies, now=fixed_time
            )
            if updated is unit:
                return self
            return replace(
                self,
                revision=self.revision + 1,
                units=(*self.units[:index], updated, *self.units[index + 1 :]),
            )
        raise ValueError("unknown render unit")

    def recast(
        self,
        role_id: str,
        *,
        voice: RevisionPin,
        expected_revision: int,
        now: datetime,
    ) -> Self:
        fixed_time = self._check_edit(expected_revision, now)
        if type(voice) is not RevisionPin:
            raise ValueError("recast requires an immutable voice revision")
        if type(role_id) is not str or not any(unit.role_id == role_id for unit in self.units):
            raise ValueError("unknown render role")
        # The snapshot constructor guarantees one voice per role; compare before changing counters.
        if all(unit.dependencies.voice == voice for unit in self.units if unit.role_id == role_id):
            return self
        units = tuple(
            unit.revise(
                expected_revision=unit.revision,
                dependencies=replace(unit.dependencies, voice=voice),
                now=fixed_time,
            )
            if unit.role_id == role_id
            else unit
            for unit in self.units
        )
        return replace(
            self,
            revision=self.revision + 1,
            casting_revision=self.casting_revision + 1,
            units=units,
        )
