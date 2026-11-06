"""Append one voice tombstone through the shared publication writer.

The published revision stays immutable.  Repeating the identical retirement is
a replay of the stored tombstone; naming a superseded revision or a different
mode/reason for the same revision is a conflict instead of a second write.
"""

from __future__ import annotations

from typing import Any

from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.domain.voice_lifecycle import (
    VoiceRetirementCommand,
    VoiceRetirementConflictError,
    VoiceRetirementReceipt,
    VoiceRetirementUnavailableError,
    VoiceRevisionNotFoundError,
)
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

_NOT_CURRENT = "voice_revision_not_current"
_ALREADY_RETIRED = "voice_revision_already_retired"


class PostgresVoiceRetirements:
    """Retire a published voice revision using the lifecycle writer identity."""

    def __init__(self, publications: PostgresPublications) -> None:
        self._publications = publications

    def retire(self, command: VoiceRetirementCommand) -> VoiceRetirementReceipt:
        if command.mode == "disable":
            # The durable voice-disable intent and its target capture do not
            # exist, so this store refuses rather than reporting an unproven stop.
            raise VoiceRetirementConflictError("voice_disable_unsupported")
        revision = int(command.revision)
        try:
            publication = self._publications.get("voice", command.voice_id, revision)
            if publication is None:
                raise VoiceRevisionNotFoundError
            stored = self._publications.retirement("voice", command.voice_id, revision)
            if stored is not None and stored["revision_id"] == publication.revision_id:
                return self._settled(stored, command)
            retired_at_us = self._publications.clock_us()
            try:
                changed = self._publications.retire(
                    publication,
                    retired_at_us=retired_at_us,
                    mode=command.mode,
                    reason=command.reason,
                )
            except ValueError:
                stored = self._publications.retirement("voice", command.voice_id, revision)
                if stored is None or stored["revision_id"] != publication.revision_id:
                    raise VoiceRetirementConflictError(_NOT_CURRENT) from None
                return self._settled(stored, command)
            if changed:
                return VoiceRetirementReceipt(
                    voice_id=command.voice_id,
                    revision=command.revision,
                    revision_id=publication.revision_id,
                    mode=command.mode,
                    reason=command.reason,
                    retired_at_us=retired_at_us,
                    replayed=False,
                )
            stored = self._publications.retirement("voice", command.voice_id, revision)
            if stored is None:
                raise VoiceRetirementUnavailableError
            return self._settled(stored, command)
        except (
            VoiceRevisionNotFoundError,
            VoiceRetirementConflictError,
            VoiceRetirementUnavailableError,
        ):
            raise
        except PublicationUnavailableError:
            raise VoiceRetirementUnavailableError from None
        except ValueError, TypeError, KeyError:
            raise VoiceRetirementUnavailableError from None

    @staticmethod
    def _settled(stored: dict[str, Any], command: VoiceRetirementCommand) -> VoiceRetirementReceipt:
        if stored["mode"] != command.mode or stored["reason"] != command.reason:
            raise VoiceRetirementConflictError(_ALREADY_RETIRED)
        return VoiceRetirementReceipt(
            voice_id=command.voice_id,
            revision=command.revision,
            revision_id=str(stored["revision_id"]),
            mode=command.mode,
            reason=command.reason,
            retired_at_us=int(stored["retired_at_us"]),
            replayed=True,
        )
