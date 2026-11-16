"""Record one operator-attested builtin voice qualification for an external engine.

The deployment must declare trust explicitly: without a valid trust declaration
this command refuses and writes nothing, so an absent node-controller never turns
into an implicit downgrade. Everything it records is a declaration -- it never
produces native runtime evidence, stop evidence, budget release or lifecycle
convergence. It does not publish a voice; run ``speechnexus-publish-builtin-voice``
with the recorded approval afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.operator_attested_qualification import (
    parse_operator_documents,
    record_operator_attestation,
)
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.operator_attestation import (
    AttestationRequest,
    OperatorAttestationError,
    TrustDeclaration,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.transcription_recipe import private_file

_MAX_BYTES = 1_048_576


class OperatorAttestationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL", min_length=1
    )
    timeout_seconds: float = Field(
        default=3,
        validation_alias="SPEECHNEXUS_PUBLICATION_TRANSACTION_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )


class _PoolDiagnostics(logging.Filter):
    """Never leak a connection string through pool diagnostics."""

    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Operator attestation database unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def _gateway_evidence() -> dict[str, object]:
    """Read the authenticated live gateway TTS mapping; never a typed claim."""

    async def read() -> dict[str, object]:
        gateway = GatewayExecutionClient.from_settings(Settings())
        try:
            return dict(await gateway.query_tts_configuration_evidence())
        finally:
            await gateway.aclose()

    return asyncio.run(read())


def _document(path: Path) -> bytes:
    return private_file(path, _MAX_BYTES)


@dataclass(frozen=True, slots=True)
class PreparedAttestation:
    """The three declared originals after parsing; no storage has been touched."""

    declaration: TrustDeclaration
    request: AttestationRequest
    release: dict[str, bytes]


def load_trust_declaration(path: Path | None) -> bytes:
    """A deployment that never declared trust cannot reach the writer at all."""
    if path is None:
        raise OperatorAttestationError("trust_declaration_missing")
    try:
        return _document(path)
    except OSError, ValueError:
        raise OperatorAttestationError("trust_declaration_missing") from None


def prepare_attestation(
    *,
    trust_declaration_file: Path | None,
    attestation_request_file: Path,
    artifact_pin: Path,
    manifest: Path,
    report: Path,
    scenario_evidence: Path,
) -> PreparedAttestation:
    """Read and parse every declared original before any transaction starts."""
    for path in (
        attestation_request_file,
        artifact_pin,
        manifest,
        report,
        scenario_evidence,
    ):
        if not path.is_absolute():
            raise OperatorAttestationError("attestation_request_invalid")
    declaration_document = load_trust_declaration(trust_declaration_file)
    request_document = _document(attestation_request_file)
    try:
        declaration, request = parse_operator_documents(
            declaration_document=declaration_document,
            request_document=request_document,
        )
    except json.JSONDecodeError:
        raise OperatorAttestationError("attestation_request_invalid") from None
    return PreparedAttestation(
        declaration=declaration,
        request=request,
        release={
            "artifact_pin": _document(artifact_pin),
            "manifest": _document(manifest),
            "report": _document(report),
            "scenario_evidence": _document(scenario_evidence),
        },
    )


_REMEDIATION = {
    # Expiring an attestation does not free its voice revision: the unique
    # identity is (voice, revision, revision_id, binding revision) and it is what
    # stops a second declaration from being read as a replacement for the first.
    "attestation_revision_already_attested": (
        "; this voice revision already has an attestation and one revision carries "
        "exactly one; publish a new voice revision and attest that instead"
    ),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--attestation-request-file", required=True, type=Path)
    parser.add_argument(
        "--trust-declaration-file",
        type=Path,
        help="The deployment's explicit external-engine trust decision; required",
    )
    for name in ("artifact-pin", "manifest", "report", "scenario-evidence"):
        parser.add_argument("--" + name, required=True, type=Path)
    options = parser.parse_args(argv)
    if not os.environ.get("SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL", "").strip():
        # Fail closed and name the input: an absent writer DSN is how a deployment
        # says "do not declare qualifications", never "trust the operator instead
        # of the controller".
        print(
            "error: SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL is not set",
            file=sys.stderr,
        )
        return 2
    try:
        settings = OperatorAttestationSettings()
    except ValueError, TypeError, KeyError, OverflowError:
        print("error: operator attestation settings are invalid", file=sys.stderr)
        return 2
    try:
        conninfo_to_dict(settings.database_url.get_secret_value())
    except psycopg.ProgrammingError, psycopg.OperationalError:
        # A DSN that libpq cannot parse is a configuration error, not a result: it
        # must name the key and exit 2 before any connection is attempted.
        print(
            "error: SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL is not a valid "
            "connection string",
            file=sys.stderr,
        )
        return 2
    try:
        prepared = prepare_attestation(
            trust_declaration_file=options.trust_declaration_file,
            attestation_request_file=options.attestation_request_file,
            artifact_pin=options.artifact_pin,
            manifest=options.manifest,
            report=options.report,
            scenario_evidence=options.scenario_evidence,
        )
        declaration, request = prepared.declaration, prepared.request
        release = prepared.release
    except OperatorAttestationError as error:
        if error.code in (
            "trust_declaration_missing",
            "trust_declaration_format_invalid",
            "trust_declaration_gaps_unacknowledged",
            "trust_declaration_target_mismatch",
            "trust_declaration_instance_not_trusted",
        ):
            print(f"error: {error.code}", file=sys.stderr)
            return 4
        parser.exit(2, f"invalid attestation input: {error.code}\n")
    except OSError, ValueError, TypeError, KeyError, OverflowError:
        parser.exit(2, "invalid attestation configuration or input\n")
    try:
        gateway = _gateway_evidence()
    except Exception:
        print("error: gateway_policy_evidence_unavailable", file=sys.stderr)
        return 4
    pool_name = "operator-attestation-" + uuid.uuid4().hex
    try:
        pool = speech_pool(
            settings.database_url.get_secret_value(),
            schema=options.schema,
            max_size=1,
            name=pool_name,
        )
        pool.open()
    except Exception:
        # Nothing was written: an unreachable writer is a deployment fact, not an
        # unknown outcome, and must not be reported as one.
        print(
            "error: operator attestation writer is unreachable; check "
            "SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL",
            file=sys.stderr,
        )
        return 3
    logger = logging.getLogger("psycopg.pool")
    diagnostics = _PoolDiagnostics(pool_name)
    logger.addFilter(diagnostics)
    outcome: dict[str, object] | None = None
    failure: tuple[int, str] | None = None
    try:
        outcome = record_operator_attestation(
            PostgresPublications(pool, timeout_seconds=settings.timeout_seconds),
            declaration=declaration,
            request=request,
            gateway_evidence=gateway,
            workspace_id=options.workspace_id,
            **release,
        )
    except OperatorAttestationError as error:
        failure = (4, f"attestation rejected: {error.code}{_REMEDIATION.get(error.code, '')}\n")
    except Exception:
        failure = (
            5,
            "attestation outcome unknown; query or replay the identical attestation "
            "with the same originals against the same target\n",
        )
    finally:
        closed = False
        try:
            pool.close()
            closed = True
        except Exception:
            failure = (
                5,
                "attestation outcome unknown; query or replay the identical attestation "
                "with the same originals against the same target\n",
            )
        finally:
            if closed:
                logger.removeFilter(diagnostics)
    if failure is not None:
        print(failure[1], end="", file=sys.stderr)
        return failure[0]
    assert outcome is not None
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
