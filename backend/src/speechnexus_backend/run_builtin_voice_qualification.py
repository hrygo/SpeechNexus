"""Issue, admit, execute or recover one authorized builtin-voice qualification probe.

Issuance verifies fixed trusted deployment inputs. Execution preserves the original
identity; --approval-id records qualification only after result and stop/release
checks pass. This command does not publish a voice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.qualification_probe_approval import (
    _commit_verified_probe,
    find_probe_approval,
    result_request,
)
from speechnexus_backend.qualification_probe_artifacts import verify_local_tts_artifacts
from speechnexus_backend.qualification_probe_authorizer import (
    issue_probe_authorization,
    require_live_gateway_policy,
)
from speechnexus_backend.qualification_probe_execution import run_original_probe
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.admission import AdmitRecipe
from speechnexus_backend.speech.infrastructure.admission_codec import (
    business_command_from_value,
    prepare_admission,
    request_from_wire,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
    MinioExecutionResultObjects,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.qualification_admission_postgres import (
    PostgresQualificationAdmission,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    nonfinite,
    private_file,
    unique_pairs,
)
from speechnexus_backend.speech.infrastructure.transcription_submit import command_digest
from speechnexus_backend.speech.infrastructure.tts_recipe import load_tts_recipe

_MAX_BYTES = 1_048_576


class QualificationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_QUALIFICATION_ADMISSION_DATABASE_URL", min_length=1
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
    parser.add_argument("--schema", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--intent-file", required=True, type=Path)
    parser.add_argument("--recipe-file", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="run or recover the admitted probe")
    parser.add_argument("--issue", action="store_true", help="issue from trusted operator evidence")
    parser.add_argument("--approval-id", help="record the independently verified probe result")
    for argument in (
        "authorization-file",
        "template-file",
        "controller-inventory-file",
        "pin-file",
        "model-root",
        "manifest-file",
        "report-file",
        "scenario-file",
    ):
        parser.add_argument("--" + argument, type=Path)
    options = parser.parse_args(argv)
    try:
        settings = QualificationSettings()
        if not options.intent_file.is_absolute() or not options.recipe_file.is_absolute():
            raise ValueError
        intent_bytes = private_file(options.intent_file, _MAX_BYTES)
        recipe_bytes = private_file(options.recipe_file, _MAX_BYTES)
        document = json.loads(
            intent_bytes,
            object_pairs_hook=unique_pairs,
            parse_constant=nonfinite,
        )
        if not isinstance(document, dict) or set(document) != {"request", "business_command"}:
            raise ValueError
        business = business_command_from_value(
            document["business_command"], max_command_bytes=_MAX_BYTES
        )
        request = request_from_wire(document["request"], business)
        request_id = document["business_command"]["plan"]["request_id"]
        if not isinstance(request_id, str) or not request_id:
            raise ValueError
        recipe = load_tts_recipe(options.recipe_file)
        if private_file(options.recipe_file, _MAX_BYTES) != recipe_bytes:
            raise ValueError
        command = AdmitRecipe(request, business, recipe.approved)
        prepared = prepare_admission(command, max_command_bytes=_MAX_BYTES)
        original_identity = {
            "operation_id": request.identity.operation_id,
            "attempt_id": request.identity.attempt_id,
            "admission_request_digest": prepared.request_digest,
            "business_request_digest": business.request_digest,
            "gateway_request_digest": command_digest(
                document["business_command"]["gateway_request"]
            ),
            "intent_file_sha256": "sha256:" + hashlib.sha256(intent_bytes).hexdigest(),
            "recipe_file_sha256": "sha256:" + hashlib.sha256(recipe_bytes).hexdigest(),
        }
        if options.approval_id and not options.execute:
            raise ValueError
        if options.approval_id:
            from os import environ

            writer_url = environ.get("SPEECHNEXUS_QUALIFICATION_WRITER_DATABASE_URL")
            originals = (
                options.pin_file,
                options.manifest_file,
                options.report_file,
                options.scenario_file,
            )
            if not writer_url or any(path is None or not path.is_absolute() for path in originals):
                raise ValueError
            recovery = result_request(
                approval_id=options.approval_id,
                authorization_id=options.authorization_id,
                artifact_pin=private_file(options.pin_file, _MAX_BYTES),
                manifest=private_file(options.manifest_file, _MAX_BYTES),
                report=private_file(options.report_file, _MAX_BYTES),
                scenario_evidence=private_file(options.scenario_file, _MAX_BYTES),
                original_identity=original_identity,
            )
            recovery_pool = speech_pool(
                writer_url, schema=options.schema, max_size=1, name="qualify-builtin-writer"
            )
            try:
                recovery_pool.open()
                if find_probe_approval(
                    PostgresPublications(recovery_pool, timeout_seconds=settings.timeout_seconds),
                    recovery,
                ):
                    print(
                        json.dumps(
                            {
                                "outcome": "replayed",
                                "qualification": "approved",
                                "approval_id": options.approval_id,
                            }
                        )
                    )
                    return
            finally:
                recovery_pool.close()
        if options.issue:
            from os import environ

            writer_url = environ.get("SPEECHNEXUS_QUALIFICATION_WRITER_DATABASE_URL")
            paths = (
                options.authorization_file,
                options.template_file,
                options.controller_inventory_file,
                options.pin_file,
                options.model_root,
                options.manifest_file,
                options.report_file,
                options.scenario_file,
            )
            if not writer_url or any(path is None or not path.is_absolute() for path in paths):
                raise ValueError
            authorization = json.loads(
                private_file(options.authorization_file, _MAX_BYTES),
                object_pairs_hook=unique_pairs,
                parse_constant=nonfinite,
            )
            if authorization.get("authorization_id") != options.authorization_id:
                raise ValueError
            gateway_request = document["business_command"]["gateway_request"]
            if (
                authorization.get("admission_request_digest") != prepared.request_digest
                or authorization.get("business_request_digest") != business.request_digest
                or authorization.get("gateway_request_digest") != command_digest(gateway_request)
                or authorization.get("command_digest") != gateway_request["command_digest"]
                or authorization.get("operation_id") != request.identity.operation_id
                or authorization.get("attempt_id") != request.identity.attempt_id
            ):
                raise ValueError
            writer_pool = speech_pool(
                writer_url, schema=options.schema, max_size=1, name="qualify-builtin-writer"
            )
            try:
                writer_pool.open()
                issue_probe_authorization(
                    PostgresPublications(writer_pool, timeout_seconds=settings.timeout_seconds),
                    settings=Settings(),
                    intent=authorization,
                    template_document=private_file(options.template_file, _MAX_BYTES),
                    controller_inventory=private_file(
                        options.controller_inventory_file, _MAX_BYTES
                    ),
                    pin_path=options.pin_file,
                    model_root=options.model_root,
                    artifact_pin=private_file(options.pin_file, _MAX_BYTES),
                    manifest=private_file(options.manifest_file, _MAX_BYTES),
                    report=private_file(options.report_file, _MAX_BYTES),
                    scenario_evidence=private_file(options.scenario_file, _MAX_BYTES),
                )
            finally:
                writer_pool.close()
        pool = speech_pool(
            settings.database_url.get_secret_value(),
            schema=options.schema,
            max_size=1,
            name="qualify-builtin-admission",
        )
    except ValidationError, ValueError, TypeError, KeyError, OSError:
        parser.exit(2, "invalid private qualification input\n")
    except Exception:
        parser.exit(5, "authorization outcome unknown; replay the identical authorization\n")
    try:
        pool.open()
        port = PostgresQualificationAdmission(
            pool,
            authorization_id=options.authorization_id,
            max_command_bytes=_MAX_BYTES,
            timeout_seconds=settings.timeout_seconds,
            enforce_binding_disable=True,
        )
        result = port.admit_recipe(command)
        if result.outcome in ("committed", "replayed") and options.execute:
            runtime_settings = Settings()
            if (
                runtime_settings.object_storage_access_key is None
                or runtime_settings.object_storage_secret_key is None
                or runtime_settings.execution_gateway_base_url is None
                or runtime_settings.execution_gateway_component_credential is None
            ):
                raise ValueError("execution configuration unavailable")
            with pool.connection(timeout=settings.timeout_seconds) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                rows = connection.execute(
                    "SELECT intent FROM speech_builtin_voice_probe_authorizations "
                    "WHERE authorization_id=%s",
                    (options.authorization_id,),
                ).fetchall()
            if len(rows) != 1:
                raise ValueError("authorization unavailable")
            require_live_gateway_policy(runtime_settings, rows[0]["intent"])
            objects = MinioExecutionResultObjects(
                endpoint=str(runtime_settings.object_storage_endpoint),
                bucket=runtime_settings.object_storage_bucket,
                access_key=runtime_settings.object_storage_access_key.get_secret_value(),
                secret_key=runtime_settings.object_storage_secret_key.get_secret_value(),
                max_bytes=runtime_settings.speech_max_payload_bytes,
            )
            gateway = GatewayExecutionClient.from_settings(runtime_settings)
            probe = run_original_probe(
                pool=pool,
                port=port,
                recipe=recipe,
                request_id=request_id,
                gateway=gateway,
                objects=objects,
                timeout_seconds=runtime_settings.speech_timeout_seconds,
            )
            require_live_gateway_policy(runtime_settings, rows[0]["intent"])
            if options.approval_id:
                from os import environ

                writer_url = environ.get("SPEECHNEXUS_QUALIFICATION_WRITER_DATABASE_URL")
                approval_paths = (
                    options.pin_file,
                    options.model_root,
                    options.manifest_file,
                    options.report_file,
                    options.scenario_file,
                )
                if not writer_url or any(
                    path is None or not path.is_absolute() for path in approval_paths
                ):
                    raise ValueError
                pin_bytes = private_file(options.pin_file, _MAX_BYTES)
                artifacts = verify_local_tts_artifacts(
                    pin_path=options.pin_file,
                    model_root=options.model_root,
                    expected_pin_digest="sha256:" + hashlib.sha256(pin_bytes).hexdigest(),
                )
                writer_pool = speech_pool(
                    writer_url, schema=options.schema, max_size=1, name="qualify-builtin-writer"
                )
                try:
                    writer_pool.open()
                    _commit_verified_probe(
                        PostgresPublications(writer_pool, timeout_seconds=settings.timeout_seconds),
                        authorization_id=options.authorization_id,
                        approval_id=options.approval_id,
                        probe=probe,
                        artifacts=artifacts,
                        artifact_pin=pin_bytes,
                        manifest=private_file(options.manifest_file, _MAX_BYTES),
                        report=private_file(options.report_file, _MAX_BYTES),
                        scenario_evidence=private_file(options.scenario_file, _MAX_BYTES),
                        original_identity=original_identity,
                    )
                finally:
                    writer_pool.close()
    except Exception:
        parser.exit(
            5, "outcome unknown; query or replay the identical authorization and original intent\n"
        )
    finally:
        pool.close()
    if result.outcome not in ("committed", "replayed"):
        parser.exit(
            4 if result.outcome != "unknown" else 5,
            f"qualification admission {result.outcome}; recovery={result.recovery}\n",
        )
    print(
        json.dumps(
            {
                "outcome": result.outcome,
                "operation_id": result.identity.operation_id,
                "attempt_id": result.identity.attempt_id,
                "qualification": "approved" if options.approval_id else "pending",
            }
        )
    )


if __name__ == "__main__":
    main()
