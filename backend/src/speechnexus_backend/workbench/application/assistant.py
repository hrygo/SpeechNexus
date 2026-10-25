"""Assistant turns over a narrow store port.

The text LLM is optional. A deployment without one refuses the turn here, before the
store opens a transaction, so a disabled assistant cannot leave accepted rows behind.
"""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.workbench.application.llm import DisabledTextLlm, LlmError, TextLlm
from speechnexus_backend.workbench.domain.assistant import AssistantTurn
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError


def _key(key: str | None) -> None:
    if key is None:
        return
    if not 8 <= len(key) <= 128:
        raise InvalidVersionChangeError("invalid workbench request key")


class AssistantTurnStore(Protocol):
    async def create_turn(
        self,
        *,
        workspace_id: str,
        project_id: str,
        input_text: str,
        voice_id: str,
        voice_revision: int,
        model: str | None,
        profile_id: str | None,
        profile_revision: int | None,
        key: str | None,
    ) -> AssistantTurn: ...

    async def get_turn(self, *, turn_id: str, workspace_id: str) -> AssistantTurn: ...

    async def cancel_current_turn(
        self, *, turn_id: str, workspace_id: str
    ) -> tuple[AssistantTurn, bool]:
        """Cancel at the revision the locked row really holds; False means no change."""
        ...


class WorkbenchAssistant:
    """`profile is None` means disabled; a configured binding is not a qualification claim."""

    def __init__(self, turns: AssistantTurnStore, llm: TextLlm | None = None) -> None:
        self._turns = turns
        self._llm: TextLlm = llm if llm is not None else DisabledTextLlm()

    @property
    def enabled(self) -> bool:
        return self._llm.profile is not None

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise LlmError("disabled")

    async def create_turn(
        self,
        *,
        workspace_id: str,
        project_id: str,
        input_text: str,
        voice_id: str,
        voice_revision: int,
        model: str | None = None,
        profile_id: str | None = None,
        profile_revision: int | None = None,
        key: str | None = None,
    ) -> AssistantTurn:
        _key(key)
        if (profile_id is None) != (profile_revision is None):
            raise InvalidVersionChangeError("incomplete assistant profile reference")
        self._require_enabled()
        return await self._turns.create_turn(
            workspace_id=workspace_id,
            project_id=project_id,
            input_text=input_text,
            voice_id=voice_id,
            voice_revision=voice_revision,
            model=model,
            profile_id=profile_id,
            profile_revision=profile_revision,
            key=key,
        )

    async def get_turn(self, *, turn_id: str, workspace_id: str) -> AssistantTurn:
        return await self._turns.get_turn(turn_id=turn_id, workspace_id=workspace_id)

    async def cancel_turn(self, *, turn_id: str, workspace_id: str) -> tuple[AssistantTurn, bool]:
        return await self._turns.cancel_current_turn(turn_id=turn_id, workspace_id=workspace_id)
