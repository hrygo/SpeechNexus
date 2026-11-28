#!/usr/bin/env python3
"""Render the app-plane Secret and ConfigMap objects from the deployment env file.

Inputs: ``.runtime/cluster/compose.env`` (written by ``deploy/cluster/init-env.py``)
and ``.runtime/cluster/private/`` (recipe JSON). Nothing secret is passed on a
command line, printed, or written outside ``.runtime``.

Two topologies, because the engine and the coordination database are reached in two
different ways while the application plane moves into the cluster:

``in-cluster`` (default)
    Database, object storage and the engine Service are all reached by their
    cluster names -- the target assembly, once the whole plane runs here.
``tunnel``
    Only the readiness report and renewal are rendered, against the *existing*
    coordination database and object storage through the reverse tunnels that are
    already up on the host (``127.0.0.1:15440`` and ``127.0.0.1:19100``), and the
    engine through the host-loopback shim (``127.0.0.1:18190``). That is the
    additive step: it produces readiness receipts for the live engine without
    forking the coordination domain (#1253).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[3]
CLUSTER = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime/cluster"
ENV_FILE = RUNTIME / "compose.env"
PRIVATE = RUNTIME / "private"
NAMESPACE = "speechnexus"


def role_dsn(env: dict[str, str], role: str, password_key: str) -> str:
    """Build one role DSN from the single password definition point."""
    password = env[password_key]
    if not password:
        raise SystemExit(f"{password_key} is blank: provision the role before rendering")
    return f"postgresql://{role}:{password}@postgres:5432/{env['POSTGRES_DB']}"


BUCKET = "speechnexus-results"

# The two-host link reaches the gateway through the bridge, not loopback, so the
# component credential would travel in clear text. The runtime refuses that unless
# the deployment states the exact origin, so the origin is written once here and
# passed to both credential-presenting app-plane units.
GATEWAY_ORIGIN = "http://bridge.speechnexus.svc:18010"

# The engine address is the one value that must not be copied from anywhere: it is
# where the running instance answers, and it changes with every generation. The
# Service name is the stable form; the loopback is what the tunnel topology has
# today, and it is precisely the volatile form #1253 is about.
ENGINE_SERVICE_HEALTH_URL = "http://engine-asr-1:18190/health"
ENGINE_LOOPBACK_HEALTH_URL = "http://127.0.0.1:18190/health"

TUNNEL_PG = ("127.0.0.1", 15440)
TUNNEL_S3 = "http://127.0.0.1:19100"


def to_tunnel(dsn: str) -> str:
    """Re-point a DSN at the host's reverse tunnel, keeping role and database."""
    parts = urlsplit(dsn)
    host, port = TUNNEL_PG
    netloc = parts.netloc.replace(f"@{parts.hostname}", f"@{host}", 1)
    if parts.port:
        netloc = netloc.replace(f":{parts.port}", f":{port}", 1)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


