"""Explicit operator command that promotes one stored binding revision.

This is the supported publication path until the frozen
``publishModelBindingRevision`` endpoint has an evidence store. It never
creates, edits or activates a revision and it exposes no HTTP surface.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from collections.abc import Sequence

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.speech.application.binding_publication import (
    BindingPublicationConflictError,
    BindingPublicationResult,
    BindingPublisher,
    BindingRevisionNotFoundError,
)
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingCapacityManifestMismatchError,
    BindingEvidenceConflictError,
)
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications

EXIT_NOT_STORED = 3
EXIT_CONFLICT = 4
EXIT_UNAVAILABLE = 5
EXIT_EVIDENCE_MISMATCH = 6
_REVISION = re.compile(r"[1-9][0-9]{0,18}\Z")


class BindingPublishSettings(BaseSettings):
    """Only the publication writer identity and its schema are required."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
        frozen=True,
    )

    publication_writer_database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", min_length=1
    )
    database_schema: str = Field(
        default="speech",
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA",
        pattern=r"^[a-z][a-z0-9_]{2,62}$",
    )
    transaction_timeout_seconds: float = Field(
        default=3,
        validation_alias="SPEECHNEXUS_PUBLICATION_TRANSACTION_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )


def _revision(value: str) -> int:
    if _REVISION.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "revision must be a positive decimal without leading zeros"
        )
    return int(value)


def publish_binding(
    *,
    database_url: str,
    schema: str,
    binding_id: str,
    revision: int,
    revision_id: str,
    capacity_manifest_id: str,
    qualification_report_id: str,
    capacity_manifest_digest: str | None = None,
    qualification_report_digest: str | None = None,
    transaction_timeout_seconds: float = 3,
) -> BindingPublicationResult:
    """Validate the revision and evidence, then publish both in one transaction.

    Importable so deployment code and tests share the exact same operation; the
    command line only parses configuration around this function.
    """
    pool = speech_pool(database_url, schema=schema, max_size=1, name="publish-binding")
    pool.open()
    try:
        publisher = BindingPublisher(
            PostgresPublications(pool, timeout_seconds=transaction_timeout_seconds),
            now_us=lambda: time.time_ns() // 1_000,
        )
        return publisher.publish(
            binding_id,
            revision,
            revision_id,
            capacity_manifest_id=capacity_manifest_id,
            qualification_report_id=qualification_report_id,
            capacity_manifest_digest=capacity_manifest_digest,
            qualification_report_digest=qualification_report_digest,
        )
    finally:
        pool.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Publish one existing binding revision through the append-only "
            "speech_publications registry"
        )
    )
    parser.add_argument("--binding-id", required=True, help="Existing binding family identity")
    parser.add_argument("--revision", required=True, type=_revision, help="Stored revision number")
    parser.add_argument("--revision-id", required=True, help="Stored immutable revision identity")
    parser.add_argument(
        "--capacity-manifest-id",
        required=True,
        help="Capacity manifest recorded as publication evidence; must match the revision",
    )
    parser.add_argument(
        "--qualification-report-id",
        required=True,
        help="Qualification report recorded as publication evidence",
    )
    parser.add_argument(
        "--capacity-manifest-digest",
        default=None,
        help="Optional sha256 digest fixing the capacity manifest content",
    )
    parser.add_argument(
        "--qualification-report-digest",
        default=None,
        help="Optional sha256 digest fixing the qualification report content",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="Override SPEECHNEXUS_DATABASE_SCHEMA for this invocation",
    )
    options = parser.parse_args(argv)
    configured = os.environ.get("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", "").strip()
    if not configured:
        parser.error(
            "Set SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL to the provisioned "
            "publication writer DSN"
        )
    try:
        settings = BindingPublishSettings()
        result = publish_binding(
            database_url=settings.publication_writer_database_url.get_secret_value(),
            schema=options.schema or settings.database_schema,
            binding_id=options.binding_id,
            revision=options.revision,
            revision_id=options.revision_id,
            capacity_manifest_id=options.capacity_manifest_id,
            qualification_report_id=options.qualification_report_id,
            capacity_manifest_digest=options.capacity_manifest_digest,
            qualification_report_digest=options.qualification_report_digest,
            transaction_timeout_seconds=settings.transaction_timeout_seconds,
        )
    except BindingRevisionNotFoundError as error:
        parser.exit(EXIT_NOT_STORED, f"not published: {error}\n")
    except BindingPublicationConflictError as error:
        parser.exit(EXIT_CONFLICT, f"conflict: {error}\n")
    except BindingEvidenceConflictError as error:
        parser.exit(EXIT_CONFLICT, f"evidence conflict: {error}\n")
    except BindingCapacityManifestMismatchError as error:
        parser.exit(EXIT_EVIDENCE_MISMATCH, f"evidence mismatch: {error}\n")
    except PublicationUnavailableError:
        parser.exit(
            EXIT_UNAVAILABLE,
            "publication unavailable: the write may or may not have committed; "
            "re-run the same binding/revision/revision_id to resolve it\n",
        )
    except ValidationError as error:
        parser.error(f"invalid configuration: {error.error_count()} field(s) failed validation")
    except (ValueError, TypeError) as error:
        parser.exit(2, f"invalid request: {error}\n")
    publication, evidence = result.publication, result.evidence
    print(
        f"published binding_id={publication.object_id} revision={publication.revision} "
        f"revision_id={publication.revision_id} published_at_us={publication.published_at_us} "
        f"capacity_manifest_id={evidence.capacity_manifest_id} "
        f"qualification_report_id={evidence.qualification_report_id} "
        f"created={str(result.created).lower()}"
    )


if __name__ == "__main__":
    main()
