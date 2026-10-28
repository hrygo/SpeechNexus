#!/usr/bin/env python
"""Provision the isolated DEL-109 gateway fixture and build one submit body.

This is a local test fixture, not the production node-controller registration path.
It inserts the five authority rows a readiness receipt would normally create and
uses the isolated MinIO instance root identity, so it must not be pointed at a
shared or production database. The consumed audio is a public Silero test clip.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from minio import Minio
from psycopg import sql

REPO = Path(__file__).resolve().parents[2]
HERE = REPO / ".paperclip-runtime" / "del109"
HERE.mkdir(parents=True, exist_ok=True, mode=0o700)
sys.path.insert(0, str(REPO))

from speechnexus_backend.speech.infrastructure.coordination_grants import (  # noqa: E402
    coordination_runtime_grants,
)
from speechnexus_gateway.domain.canonical import command_digest  # noqa: E402
from speechnexus_gateway.domain.submission import SubmissionIdentity, SubmitRequest  # noqa: E402

MODE = 0o600
BUCKET = "del109-file-asr"
NODE = "node-del109"
GATEWAY = "gateway-del109"
POOL = "asr-batch"
INSTANCE = "instance-del109"
BINDING = "binding-del109"
POLICY = "policy-del109"
WORKSPACE = "workspace-del109"
OPERATION = "operation-del109"
ATTEMPT = "attempt-del109"
STAGE = "stage-del109"
LEASE = "lease-del109"
INTENT = "intent-del109"
DIGEST = "sha256:" + "a" * 64


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(MODE)


def ensure_owner() -> str:
    path = HERE / "owner.env"
    if path.exists():
        password = read_env(path)["PGPASSWORD"]
    else:
        password = secrets.token_hex(24)
        write_env(path, {"PGPASSWORD": password})
        statement = "ALTER USER postgres PASSWORD '" + password.replace("'", "''") + "';\n"
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "del109-pg",
                "psql",
                "-U",
                "postgres",
                "-d",
                "speechnexus",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            input=statement.encode(),
            check=True,
            capture_output=True,
        )
    return (
        "postgresql://postgres:"
        + urllib.parse.quote(password, safe="")
        + "@127.0.0.1:55442/speechnexus"
    )


def main() -> int:
    os.umask(0o077)
    admin_dsn = ensure_owner()
    if "127.0.0.1:55442" not in admin_dsn:
        raise SystemExit("refusing a non-DEL-109 control database")
    minio = read_env(HERE / "minio.env")
    gateway_db = HERE / "gateway-db.env"
    if gateway_db.exists():
        gateway_password = read_env(gateway_db)["GATEWAY_DB_PASSWORD"]
    else:
        gateway_password = secrets.token_hex(24)
        write_env(gateway_db, {"GATEWAY_DB_PASSWORD": gateway_password})

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("SET search_path TO speech, public")
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='sn_gateway'").fetchone()
        if exists is None:
            conn.execute("CREATE ROLE sn_gateway LOGIN")
        conn.execute(
            sql.SQL("ALTER ROLE sn_gateway PASSWORD {}").format(sql.Literal(gateway_password))
        )
        for statement in coordination_runtime_grants(
            "speech", speech_role="sn_api", gateway_role="sn_gateway"
        ):
            conn.execute(statement)

        now_us = int(time.time() * 1_000_000)
        deadline_us = now_us + 3600 * 1_000_000
        for table, column, value in (
            ("submit_intents", "submit_intent_id", INTENT),
            ("output_stream_heads", "operation_id", OPERATION),
            ("admission_leases", "lease_id", LEASE),
            ("admission_attempts", "operation_id", OPERATION),
            ("runtime_instance_heads", "instance_id", INSTANCE),
            ("engine_instances", "instance_id", INSTANCE),
            ("host_nodes", "host_node_id", NODE),
            ("workspace_quotas", "workspace_id", WORKSPACE),
            ("resource_pools", "pool_id", POOL),
        ):
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE {} = %s").format(
                    sql.Identifier("speech", table), sql.Identifier(column)
                ),
                (value,),
            )
        conn.execute(
            "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
            "VALUES (%s, 'batch', 'active')",
            (POOL,),
        )
        conn.execute(
            "INSERT INTO workspace_quotas (workspace_id, max_attempts, policy_revision) "
            "VALUES (%s, 1, %s)",
            (WORKSPACE, POLICY),
        )
        conn.execute(
            "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, "
            "topology_digest) VALUES (%s, 'unified_memory', 'active', %s)",
            (NODE, DIGEST),
        )
        conn.execute(
            "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, "
            "pool_id, gateway_id, binding_revision_id, manifest_id, approved_template_id, "
            "management_mode, loading_policy, config_digest, instance_state, max_slots, "
            "ready_at_us) VALUES (%s, 1, %s, %s, %s, %s, 'manifest-del109', "
            "'template-del109', 'managed', 'resident', %s, 'ready', 1, %s)",
            (INSTANCE, NODE, POOL, GATEWAY, BINDING, DIGEST, now_us),
        )
        conn.execute(
            "INSERT INTO runtime_instance_heads (instance_id, current_generation, "
            "generation_high_water) VALUES (%s, 1, 1)",
            (INSTANCE,),
        )
        conn.execute(
            "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
            "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, "
            "service_class, request_digest, required_stage_ids) "
            "VALUES (%s, %s, %s, %s, 'manifest-del109', %s, %s, 'batch', %s, %s)",
            (OPERATION, ATTEMPT, WORKSPACE, BINDING, DIGEST, POLICY, DIGEST, json.dumps([STAGE])),
        )
        conn.execute(
            "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
            "host_node_id, instance_id, instance_generation, pool_id, gateway_id, "
            "fencing_token, reserved_slots, lease_state, cleanup_state, deadline_at_us) "
            "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, 1, 1, 'reserved', 'pending', %s)",
            (LEASE, OPERATION, ATTEMPT, STAGE, NODE, INSTANCE, POOL, GATEWAY, deadline_us),
        )

    admin = Minio(
        "127.0.0.1:59010",
        access_key=minio["MINIO_ROOT_USER"],
        secret_key=minio["MINIO_ROOT_PASSWORD"],
        secure=False,
    )
    if not admin.bucket_exists(BUCKET):
        admin.make_bucket(BUCKET)
    wav = HERE / "audio" / "speech-12s.wav"
    payload = wav.read_bytes()
    key = "private/del109/speech-12s-" + secrets.token_hex(4) + ".wav"
    import io

    admin.put_object(BUCKET, key, io.BytesIO(payload), len(payload), content_type="audio/wav")

    command = copy.deepcopy(
        json.loads((REPO / "contracts/internal/execution-command.schema.json").read_text())[
            "examples"
        ][1]
    )
    command["audio"]["storage"].update(
        {
            "asset_or_artifact_id": "asset-del109",
            "workspace_id": WORKSPACE,
            "storage_bucket": BUCKET,
            "object_key": key,
            "byte_length": len(payload),
            "content_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
    )
    command["audio"].update({"media_type": "audio/wav", "sample_rate_hz": 16000, "channels": 1})
    command["parameters"].update(
        {
            "language": "en",
            "turn_detection": {"mode": "server_vad", "silence_duration_ms": 500},
            "preview_interval_ms": 500,
            "max_segment_ms": 20000,
            "final_deadline_ms": 10000,
            "auxiliary_deadline_ms": 10000,
            "response_format": "json",
        }
    )
    deadline = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    request = SubmitRequest(
        identity=SubmissionIdentity(
            submit_intent_id=INTENT,
            lease_id=LEASE,
            operation_id=OPERATION,
            attempt_id=ATTEMPT,
            stage_id=STAGE,
            workspace_id=WORKSPACE,
            pool_id=POOL,
            host_node_id=NODE,
            gateway_id=GATEWAY,
            instance_id=INSTANCE,
            instance_generation=1,
            binding_revision_id=BINDING,
            policy_revision_id=POLICY,
            voice_revision_id=None,
            fencing_token=1,
            command_digest=command_digest(command),
            deadline_at=deadline,
            max_output_audio_ms=None,
            sample_rate_hz=None,
        ),
        command=command,
    )
    (HERE / "submit.json").write_text(json.dumps(request.effective_request["execution_request"]))
    (HERE / "submit.json").chmod(MODE)

    source = HERE / "controlled-asr-source.json"
    source.write_text(
        json.dumps(
            {
                "protocol": "vllm_api_key_v1",
                "target": {
                    "host_node_id": NODE,
                    "gateway_id": GATEWAY,
                    "pool_id": POOL,
                    "instance_id": INSTANCE,
                    "instance_generation": 1,
                    "binding_revision_id": BINDING,
                },
                "address": "127.0.0.1",
                "port": 18100,
                "served_model": "qwen3-asr-1.7b",
            }
        )
    )
    source.chmod(MODE)
    root = HERE / "controller-root.env"
    if not root.exists():
        write_env(root, {"SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY": secrets.token_hex(32)})
    descriptor = HERE / "controlled-asr-endpoint.json"
    descriptor.unlink(missing_ok=True)
    env = {**os.environ, **read_env(root)}
    subprocess.run(
        [
            str(REPO / ".venv/bin/python"),
            str(REPO / "scripts/dev-env/provision-controlled-asr.py"),
            "--input",
            str(source),
            "--output",
            str(descriptor),
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    descriptor.chmod(MODE)

    spool = HERE / "spool"
    spool.mkdir(mode=0o700, exist_ok=True)
    spool.chmod(0o700)
    locks = HERE / "gateway-lock"
    locks.mkdir(mode=0o700, exist_ok=True)
    locks.chmod(0o700)

    config = {
        "protocol": "file_asr_v1",
        "controlled_endpoint_file": str(descriptor),
        "require_correlation_echo": False,
        "vad_model_file": str(HERE / "models" / "silero_vad.onnx"),
        "vad_model_sha256": "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
        "objects": {
            "endpoint": "http://127.0.0.1:59010",
            "input_bucket": BUCKET,
            "result_bucket": BUCKET,
            "spool_directory": str(spool),
            "tls_ca_file": None,
            "result_timeout_seconds": 30,
        },
        "vad_tuning": {
            "config_revision": POLICY,
            "threshold": 0.5,
            "end_threshold": 0.35,
            "min_speech_ms": 96,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500,
            "max_segment_ms": 20000,
        },
        "input_limits": {"max_bytes": 268435456, "memory_bytes": 8388608, "timeout_seconds": 60},
        "decode_limits": {
            "max_encoded_bytes": 268435456,
            "max_duration_ms": 120000,
            "max_frame_samples": 65536,
            "timeout_seconds": 30,
        },
        "worker_limits": {
            "max_result_bytes": 4194304,
            "max_command_bytes": 65536,
            "max_pending": 1,
            "shutdown_seconds": 10,
        },
        "max_completion_tokens": 2048,
        "max_call_seconds": 120,
    }
    config_path = HERE / "gateway-file-asr.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    config_path.chmod(MODE)

    component = HERE / "gateway-component.env"
    if not component.exists():
        write_env(component, {"SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN": secrets.token_hex(24)})
    token = read_env(component)["SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN"]
    dsn = (
        "postgresql://sn_gateway:"
        + urllib.parse.quote(gateway_password, safe="")
        + "@127.0.0.1:55442/speechnexus"
    )
    write_env(
        HERE / "gateway.env",
        {
            "SPEECHNEXUS_GATEWAY_ENVIRONMENT": "development",
            "SPEECHNEXUS_GATEWAY_NODE_ID": NODE,
            "SPEECHNEXUS_GATEWAY_GATEWAY_ID": GATEWAY,
            "SPEECHNEXUS_GATEWAY_HOST": "127.0.0.1",
            "SPEECHNEXUS_GATEWAY_PORT": "58030",
            "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL": dsn,
            "SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN": token,
            "SPEECHNEXUS_GATEWAY_INGRESS_LOCK_DIRECTORY": str(locks),
            "SPEECHNEXUS_GATEWAY_FILE_ASR_CONFIG_FILE": str(config_path),
            "SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_ACCESS_KEY": minio["MINIO_ROOT_USER"],
            "SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_SECRET_KEY": minio["MINIO_ROOT_PASSWORD"],
        },
    )
    print(
        json.dumps(
            {
                "bucket": BUCKET,
                "object_key": key,
                "bytes": len(payload),
                "submit": str(HERE / "submit.json"),
                "config": str(config_path),
                "gateway_env": str(HERE / "gateway.env"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
