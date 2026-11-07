"""Record one measured-capability evidence row from a succeeded validation Job.

The operator supplies the committed report bytes. The recorder proves those bytes
are exactly the bytes the validation report Artifact commits, parses the measured
capabilities and coverage out of them, and appends one INSERT-only evidence row.
It never invents a capability and never rewrites an existing row; a corrected
measurement is a new validation Job and a new evidence row.

This command does not publish a voice.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceEvidenceConflictError,
    VoiceEvidenceNotEligibleError,
    VoiceEvidenceUnavailableError,
    VoiceReportUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres import (
    PostgresVoiceEvidenceRecorder,
)

_MAX_REPORT_BYTES = 1_048_576


class VoiceEvidenceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL", min_length=1
    )
    timeout_seconds: float = Field(
        default=3,
        validation_alias="SPEECHNEXUS_VOICE_EVIDENCE_TRANSACTION_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--validation-job-id", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--measured-by",
        default="record_voice_capability_evidence/v1",
        help="Operator or verifier label, 1..256 characters; never a credential",
    )
    options = parser.parse_args(argv)
    try:
        if not options.report.is_absolute():
            raise ValueError
        report = options.report.read_bytes()
        if not 1 <= len(report) <= _MAX_REPORT_BYTES:
            raise ValueError
    except OSError, ValueError:
        parser.exit(2, "invalid validation report file\n")
    try:
        settings = VoiceEvidenceSettings()
    except ValidationError:
        parser.exit(2, "invalid voice evidence recorder configuration\n")
    try:
        pool = speech_pool(
            settings.database_url.get_secret_value(),
            schema=options.schema,
            max_size=1,
            name="record-voice-capability-evidence",
        )
    except ValueError:
        parser.exit(2, "invalid voice evidence recorder configuration\n")
    try:
        pool.open()
        evidence, created = PostgresVoiceEvidenceRecorder(
            pool, timeout_seconds=settings.timeout_seconds
        ).record(options.validation_job_id, report, options.measured_by)
    except VoiceEvidenceNotEligibleError as error:
        parser.exit(4, f"validation run is not eligible: {error}\n")
    except VoiceEvidenceConflictError:
        parser.exit(6, "validation job already records different evidence\n")
    except VoiceEvidenceUnavailableError, VoiceReportUnavailableError:
        parser.exit(5, "evidence storage or committed report is unavailable\n")
    finally:
        pool.close()
    print(json.dumps({**evidence.as_row(), "created": created}, default=str))


if __name__ == "__main__":
    main()
