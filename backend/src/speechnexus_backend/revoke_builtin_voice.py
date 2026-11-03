"""Withdraw one source or qualification approval, never stop an engine implicitly."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.builtin_voice_revocation import (
    BuiltinVoiceRevocationRejectedError,
    revoke_builtin_voice,
)
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications


class RevocationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_QUALIFICATION_WRITER_DATABASE_URL", min_length=1
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
    parser.add_argument("--kind", choices=("source", "qualification"), required=True)
    for name in ("approval-id", "reason", "database", "schema", "installation-id"):
        parser.add_argument("--" + name, required=True)
    options = parser.parse_args(argv)
    try:
        settings = RevocationSettings()
        pool = speech_pool(
            settings.database_url.get_secret_value(),
            schema=options.schema,
            max_size=1,
            name="revoke-builtin-voice",
        )
    except ValidationError, ValueError:
        parser.exit(2, "invalid qualification writer configuration\n")
    try:
        pool.open()
        result = revoke_builtin_voice(
            PostgresPublications(pool, timeout_seconds=settings.timeout_seconds),
            kind=options.kind,
            approval_id=options.approval_id,
            reason=options.reason,
            database=options.database,
            schema=options.schema,
            installation_id=options.installation_id,
        )
    except BuiltinVoiceRevocationRejectedError as error:
        parser.exit(4, f"revocation rejected: {error}\n")
    except PublicationUnavailableError:
        parser.exit(
            5,
            "revocation outcome unknown; verify the original installation, database and "
            "schema, then replay the same kind, approval ID and reason; "
            "do not assume existing execution stopped or capacity was released\n",
        )
    finally:
        pool.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
