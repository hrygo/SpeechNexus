"""In-process default registry; production writer lifecycle and HTTP are separate."""

import uuid
from collections.abc import Callable

from speechnexus_backend.speech.application.binding_documents import check_command
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.binding_defaults import (
    BindingDefaultsDraftResult,
    BindingDefaultsSnapshot,
)
from speechnexus_backend.speech.domain.binding_drafts import (
    BindingDraft,
    BindingDraftResult,
    BindingUnavailableError,
    CreateBindingDraft,
)
from speechnexus_backend.speech.domain.configuration import revision_number
from speechnexus_backend.speech.infrastructure.binding_defaults_sql import (
    create_default_draft,
    read_default_snapshot,
    recover_default_draft,
)
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import PostgresBindingDrafts
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class PostgresBindingDefaults:
    def __init__(
        self,
        pool: SpeechPool,
        *,
        timeout_seconds: float = 3,
        identity_factory: Callable[[], str] | None = None,
        snapshot_identity_factory: Callable[[], str] | None = None,
    ) -> None:
        self._driver = PostgresBindingDrafts(
            pool, timeout_seconds=timeout_seconds, identity_factory=identity_factory
        )
        self._snapshot_identity = snapshot_identity_factory or (
            lambda: "mbdefaults-" + uuid.uuid4().hex
        )

    def create(self, command: CreateBindingDraft) -> BindingDefaultsDraftResult:
        check_command(command)
        try:
            return self._driver._run(
                lambda cursor: create_default_draft(
                    cursor,
                    command,
                    cursor.clock,
                    self._driver._new_identity,
                    self._snapshot_identity,
                )
            )
        except BindingUnavailableError:
            return BindingDefaultsDraftResult(BindingDraftResult("unknown"))

    def recover(self, command: CreateBindingDraft) -> BindingDefaultsDraftResult:
        check_command(command)
        try:
            return self._driver._run(
                lambda cursor: recover_default_draft(cursor, command), read_only=True
            )
        except BindingUnavailableError:
            return BindingDefaultsDraftResult(BindingDraftResult("unknown"))

    def get(self, binding_id: str, revision: str) -> BindingDraft | None:
        return self._driver.get(binding_id, revision)

    def get_defaults(self, binding_id: str, revision: str) -> BindingDefaultsSnapshot | None:
        identifier(binding_id)
        revision_number(revision)
        return self._driver._run(
            lambda cursor: read_default_snapshot(cursor, binding_id, revision), read_only=True
        )
