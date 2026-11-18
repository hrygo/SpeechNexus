"""Admission transactions that also persist the synchronous ASR settlement root."""

from __future__ import annotations

import json
from typing import Any

from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_sql import (
    insert_asr_settlement,
)


class PostgresTranscriptionAdmissionTransactions(PostgresAdmissionTransactions):
    """Same transactional admission; the transcription root joins that transaction."""

    def _record_settlement(self, cursor: Any, prepared: Any) -> None:
        body = json.loads(prepared.command.business_command.document)
        if body.get("kind") == "asr_file":
            insert_asr_settlement(cursor, prepared.command.request)
            return
        super()._record_settlement(cursor, prepared)
