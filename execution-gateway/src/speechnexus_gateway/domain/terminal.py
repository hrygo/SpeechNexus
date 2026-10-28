"""The engine's own terminal answers; a fact, never a stop or budget decision."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")
_MAX_TERMINAL_RESPONSES: Final = 4096


@dataclass(frozen=True, slots=True)
class EngineTerminalResponse:
    """The engine's own terminal answers for one admitted execution.

    Only the adapter that issued the request(s) may build this, and only after every
    request returned a validated terminal response with no request outstanding. It is
    not a node-controller instance fact, not a timeout, disconnect or cancel
    acknowledgment, and it covers exactly one execution identity.
    """

    submit_intent_id: str
    response_ids: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.submit_intent_id) is not str
            or _IDENTIFIER.fullmatch(self.submit_intent_id) is None
        ):
            raise ValueError("an engine terminal fact needs a recorded execution")
        if (
            type(self.response_ids) is not tuple
            or not 1 <= len(self.response_ids) <= _MAX_TERMINAL_RESPONSES
            or any(
                type(value) is not str or _IDENTIFIER.fullmatch(value) is None
                for value in self.response_ids
            )
            or len(set(self.response_ids)) != len(self.response_ids)
        ):
            raise ValueError(
                "an engine terminal fact requires distinct original request identities"
            )
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ValueError("an engine terminal fact needs a timezone")
