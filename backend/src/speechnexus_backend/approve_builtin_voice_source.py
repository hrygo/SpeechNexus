"""Record one source decision already made by an authorized independent operator."""

from __future__ import annotations

import argparse
import json
import logging
import re
import uuid
from collections.abc import Sequence
from datetime import datetime

from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.builtin_voice_source_approval import (
    BuiltinSourceApprovalError,
    approve_builtin_voice_source,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


class _PoolDiagnostics(logging.Filter):
    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Source approval database unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class SourceApprovalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL", min_length=1
    )
    timeout_seconds: float = Field(
        default=3,
        validation_alias="SPEECHNEXUS_PUBLICATION_TRANSACTION_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "database",
        "schema",
        "installation-id",
        "source-approval-id",
        "voice-id",
        "source-revision",
        "source-digest",
        "native-speaker-id",
        "expires-at",
    ):
        parser.add_argument("--" + name, required=True)
    options = parser.parse_args(argv)
    try:
        settings = SourceApprovalSettings()
        if _TIMESTAMP.fullmatch(options.expires_at) is None:
            raise ValueError
        expiry = datetime.fromisoformat(options.expires_at.replace("Z", "+00:00"))
        conninfo_to_dict(settings.database_url.get_secret_value())
        pool_name = "approve-builtin-source-" + uuid.uuid4().hex
        pool = speech_pool(
            settings.database_url.get_secret_value(),
            schema=options.schema,
            max_size=1,
            name=pool_name,
        )
    except Exception:
        parser.exit(2, "invalid source approval configuration or input\n")
    logger = logging.getLogger("psycopg.pool")
    diagnostics = _PoolDiagnostics(pool_name)
    logger.addFilter(diagnostics)
    failure: tuple[int, str] | None = None
    result: dict[str, str] | None = None
    try:
        pool.open()
        result = approve_builtin_voice_source(
            PostgresPublications(pool, timeout_seconds=settings.timeout_seconds),
            database=options.database,
            schema=options.schema,
            installation_id=options.installation_id,
            source_approval_id=options.source_approval_id,
            voice_id=options.voice_id,
            source_revision=options.source_revision,
            source_digest=options.source_digest,
            native_speaker_id=options.native_speaker_id,
            expires_at=expiry,
        )
    except BuiltinSourceApprovalError as error:
        failure = (4, f"source approval rejected: {error}\n")
    except Exception:
        failure = (
            5,
            "source approval outcome unknown; query or replay the same approval ID "
            "and all original inputs against the same target\n",
        )
    finally:
        closed = False
        try:
            pool.close()
            closed = True
        except Exception:
            failure = (
                5,
                "source approval outcome unknown; query or replay the same approval ID "
                "and all original inputs against the same target\n",
            )
        finally:
            # A failed close may leave pool workers alive and still logging.
            if closed:
                logger.removeFilter(diagnostics)
    if failure is not None:
        parser.exit(*failure)
    assert result is not None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
