"""Publish one independently approved builtin voice with an explicit intent."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from speechnexus_backend.builtin_voice_publication import (
    BuiltinVoicePublicationRejectedError,
    publish_builtin_voice,
)
from speechnexus_backend.publish_binding import BindingPublishSettings
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--key-digest", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--installation-id", required=True)
    parser.add_argument(
        "--expected-head", required=True, help="Current revision, or 'none' if absent"
    )
    for name in ("artifact-pin", "manifest", "report", "scenario-evidence"):
        parser.add_argument("--" + name, required=True, type=Path)
    options = parser.parse_args(argv)
    try:
        documents = []
        for name in ("artifact_pin", "manifest", "report", "scenario_evidence"):
            with getattr(options, name).open("rb") as stream:
                documents.append(stream.read(1_048_577))
        if any(not 1 <= len(raw) <= 1_048_576 for raw in documents):
            raise ValueError
    except OSError, ValueError:
        parser.exit(2, "invalid release evidence files\n")

    try:
        settings = BindingPublishSettings()
    except ValidationError:
        parser.exit(2, "invalid publication writer configuration\n")
    try:
        pool = speech_pool(
            settings.publication_writer_database_url.get_secret_value(),
            schema=options.schema,
            max_size=1,
            name="publish-builtin-voice",
        )
    except ValueError:
        parser.exit(2, "invalid publication writer configuration\n")
    try:
        pool.open()
        result = publish_builtin_voice(
            PostgresPublications(pool, timeout_seconds=settings.transaction_timeout_seconds),
            key_digest=options.key_digest,
            approval_id=options.approval_id,
            database=options.database,
            schema=options.schema,
            installation_id=options.installation_id,
            expected_head=None if options.expected_head == "none" else options.expected_head,
            artifact_pin=documents[0],
            manifest=documents[1],
            report=documents[2],
            scenario_evidence=documents[3],
        )
    except BuiltinVoicePublicationRejectedError as error:
        parser.exit(4, f"publication rejected: {error}\n")
    except Exception:
        parser.exit(5, "publication unavailable; retry the identical original intent to recover\n")
    finally:
        pool.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
