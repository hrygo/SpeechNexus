#!/usr/bin/env python3
"""Read-only association of one SDK result to its persisted execution."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import os
import stat
import wave
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row
from pydantic import SecretStr
from speechnexus_contracts.execution import parse
from speechnexus_contracts.execution_canonical import effective_execution_request

from speechnexus_backend.speech.application.transcription import AdmittedExecution
from speechnexus_backend.speech.domain.speech_synthesis import SpeechFormat, SpeechPlan
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
    MinioExecutionResultObjects,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    nonfinite,
    private_file,
    unique_pairs,
)
from speechnexus_backend.speech.infrastructure.transcription_submit import command_digest
from speechnexus_backend.speech.infrastructure.tts_admission import _identities
from speechnexus_backend.speech.infrastructure.tts_submit import build_tts_submit

ROOT: Path
PRIVATE: Path
FIELDS = {
    "operations": ("operation_id",),
    "attempts": ("operation_id", "attempt_id"),
    "leases": ("lease_id",),
    "submits": ("submit_intent_id",),
}


def read(path: Path) -> dict[str, Any]:
    result = json.loads(
        private_file(path, 1048576), object_pairs_hook=unique_pairs, parse_constant=nonfinite
    )
    if not isinstance(result, dict):
        raise ValueError("invalid private document")
    return result


def write(path: Path, value: object) -> None:
    data = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if private_file(path, 1048576) != data:
            raise ValueError("evidence conflict") from None
        return
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def connect(dsn: str, identity: dict[str, str]) -> Any:
    checked_sql_name(identity["schema"])
    conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute(
            "SELECT set_config('search_path',%s,true)", (identity["schema"] + ",pg_catalog",)
        )
        conn.execute("SET LOCAL statement_timeout='3s'")
        current = conn.execute(
            "SELECT current_database() AS database,current_schema() AS schema,"
            "installation_id::text AS installation_id FROM speech_publication_installation "
            "WHERE singleton IS TRUE"
        ).fetchone()
        if current != identity:
            raise ValueError("installation mismatch")
        return conn
    except BaseException:
        conn.close()
        raise


def snapshot(conn: Any) -> dict[str, Any]:
    queries = {
        "operations": (
            "SELECT operation_id FROM admission_operations ORDER BY operation_id LIMIT 1025"
        ),
        "attempts": (
            "SELECT operation_id,attempt_id FROM admission_attempts "
            "ORDER BY operation_id,attempt_id LIMIT 1025"
        ),
        "leases": "SELECT lease_id FROM admission_leases ORDER BY lease_id LIMIT 1025",
        "submits": (
            "SELECT submit_intent_id FROM submit_intents ORDER BY submit_intent_id LIMIT 1025"
        ),
    }
    result = {key: conn.execute(query).fetchall() for key, query in queries.items()}
    if any(len(rows) > 1024 for rows in result.values()):
        raise ValueError("snapshot too large")
    return result


def delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    if set(before) != set(FIELDS) or set(after) != set(FIELDS):
        raise ValueError("invalid snapshot")
    result = {}
    for key, fields in FIELDS.items():

        def project(rows: Any, fields: tuple[str, ...] = fields) -> set[tuple[str, ...]]:
            if not isinstance(rows, list) or len(rows) > 1024:
                raise ValueError("invalid snapshot rows")
            values = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(fields):
                    raise ValueError("invalid snapshot columns")
                value = tuple(row[field] for field in fields)
                if any(not isinstance(item, str) or not item for item in value):
                    raise ValueError("invalid identity")
                values.append(value)
            if len(set(values)) != len(values):
                raise ValueError("duplicate snapshot identity")
            return set(values)

        old = project(before[key])
        new = project(after[key])
        if not old <= new or len(new - old) != 1:
            raise ValueError("ambiguous execution delta")
        result[key] = next(iter(new - old))
    return result


def sdk_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tts_sdk_evidence", Path(__file__).with_name("verify-file-tts-sdk.py")
    )
    if spec is None or spec.loader is None:
        raise ValueError("SDK tool unavailable")
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    return sdk


def pcm(data: bytes) -> bytes:
    sdk_module().wav_facts(data)
    with wave.open(io.BytesIO(data), "rb") as source:
        if (
            source.getframerate(),
            source.getnchannels(),
            source.getsampwidth(),
            source.getcomptype(),
        ) != (24000, 1, 2, "NONE"):
            raise ValueError("invalid audio format")
        frames = source.getnframes()
        if not 0 < frames <= 240000:
            raise ValueError("invalid audio duration")
        result = source.readframes(frames)
        if len(result) != frames * 2:
            raise ValueError("truncated audio")
        return result


def validate_stop(stop: list[dict[str, Any]], dispatched: Any) -> None:
    if (
        len(stop) != 1
        or stop[0]["execution_stopped"] != 1
        or stop[0]["resources_released"] != 1
        or type(dispatched) is not int
        or type(stop[0]["observed_at_us"]) is not int
        or stop[0]["observed_at_us"] < dispatched
    ):
        raise ValueError("stop evidence unavailable")


def validate_submission(request: Mapping[str, Any], submission: dict[str, Any]) -> None:
    if any(
        submission[key] != request[key]
        for key in (
            "operation_id",
            "attempt_id",
            "lease_id",
            "submit_intent_id",
            "command_digest",
            "voice_revision_id",
        )
    ) or request["command_digest"] != command_digest(request["command"]):
        raise ValueError("submit mismatch")


def validate_sdk_command(
    started: dict[str, Any], plan_value: dict[str, Any], request: Mapping[str, Any]
) -> None:
    sdk = sdk_module()
    body = {
        "model": started["binding_id"],
        "input": sdk.TEXT,
        "voice": started["voice_id"],
        "response_format": "wav",
        "speed": 1.0,
        "nexus": {
            "workspace_id": started["workspace_id"],
            "voice_revision": started["voice_revision"],
            "client_reference": started["client_reference"],
            "overrides": {"tts": {"language": "en"}},
        },
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if started["request_sha256"] != "sha256:" + hashlib.sha256(encoded).hexdigest():
        raise ValueError("SDK request digest mismatch")
    values = dict(plan_value)
    values["response_format"] = SpeechFormat(values["response_format"])
    plan = SpeechPlan(**values)
    if (
        plan.text != sdk.TEXT
        or plan.model_id != started["binding_id"]
        or plan.voice_id != started["voice_id"]
        or str(plan.voice_revision) != started["voice_revision"]
        or plan.workspace_id != started["workspace_id"]
        or plan.speed != 1.0
        or plan.language != "en"
        or plan.response_format != SpeechFormat.WAV
        or plan.instructions != ""
        or plan.seed is not None
    ):
        raise ValueError("SDK request and plan mismatch")
    deadline = datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        raise ValueError("missing deadline timezone")
    delta = deadline.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    deadline_us = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    keys = (
        "submit_intent_id",
        "lease_id",
        "operation_id",
        "attempt_id",
        "stage_id",
        "workspace_id",
        "pool_id",
        "host_node_id",
        "gateway_id",
        "instance_id",
        "instance_generation",
        "binding_revision_id",
        "policy_revision_id",
        "fencing_token",
    )
    admitted = cast(Any, AdmittedExecution)(*(request[key] for key in keys), deadline_us)
    if build_tts_submit(plan, admitted) != request:
        raise ValueError("gateway command differs from original plan")


async def gateway(request: Any, context: dict[str, Any]) -> tuple[bytes, str]:
    port = read(ROOT / f"gateway-port-{request['instance_generation']}.json")["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid gateway port")
    client = GatewayExecutionClient(
        base_url=f"http://localhost:{port}",
        credential=SecretStr(context["gateway_component_token"]),
        timeout_seconds=5,
        max_retries=0,
        backoff_seconds=0,
    )
    try:
        status = await client.query(request["submit_intent_id"])
        keys = (
            "submit_intent_id",
            "lease_id",
            "operation_id",
            "attempt_id",
            "stage_id",
            "workspace_id",
            "pool_id",
            "host_node_id",
            "gateway_id",
            "instance_id",
            "instance_generation",
            "binding_revision_id",
            "policy_revision_id",
            "voice_revision_id",
            "fencing_token",
            "command_digest",
        )
        if status["execution_state"] != "succeeded" or any(
            status.get(k) != request[k] for k in keys
        ):
            raise ValueError("gateway execution mismatch")
        receipt = await client.audio_staging_result(request["submit_intent_id"])
        staging = receipt["staging_result"]
        if staging is None:
            raise ValueError("missing staging result")
        digest = command_digest(effective_execution_request(request))
        if (
            receipt["submit_intent_id"] != request["submit_intent_id"]
            or receipt["request_digest"] != digest
            or staging["request_digest"] != digest
            or staging["reference"]["workspace_id"] != request["workspace_id"]
        ):
            raise ValueError("gateway result mismatch")
        objects = MinioExecutionResultObjects(
            endpoint=context["object_storage_endpoint"],
            bucket=context["object_storage_bucket"],
            access_key=context["object_access_key"],
            secret_key=context["object_secret_key"],
            max_bytes=1048576,
        )
        try:
            payload = await asyncio.to_thread(
                objects.read, staging["reference"], staging["storage_version"]
            )
        finally:
            objects.close()
        return payload, digest
    finally:
        await client.aclose()


def main() -> None:
    global ROOT, PRIVATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--task-root", type=Path, required=True)
    args = parser.parse_args()
    stage = "configuration"
    try:
        ROOT = args.task_root
        info = ROOT.lstat()
        if (
            not ROOT.is_absolute()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("invalid task root")
        PRIVATE = ROOT / "private"
        info = PRIVATE.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("invalid private directory")
        identity = read(PRIVATE / "installation.json")
        if set(identity) != {"database", "schema", "installation_id"} or any(
            not isinstance(value, str) or not value for value in identity.values()
        ):
            raise ValueError("unexpected database")
        observer = read(PRIVATE / "connections.json")["owner_dsn"]
        ids = read(PRIVATE / "profile-identities.json")
        binding = read(PRIVATE / "binding-created.json")
        readiness_raw = private_file(PRIVATE / "public-phase-readiness.json", 1048576)
        readiness = json.loads(
            readiness_raw, object_pairs_hook=unique_pairs, parse_constant=nonfinite
        )
        readiness_digest = "sha256:" + hashlib.sha256(readiness_raw).hexdigest()
        if (
            set(readiness)
            != {"instance_id", "instance_generation", "verified_at_us", "expires_at_us"}
            or readiness["instance_id"] != ids["instance_id"]
            or type(readiness["instance_generation"]) is not int
            or readiness["instance_generation"] < 1
            or type(readiness["verified_at_us"]) is not int
            or type(readiness["expires_at_us"]) is not int
            or not 0 < readiness["verified_at_us"] < readiness["expires_at_us"]
        ):
            raise ValueError("invalid original readiness")
        stage = "database"
        with connect(observer, identity) as conn:
            current = snapshot(conn)
            if args.phase == "before":
                write(
                    PRIVATE / "sdk-association-before.json",
                    {
                        "identity": identity,
                        "snapshot": current,
                        "readiness": readiness,
                        "readiness_digest": readiness_digest,
                    },
                )
                print('{"phase":"before","outcome":"saved"}')
                return
            before = read(PRIVATE / "sdk-association-before.json")
            if (
                before["identity"] != identity
                or before["readiness"] != readiness
                or before["readiness_digest"] != readiness_digest
            ):
                raise ValueError("installation changed")
            changes = delta(before["snapshot"], current)
            operation = changes["operations"][0]
            attempt_operation, attempt = changes["attempts"]
            lease, submit = changes["leases"][0], changes["submits"][0]
            row = conn.execute(
                "SELECT operation_id,initial_attempt_id,request_digest,request_document "
                "FROM admission_operations WHERE operation_id=%s",
                (operation,),
            ).fetchone()
            allocation = conn.execute(
                "SELECT * FROM admission_leases WHERE lease_id=%s", (lease,)
            ).fetchone()
            submission = conn.execute(
                "SELECT * FROM submit_intents WHERE submit_intent_id=%s", (submit,)
            ).fetchone()
            stop = conn.execute(
                "SELECT evidence_kind,evidence_digest,observed_at_us,execution_stopped,"
                "resources_released FROM execution_stop_evidence WHERE submit_intent_id=%s "
                "ORDER BY evidence_id LIMIT 2",
                (submit,),
            ).fetchall()
            if (
                not row
                or not allocation
                or not submission
                or attempt_operation != operation
                or row["initial_attempt_id"] != attempt
            ):
                raise ValueError("missing linked execution")
            raw = row["request_document"]
            if "sha256:" + hashlib.sha256(raw.encode()).hexdigest() != row["request_digest"]:
                raise ValueError("invalid business digest")
            business = json.loads(raw)
            request = parse("SubmitExecutionRequest", business["gateway_request"])
            plan = business["plan"]
            started = read(ROOT / "sdk-evidence" / "started.json")
            expected_identity = _identities(plan["request_id"])[0]
            if (expected_identity.operation_id, expected_identity.attempt_id) != (
                operation,
                attempt,
            ):
                raise ValueError("operation identity mismatch")
            if (
                business["kind"] != "tts_file"
                or plan["model_id"] != binding["binding_id"]
                or plan["voice_id"] != started["voice_id"]
                or plan["voice_revision_id"] != ids["voice_revision_id"]
                or str(plan["voice_revision"]) != started["voice_revision"]
                or plan["workspace_id"] != started["workspace_id"]
                or plan["model_id"] != started["binding_id"]
                or request["binding_revision_id"] != ids["binding_revision_id"]
                or request["voice_revision_id"] != ids["voice_revision_id"]
                or request["instance_id"] != ids["instance_id"]
                or request["instance_generation"] != readiness["instance_generation"]
            ):
                raise ValueError("execution facts mismatch")
            if (
                request["operation_id"],
                request["attempt_id"],
                request["lease_id"],
                request["submit_intent_id"],
            ) != (operation, attempt, lease, submit):
                raise ValueError("execution identity mismatch")
            if any(
                allocation[k] != request[k]
                for k in (
                    "operation_id",
                    "attempt_id",
                    "lease_id",
                    "stage_id",
                    "instance_id",
                    "instance_generation",
                    "gateway_id",
                    "pool_id",
                    "host_node_id",
                    "fencing_token",
                )
            ):
                raise ValueError("lease mismatch")
            validate_submission(request, submission)
            validate_sdk_command(started, plan, request)
            stage = "sdk_evidence"
            result = read(ROOT / "sdk-evidence" / "result.json")
            audio = private_file(ROOT / "sdk-evidence" / "audio.wav", 1048576)
            if (
                result["outcome"] != "success"
                or result["status"] != 200
                or result["sha256"] != "sha256:" + hashlib.sha256(audio).hexdigest()
            ):
                raise ValueError("SDK evidence mismatch")
            stage = "gateway"
            payload, digest = asyncio.run(gateway(request, read(PRIVATE / "api-context.json")))
            if pcm(audio) != pcm(payload):
                raise ValueError("SDK and staged PCM mismatch")
            if submission["request_digest"] != digest:
                raise ValueError("stored request digest mismatch")
            validate_stop(stop, submission["dispatched_at_us"])
            output = {
                "identity": identity,
                "operation_id": operation,
                "attempt_id": attempt,
                "lease_id": lease,
                "submit_intent_id": submit,
                "command_digest": request["command_digest"],
                "business_digest": row["request_digest"],
                "gateway_request_digest": digest,
                "dispatched_at_us": submission["dispatched_at_us"],
                "sdk_audio_sha256": result["sha256"],
                "pcm_sha256": "sha256:" + hashlib.sha256(pcm(audio)).hexdigest(),
                "stop_evidence": stop[0],
            }
        stage = "output"
        write(PRIVATE / "sdk-association-after.json", output)
        print('{"phase":"after","outcome":"verified"}')
    except Exception:
        parser.exit(2, f"association unavailable: stage={stage} type=validation_or_io\n")


if __name__ == "__main__":
    main()