RECIPES = {
    "transcription-recipe.json": ROOT / "deploy/private/transcription-recipe.example.json",
    "speech-recipe.json": ROOT / "deploy/private/speech-recipe.example.json",
}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def kubectl(*args: str, stdin: str | None = None) -> None:
    result = subprocess.run(
        ["kubectl", *args], input=stdin, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(f"kubectl {' '.join(args)} failed")


def apply_secret(name: str, source: Path) -> None:
    rendered = subprocess.run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "create",
            "secret",
            "generic",
            name,
            f"--from-env-file={source}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if rendered.returncode != 0:
        sys.stderr.write(rendered.stderr)
        raise SystemExit(f"render secret {name} failed")
    kubectl("-n", NAMESPACE, "apply", "-f", "-", stdin=rendered.stdout)


def apply_file_secret(name: str, files: dict[str, Path]) -> None:
    args = ["kubectl", "-n", NAMESPACE, "create", "secret", "generic", name]
    args += [f"--from-file={key}={path}" for key, path in files.items()]
    args += ["--dry-run=client", "-o", "yaml"]
    rendered = subprocess.run(args, text=True, capture_output=True, check=False)
    if rendered.returncode != 0:
        sys.stderr.write(rendered.stderr)
        raise SystemExit(f"render secret {name} failed")
    kubectl("-n", NAMESPACE, "apply", "-f", "-", stdin=rendered.stdout)


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topology",
        choices=("in-cluster", "tunnel"),
        default="in-cluster",
        help="in-cluster: database, object storage and engine by cluster name; "
        "tunnel: only the readiness units, against the existing tunnels",
    )
    args = parser.parse_args(argv)
    topology = args.topology
    if not ENV_FILE.exists():
        raise SystemExit(f"missing {ENV_FILE}; run deploy/cluster/init-env.py first")
    env = load_env(ENV_FILE)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    for name, example in RECIPES.items():
        target = PRIVATE / name
        if not target.exists():
            target.write_text(example.read_text())
            target.chmod(0o600)

    namespace = subprocess.run(
        ["kubectl", "get", "namespace", NAMESPACE], capture_output=True, text=True
    )
    if namespace.returncode != 0:
        raise SystemExit("namespace speechnexus is missing; apply namespace.yaml first")

    tmp = Path(tempfile.mkdtemp(prefix="render-", dir=RUNTIME))
    try:
        postgres = {
            "POSTGRES_USER": env["POSTGRES_USER"],
            "POSTGRES_PASSWORD": env["POSTGRES_PASSWORD"],
            "POSTGRES_DB": env["POSTGRES_DB"],
        }
        minio = {
            "MINIO_ROOT_USER": env["MINIO_ROOT_USER"],
            "MINIO_ROOT_PASSWORD": env["MINIO_ROOT_PASSWORD"],
        }
        migrate = {
            "SPEECHNEXUS_MIGRATION_DATABASE_URL": env["SPEECHNEXUS_MIGRATION_DATABASE_URL"],
        }
        token_file = RUNTIME / "asset-inspection.token"
        if token_file.exists():
            inspection_token = token_file.read_text().strip()
        else:
            import secrets

            inspection_token = secrets.token_urlsafe(32)
            token_file.write_text(inspection_token + "\n")
            token_file.chmod(0o600)

        api = {
            "SPEECHNEXUS_ENVIRONMENT": "development",
            "SPEECHNEXUS_PROCESS_ROLE": "api",
            "SPEECHNEXUS_NODE_ID": "node-app-1",
            "SPEECHNEXUS_INSTANCE_ID": "api-1",
            "SPEECHNEXUS_HOST": "0.0.0.0",
            "SPEECHNEXUS_PORT": "8000",
            "SPEECHNEXUS_DATABASE_URL": env["SPEECHNEXUS_API_DATABASE_URL"],
            "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
            "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": "http://minio:9000/minio/health/live",
            "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": BUCKET,
            "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": env["MINIO_ROOT_USER"],
            "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": env["MINIO_ROOT_PASSWORD"],
            "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE": (
                "/run/speechnexus-private/transcription-recipe.json"
            ),
            "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET": BUCKET,
            "SPEECHNEXUS_TRANSCRIPTION_POLL_SECONDS": "0.25",
            "SPEECHNEXUS_TRANSCRIPTION_MAX_AUDIO_MS": "120000",
            "SPEECHNEXUS_TRANSCRIPTION_MAX_UPLOAD_BYTES": "26214400",
            "SPEECHNEXUS_TRANSCRIPTION_TIMEOUT_SECONDS": "120",
            "SPEECHNEXUS_TRANSCRIPTION_MAX_COMMAND_BYTES": "65536",
            "SPEECHNEXUS_TRANSCRIPTION_STORAGE_TIMEOUT_SECONDS": "3",
            "SPEECHNEXUS_SPEECH_ENABLED": "true",
            "SPEECHNEXUS_SPEECH_RECIPE_FILE": "/run/speechnexus-private/speech-recipe.json",
            "SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE": "en",
            "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": GATEWAY_ORIGIN,
            "SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN": GATEWAY_ORIGIN,
            "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": env[
                "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL"
            ],
            "SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS": "10",
            "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": env["SPEECHNEXUS_JOB_WRITER_DATABASE_URL"],
            "SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL": role_dsn(
                env, "sn_artifact_reader", "SPEECHNEXUS_ARTIFACT_READER_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": role_dsn(
                env, "sn_job_admission", "SPEECHNEXUS_JOB_ADMISSION_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_ASSET_INSPECTOR": "audio",
            "SPEECHNEXUS_ASSET_INSPECTION_URL": "http://asset-inspector:8080",
            "SPEECHNEXUS_ASSET_INSPECTION_TOKEN": inspection_token,
            "SPEECHNEXUS_ASSET_DEFAULT_TTL_SECONDS": "604800",
            "SPEECHNEXUS_ASSET_ALLOWED_ORIGINS": '["http://127.0.0.1:8000","http://localhost:8000"]',
            "SPEECHNEXUS_ASSET_ALLOWED_HOSTS": '["localhost","127.0.0.1","::1"]',
            # Binding administration needs its own least-privilege identities; the
            # defaults-writer mode stays unconfigured on purpose (see
            # docs/operations/compose-minimal.md §"不创建的账号").
            "SPEECHNEXUS_BINDING_WRITER_DATABASE_URL": role_dsn(
                env, "sn_binding_writer", "SPEECHNEXUS_BINDING_WRITER_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL": role_dsn(
                env, "sn_binding_admin_reader", "SPEECHNEXUS_BINDING_ADMIN_READER_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL": role_dsn(
                env, "sn_binding_lifecycle", "SPEECHNEXUS_BINDING_LIFECYCLE_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL": role_dsn(
                env, "sn_binding_list_reader", "SPEECHNEXUS_BINDING_LIST_READER_ROLE_PASSWORD"
            ),
            # Readers and writers the reference API declares for its fuller
            # surface (catalog, audit, pools, quarantine release, settlement,
            # workspace ownership). The roles exist in the cluster role set, so
            # the port carries the same variable names instead of leaving the
            # features silently unavailable.
            "SPEECHNEXUS_AUDIT_READER_DATABASE_URL": role_dsn(
                env, "sn_audit_reader", "SPEECHNEXUS_AUDIT_READER_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL": role_dsn(
                env, "sn_catalog_reader", "SPEECHNEXUS_CATALOG_READER_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL": role_dsn(
                env, "sn_model_instance_reader", "SPEECHNEXUS_MODEL_INSTANCE_READER_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL": role_dsn(
                env, "sn_resource_pool_reader", "SPEECHNEXUS_RESOURCE_POOL_READER_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL": role_dsn(
                env, "sn_quarantine_release", "SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL": role_dsn(
                env, "sn_speech_settlement", "SPEECHNEXUS_SPEECH_SETTLEMENT_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL": role_dsn(
                env, "sn_workspace_writer", "SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD"
            ),
        }

        inspector_token = {"SPEECHNEXUS_ASSET_INSPECTION_TOKEN": inspection_token}
        dispatch = {
            "SPEECHNEXUS_PROCESS_ROLE": "job-worker",
            "SPEECHNEXUS_NODE_ID": "node-app-1",
            "SPEECHNEXUS_INSTANCE_ID": "dispatch-1",
            "SPEECHNEXUS_DATABASE_URL": env["SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL"],
            "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
            "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": GATEWAY_ORIGIN,
            "SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN": GATEWAY_ORIGIN,
            "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": env[
                "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL"
            ],
            "SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS": "10",
            "SPEECHNEXUS_JOB_DISPATCH_KINDS": "asr",
            "SPEECHNEXUS_JOB_DISPATCH_BATCH_SIZE": "8",
            "SPEECHNEXUS_JOB_DISPATCH_LEASE_MS": "30000",
            "SPEECHNEXUS_JOB_DISPATCH_POLL_SECONDS": "1",
            "SPEECHNEXUS_JOB_DISPATCH_JOB_TIMEOUT_SECONDS": "600",
            "SPEECHNEXUS_JOB_DISPATCH_MAX_ATTEMPTS": "5",
            "SPEECHNEXUS_JOB_DISPATCH_INTERVAL_SECONDS": "5",
            "SPEECHNEXUS_JOB_DISPATCH_STORAGE_TIMEOUT_SECONDS": "3",
            "SPEECHNEXUS_ARTIFACT_RETENTION_SECONDS": "2592000",
            "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
            "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": BUCKET,
            "SPEECHNEXUS_EXECUTION_RESULT_BUCKET": BUCKET,
            "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": env["MINIO_ROOT_USER"],
            "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": env["MINIO_ROOT_PASSWORD"],
        }
        registration = {
            "SPEECHNEXUS_REGISTRATION_DATABASE_URL": env["SPEECHNEXUS_REGISTRATION_DATABASE_URL"],
        }
        # Background units from the reference assembly. Variable names are the
        # reference names; where the two-host link goes through the bridge the
        # timeout budget is raised, because the tunnel adds a round trip that the
        # in-process defaults (3 s) do not survive.
        artifact_retention = {
            "SPEECHNEXUS_PROCESS_ROLE": "scheduler",
            "SPEECHNEXUS_NODE_ID": "node-app-1",
            "SPEECHNEXUS_INSTANCE_ID": "retention-1",
            "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
            "SPEECHNEXUS_DATABASE_URL": env["SPEECHNEXUS_ARTIFACT_RETENTION_DATABASE_URL"],
            "SPEECHNEXUS_ARTIFACT_RETENTION_INTERVAL_SECONDS": "60",
            "SPEECHNEXUS_ARTIFACT_RETENTION_BATCH_SIZE": "10",
            "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
            "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": "http://minio:9000/minio/health/live",
            "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": BUCKET,
            "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": env["MINIO_ROOT_USER"],
            "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": env["MINIO_ROOT_PASSWORD"],
        }
        admission_watch = {
            "SPEECHNEXUS_PROCESS_ROLE": "scheduler",
            "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
            "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": role_dsn(
                env, "sn_job_admission", "SPEECHNEXUS_JOB_ADMISSION_ROLE_PASSWORD"
            ),
            "SPEECHNEXUS_ADMISSION_WATCH_ENABLED": "true",
            "SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS": "60",
            "SPEECHNEXUS_ADMISSION_WATCH_ALERT_AFTER": "3",
            "SPEECHNEXUS_ADMISSION_WATCH_WORKSPACES": "workspace-default",
            "SPEECHNEXUS_ADMISSION_WATCH_TRANSACTION_TIMEOUT_SECONDS": "10",
        }
        # #1248's coordinator-side sweep runs under the operator release identity,
        # so its Secret carries exactly that DSN and the schema and nothing else:
        # a scheduled Pod does not get to read the API's tokens through `api-env`.
        # The values are taken from `api` instead of re-deriving the DSN, so the
        # role/password pair exists once.
        lease_reconciler = {
            name: api[name]
            for name in (
                "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL",
                "SPEECHNEXUS_DATABASE_SCHEMA",
            )
        }
        # The readiness units are the only ones rendered for both topologies, so
        # the two values that differ between them are chosen here and nowhere else.
        tunnel = topology == "tunnel"
        readiness = {
            "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
            "SPEECHNEXUS_DATABASE_URL": to_tunnel(env["SPEECHNEXUS_REGISTRATION_DATABASE_URL"])
            if tunnel
            else env["SPEECHNEXUS_REGISTRATION_DATABASE_URL"],
            "SPEECHNEXUS_ENGINE_HEALTH_URL": ENGINE_LOOPBACK_HEALTH_URL
            if tunnel
            else ENGINE_SERVICE_HEALTH_URL,
            "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE": (
                "/run/speechnexus-private/transcription-recipe.json"
            ),
            "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET": BUCKET,
            "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": TUNNEL_S3 if tunnel else "http://minio:9000",
            "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": BUCKET,
            "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": env["MINIO_ROOT_USER"],
            "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": env["MINIO_ROOT_PASSWORD"],
        }

        # Workbench control plane. The migrate identity comes from the migration
        # credential set; the serving and cleanup identities are the two roles the
        # workbench namespace grants target after the migration bootstraps it.
        workbench_migrate = {
            "SPEECHNEXUS_MIGRATION_DATABASE_URL": env["SPEECHNEXUS_MIGRATION_DATABASE_URL"],
            "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": env.get(
                "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA", "workbench"
            ),
            "SPEECHNEXUS_WORKBENCH_DATABASE_URL": env["SPEECHNEXUS_WORKBENCH_DATABASE_URL"],
            "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL": env[
                "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL"
            ],
        }
        workbench_api = {
            "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": workbench_migrate[
                "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA"
            ],
            "SPEECHNEXUS_WORKBENCH_DATABASE_URL": env["SPEECHNEXUS_WORKBENCH_DATABASE_URL"],
            "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL": env[
                "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL"
            ],
            "SPEECHNEXUS_WORKBENCH_WORKSPACE_ID": env.get(
                "SPEECHNEXUS_WORKBENCH_WORKSPACE_ID", "workspace-default"
            ),
            "SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID": env.get(
                "SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID", "deployment-minimal-cluster"
            ),
            "SPEECHNEXUS_WORKBENCH_CURSOR_KEY": env["SPEECHNEXUS_WORKBENCH_CURSOR_KEY"],
            "SPEECHNEXUS_WORKBENCH_ALLOWED_ORIGINS": (
                '["http://127.0.0.1:8081","http://workbench.speechnexus.svc:8080"]'
            ),
        }

        sources = {
            "postgres-credentials": postgres,
            "minio-credentials": minio,
            "migrate-credentials": migrate,
            "api-env": api,
            "registration-credentials": registration,
            "asset-inspector-token": inspector_token,
            "dispatch-env": dispatch,
            "artifact-retention-env": artifact_retention,
            "admission-watch-env": admission_watch,
            "lease-reconciler-env": lease_reconciler,
            "readiness-env": readiness,
            "workbench-migrate-env": workbench_migrate,
            "workbench-api-env": workbench_api,
        }
        if topology == "tunnel":
            # Deliberately only the readiness units: everything else would name
            # services that this assembly does not have, and rendering them here
            # would make an empty coordination domain look assembled.
            sources = {"readiness-env": readiness}
        for name, values in sources.items():
            path = tmp / f"{name}.env"
            write_env_file(path, values)
            apply_secret(name, path)

        apply_file_secret(
            "private-config",
            {name: PRIVATE / name for name in RECIPES},
        )
        if topology == "tunnel":
            printable = sorted(sources)
            print(f"[{topology}] rendered secrets {printable} + private-config")
            return 0

        configmap = subprocess.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "create",
                "configmap",
                "minio-init-script",
                f"--from-file=init-object-storage.py={ROOT / 'deploy/init-object-storage.py'}",
                "--dry-run=client",
                "-o",
                "yaml",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if configmap.returncode != 0:
            sys.stderr.write(configmap.stderr)
            raise SystemExit("render configmap minio-init-script failed")
        kubectl("-n", NAMESPACE, "apply", "-f", "-", stdin=configmap.stdout)

        gate = subprocess.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "create",
                "configmap",
                "workbench-startup-gate",
                f"--from-file=workbench-startup-gate.py="
                f"{ROOT / 'deploy/workbench-startup-gate.py'}",
                "--dry-run=client",
                "-o",
                "yaml",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if gate.returncode != 0:
            sys.stderr.write(gate.stderr)
            raise SystemExit("render configmap workbench-startup-gate failed")
        kubectl("-n", NAMESPACE, "apply", "-f", "-", stdin=gate.stdout)

        nginx = subprocess.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "create",
                "configmap",
                "workbench-nginx-template",
                "--from-file=default.conf.template="
                f"{CLUSTER / 'app-plane/workbench-nginx.conf.template'}",
                "--dry-run=client",
                "-o",
                "yaml",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if nginx.returncode != 0:
            sys.stderr.write(nginx.stderr)
            raise SystemExit("render configmap workbench-nginx-template failed")
        kubectl("-n", NAMESPACE, "apply", "-f", "-", stdin=nginx.stdout)

        print(
            f"[{topology}] rendered secrets {sorted(sources)} + private-config, "
            "configmaps minio-init-script, workbench-startup-gate, workbench-nginx-template"
        )
        return 0
    finally:
        for leftover in tmp.iterdir():
            leftover.unlink()
        tmp.rmdir()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
