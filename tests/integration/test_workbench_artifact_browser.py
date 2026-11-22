"""The workbench page reads one Job's own transcript Artifact in a real browser.

`#829` AC2's remaining half is the *consumption* side: the deployed workbench page must
read the Artifact of the same Job the API reports, over its own origin, and both sides
must have been built from one contract revision. This module drives that path in a real
Chromium against the real API composition root, a real PostgreSQL schema and real
versioned object storage, and then compares three independent records of the same bytes:

1. what the browser wrote to disk (its own SHA-256, recomputed here);
2. what `GET /v1/nexus/artifacts/{id}` and `.../content` answered (status, `etag`,
   `content-length`) — read from the browser's own network log;
3. what the committed rows and the object store hold.

Scope, stated plainly. The Job and Artifact rows are fixture state: this file stages a
Job that already succeeded instead of waiting for a real engine, because the engine path
(vLLM ASR + private model) is the deployment's own qualification and the fixture is the
same device `test_artifact_http_real_storage.py` already uses for the Artifact routes.
What this file does **not** prove is that an engine can produce such a Job — that stays
`#829`'s open item, and no assertion here claims it.

Prerequisites (each missing one fails by name, never silently and never as a skip):

* `SPEECHNEXUS_TEST_DATABASE_URL` — an isolated PostgreSQL DSN with namespace and role
  creation rights. Missing is a failure: a skip is not evidence.
* `SPEECHNEXUS_TEST_MINIO_ENDPOINT`, `SPEECHNEXUS_TEST_MINIO_ACCESS_KEY`,
  `SPEECHNEXUS_TEST_MINIO_SECRET_KEY` — an isolated MinIO. Missing is a failure naming
  the missing variables: the object-storage half of this evidence is not optional.
* Node with `@playwright/test` and its Chromium, plus `web/workbench/dist` built from
  this checkout. The browser program is `workbench-artifact-probe.mjs` beside this
  file; it only reports what the page did, so its evidence can be read beside the
  assertions. Missing is a failure naming the build command: a browser probe without a
  browser is not evidence, and a skip there would read as one.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from minio import Minio
from minio.commonconfig import ENABLED
from minio.error import S3Error
from minio.versioningconfig import VersioningConfig
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import AnyHttpUrl, SecretStr
from speechnexus_contracts.contract_digest import CONTRACT_DIGEST
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.readiness import StaticProbe
from tests.support.portable_pg import connect as portable_pg_connect
from tests.unit.backend.test_health import _settings as _base_settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.infrastructure.artifact_schema import artifact_reader_grants
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.interfaces.http.version_identity import version_identity_body

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
DATABASE_VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
OBJECT_VARIABLES = (
    "SPEECHNEXUS_TEST_MINIO_ENDPOINT",
    "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY",
    "SPEECHNEXUS_TEST_MINIO_SECRET_KEY",
)
PASSWORD = "synthetic_workbench_page_reader"
WORKSPACE = "workspace-default"
MODEL = "asr-quality"
PROBE = "tests/integration/workbench-artifact-probe.mjs"
# Optional: run the deployed Web image as the tier instead of the Python replica.
IMAGE_VARIABLE = "WORKBENCH_PAGE_IMAGE"
DIST = ROOT / "web/workbench/dist"
# The two committed transcripts differ in both bytes and digest, so a read of the wrong
# Job's Artifact cannot be mistaken for a read of this one.
TRANSCRIPT = b'{"kind":"asr","language":"zh","text":"synthetic transcript one"}\n'
OTHER_TRANSCRIPT = b'{"kind":"asr","language":"zh","text":"synthetic transcript two"}\n'
# The production web tier's headers: the page must render under the deployed CSP.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(self), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
        "connect-src 'self' ws: wss:; form-action 'self'"
    ),
}


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class Artifact:
    """One committed transcript Artifact, as both the API and the page must see it."""

    id: str
    job_id: str
    receipt_id: str
    content: bytes = field(repr=False)

    @property
    def sha256(self) -> str:
        return _digest(self.content)

    @property
    def bytes(self) -> int:
        return len(self.content)


@dataclass
class PageSandbox:
    url: str
    api_url: str
    schema: str
    role: str
    admin: psycopg.Connection[dict[str, Any]] = field(repr=False)
    objects: Minio = field(repr=False)
    bucket: str
    output: Path = field(repr=False)
    # A digest the front tier may answer `/v1/nexus/version` with instead of the real one.
    version_override: list[str | None] = field(default_factory=lambda: [None])

    def get(self, path: str) -> httpx.Response:
        """Read through the API itself, the same origin the page's proxy forwards to."""
        with httpx.Client(base_url=self.api_url, timeout=10) as client:
            return client.get(path)

    def front(self, path: str) -> httpx.Response:
        with httpx.Client(base_url=self.url, timeout=10) as client:
            return client.get(path)


def _required_environment() -> tuple[str, str, str, str]:
    database = os.environ.get(DATABASE_VARIABLE)
    if not database:
        pytest.fail(
            f"Configure an isolated PostgreSQL DSN in {DATABASE_VARIABLE}; skip is not evidence"
        )
    objects = [os.environ.get(name) for name in OBJECT_VARIABLES]
    missing = [name for name, value in zip(OBJECT_VARIABLES, objects, strict=True) if not value]
    if missing:
        pytest.fail(
            "Configure an isolated MinIO in "
            + ", ".join(missing)
            + "; missing object storage is not evidence and is never a skip"
        )
    return (database, cast(str, objects[0]), cast(str, objects[1]), cast(str, objects[2]))


def _require_browser() -> None:
    """A browser probe without a browser is a failed run, never a quiet skip."""
    if not DIST.joinpath("index.html").is_file():
        pytest.fail(
            "Build the workbench bundle first: pnpm install --frozen-lockfile && "
            "pnpm --filter @speechnexus/workbench build"
        )
    if (
        subprocess.run(
            ["node", "-e", "require.resolve('@playwright/test')"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    ):
        pytest.fail(
            "Install the web toolchain and its Chromium first: pnpm install --frozen-lockfile "
            "&& pnpm browser:install"
        )


def _migrate(url: str, schema: str) -> None:
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def _seed_committed_job(
    admin: psycopg.Connection[dict[str, Any]],
    objects: Minio,
    bucket: str,
    artifact: Artifact,
    *,
    model: str = MODEL,
) -> tuple[str, str]:
    """One succeeded Job whose committed transcript Artifact is readable.

    The rows are the same committed shape the result publisher writes; the object is a
    real versioned object, so the Artifact routes verify a real digest instead of a stub.
    Returns the object key and version so cleanup can delete the exact version it wrote,
    even on a path where listing the bucket is what failed.
    """
    object_key = f"private/artifacts/{artifact.id}/object"
    written = objects.put_object(
        bucket,
        object_key,
        io.BytesIO(artifact.content),
        len(artifact.content),
        content_type="application/json",
    )
    assert written.version_id is not None
    admin.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, result_json, created_at_us, updated_at_us) VALUES "
        "(%s, %s, 'asr', %s, %s, 'succeeded', 2, %s, 1700000000000000, 1700000002000000)",
        (
            artifact.job_id,
            WORKSPACE,
            model,
            _digest(json.dumps({"operation": "create_job"}, sort_keys=True).encode()),
            json.dumps(
                {
                    "kind": "asr",
                    "transcript_artifact_id": artifact.id,
                    "receipt_id": artifact.receipt_id,
                }
            ),
        ),
    )
    admin.execute(
        "INSERT INTO speech_job_submissions (job_id, request_id, client_reference, "
        "effective_configuration_json, execution_revision, progress_unit, progress_total, "
        "submitted_at_us) VALUES (%s, %s, NULL, %s, '1', 'audio_ms', 1, 1700000000000000)",
        (
            artifact.job_id,
            "request-browser-1",
            json.dumps(
                {
                    "models": {"asr": {"id": model, "revision": "1"}},
                    "policy_revision": "1",
                    "catalog_revision": "1",
                }
            ),
        ),
    )
    admin.execute(
        "INSERT INTO speech_artifacts (artifact_id, workspace_id, artifact_kind, "
        "artifact_state, media_type, bytes, sha256, receipt_id, source_operation_id, "
        "object_key, storage_version_id, created_at_us, expires_at_us, deleted_at_us) "
        "VALUES (%s, %s, 'transcript', 'ready', 'application/json', %s, %s, %s, "
        "'operation-browser-1', %s, %s, 1700000000000000, NULL, NULL)",
        (
            artifact.id,
            WORKSPACE,
            artifact.bytes,
            artifact.sha256,
            artifact.receipt_id,
            object_key,
            written.version_id,
        ),
    )
    return object_key, written.version_id


def _api_settings(
    url: str,
    schema: str,
    role: str,
    reader_role: str,
    endpoint: str,
    bucket: str,
    access: str,
    secret: str,
) -> Any:
    writer = make_conninfo(url, user=role, password=PASSWORD)
    reader = make_conninfo(url, user=reader_role, password=PASSWORD)
    return _base_settings().model_copy(
        update={
            "process_role": "api",
            "node_id": "node-workbench-page",
            "instance_id": "api-workbench-page",
            "database_url": SecretStr(url),
            "database_schema": schema,
            "job_writer_database_url": SecretStr(writer),
            "artifact_reader_database_url": SecretStr(reader),
            "object_storage_endpoint": AnyHttpUrl(endpoint),
            "object_storage_healthcheck_url": AnyHttpUrl(
                endpoint.rstrip("/") + "/minio/health/live"
            ),
            "object_storage_bucket": bucket,
            "object_storage_access_key": SecretStr(access),
            "object_storage_secret_key": SecretStr(secret),
            "capability_default_workspace_id": WORKSPACE,
        }
    )


# S3 answers that mean the request was refused or had nothing to act on, so no bucket
# can have been created; cleanup after a failed `make_bucket` need not report these.
_NOTHING_CREATED = frozenset(
    {"NoSuchBucket", "SignatureDoesNotMatch", "AccessDenied", "InvalidAccessKeyId"}
)


def _cancel_tasks(loop: asyncio.AbstractEventLoop) -> None:
    for task in asyncio.all_tasks(loop):
        task.cancel()


def _stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        raise RuntimeError("the server thread did not stop after shutdown")


def _remove_listed_versions(objects: Minio, bucket: str) -> None:
    for item in objects.list_objects(bucket, recursive=True, include_version=True):
        objects.remove_object(bucket, item.object_name, version_id=item.version_id)


def _remove_seeded_objects(objects: Minio, bucket: str, seeded: list[tuple[str, str]]) -> None:
    for key, version_id in seeded:
        objects.remove_object(bucket, key, version_id=version_id)


def _remove_bucket(objects: Minio, bucket: str) -> None:
    _remove_listed_versions(objects, bucket)
    objects.remove_bucket(bucket)


def _drop_roles(admin: psycopg.Connection[dict[str, Any]], names: tuple[str, str]) -> None:
    for name in names:
        admin.execute(f'DROP ROLE IF EXISTS "{name}"')


def _serve(app: FastAPI) -> tuple[str, uvicorn.Server, threading.Thread]:
    """Serve one app on a loopback port; the browser needs real HTTP, not ASGI transport.

    The server owns its event loop so a startup that never finishes can be aborted: the
    timeout path cancels the loop's tasks, joins the thread and closes the socket.
    `should_exit` alone does not interrupt a lifespan that is still starting, and a
    daemon thread stuck there would outlive the failure still holding the port.
    """
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="on"))
    loop_slot: list[asyncio.AbstractEventLoop] = []
    crash: list[BaseException] = []

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_slot.append(loop)
        try:
            loop.run_until_complete(server.serve(sockets=[sock]))
        except BaseException as error:
            # A startup failure or this helper's own cancellation lands here; `_serve`
            # reports the first and the timeout path reports the second.
            crash.append(error)
        finally:
            loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if crash:
                raise AssertionError(
                    f"the app did not start: {type(crash[0]).__name__}: {crash[0]}"
                ) from crash[0]
            if not thread.is_alive():
                raise AssertionError("the app did not start: the server thread exited")
            if time.monotonic() >= deadline:
                raise AssertionError("the app did not start")
            time.sleep(0.01)
    except BaseException:
        server.should_exit = True
        if loop_slot:
            loop = loop_slot[0]
            # The loop may already have stopped on its own; cancellation is best effort.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_cancel_tasks, loop)
        thread.join(timeout=10)
        sock.close()
        raise
    return f"http://127.0.0.1:{port}", server, thread


def _web_tier(api_url: str, version_override: list[str | None]) -> FastAPI:
    """The deployed web tier: one origin, the built bundle, `/v1/` proxied to the API."""

    app = FastAPI()
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    def with_headers(response: Response) -> Response:
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    @app.get("/")
    async def index() -> Response:
        return with_headers(FileResponse(DIST / "index.html"))

    @app.get("/favicon.svg")
    async def favicon() -> Response:
        return with_headers(FileResponse(DIST / "favicon.svg"))

    @app.get("/v1/nexus/version")
    async def version() -> Response:
        digest = version_override[0] or CONTRACT_DIGEST
        body = version_identity_body("0.1.0.dev0")
        body["contract_digest"] = digest
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.api_route("/v1/{path:path}", methods=["GET"])
    async def proxy(path: str, request: Request) -> Response:
        async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
            forwarded = await client.get(
                f"/v1/{path}", params=request.query_params, headers={"accept": "*/*"}
            )
        headers = {
            name: value
            for name, value in forwarded.headers.items()
            if name.lower()
            in {"content-type", "content-length", "etag", "content-disposition", "cache-control"}
        }
        return with_headers(Response(forwarded.content, forwarded.status_code, headers=headers))

    return app


def _free_port() -> int:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _host_ipv4(image: str) -> str:
    """The host's IPv4 as a container sees it; nginx gets a literal, never a DNS name.

    The image's own nginx resolves variable upstreams with Docker's embedded DNS, and
    `host.docker.internal` also has an IPv6 record, so the upstream is pinned to the
    IPv4 literal the same DNS answers with.
    """
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "nslookup host.docker.internal 2>&1 | sed -n 's/^Address: *//p'",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    answers = [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().replace(".", "").isdigit()
    ]
    assert answers, completed.stdout
    return answers[0]


def _run_image_tier(image: str, api_url: str, name: str) -> tuple[str, str]:
    """Serve the deployed Web image: its nginx config, its CSP, its proxy to the API."""
    port = _free_port()
    host = _host_ipv4(image)
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            f"127.0.0.1:{port}:8080",
            "--env",
            f"WEB_SPEECH_UPSTREAM={api_url.replace('127.0.0.1', host)}",
            "--env",
            f"WEB_WORKBENCH_UPSTREAM={api_url.replace('127.0.0.1', host)}",
            "--env",
            "WEB_DNS_RESOLVER=127.0.0.11",
            image,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    origin = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if httpx.get(origin + "/healthz", timeout=2).status_code == 200:
                    return origin, name
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise AssertionError("the Web image did not answer /healthz: " + name)
    except BaseException:
        # The container exists even when the tier never becomes ready; a failed setup
        # must not leave it behind for the next run to trip over.
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)
        raise


@contextmanager
def _stack(*, image: str | None) -> Iterator[PageSandbox]:
    """Stage the real stack; everything created after `try` opens is cleaned in `finally`.

    No resource is created before the `try` opens. Each handle stays absent until its
    creation succeeds, and `finally` cleans only what exists — so a failure while
    creating the schema, either role, the bucket or the servers runs the same cleanup
    instead of leaking the schema, both roles and the bucket.

    Cleanup steps fail independently: one broken step must not skip the rest or replace
    the failure that caused the unwind. A cleanup failure is attached to the original
    exception as a note; if the body succeeded, it is raised on its own naming the step.
    """
    _require_browser()
    url, endpoint, access, secret = _required_environment()
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_page_int_" + suffix
    # Two identities, as a deployment has: the Job read surface and the Artifact reader
    # are separate principals, and the reader one is verified to hold nothing else.
    role, reader_role = "sn_page_jobs_" + suffix, "sn_page_artifact_" + suffix
    bucket = "sn-page-" + suffix
    admin: psycopg.Connection[dict[str, Any]] | None = None
    pool: SpeechPool | None = None
    objects: Minio | None = None
    api_server: uvicorn.Server | None = None
    api_thread: threading.Thread | None = None
    front_server: uvicorn.Server | None = None
    front_thread: threading.Thread | None = None
    container: str | None = None
    bucket_created = False
    seeded: list[tuple[str, str]] = []
    try:
        admin = portable_pg_connect(url, row_factory=dict_row, autocommit=True)
        _migrate(url, schema)
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        admin.execute(f"CREATE ROLE \"{reader_role}\" LOGIN PASSWORD '{PASSWORD}'")
        for grant in public_job_grants(schema, role):
            admin.execute(grant)
        for grant in artifact_reader_grants(schema, reader_role):
            admin.execute(grant)
        # The migration already seeds the default workspace; only create it when absent.
        admin.execute(
            "INSERT INTO speech_workspaces (workspace_id, display_name, workspace_state, "
            "created_at_us) VALUES (%s, 'Synthetic page workspace', 'active', 1700000000000000) "
            "ON CONFLICT (workspace_id) DO NOTHING",
            (WORKSPACE,),
        )
        objects = Minio(
            endpoint.removeprefix("http://").removeprefix("https://"),
            access_key=access,
            secret_key=secret,
            secure=endpoint.startswith("https://"),
            region="us-east-1",
        )
        try:
            objects.make_bucket(bucket)
        except BaseException as error:
            # CreateBucket can commit server-side even when its reply is lost, so a
            # client error does not prove the bucket is absent. `bucket_created` is
            # still false, so remove it here rather than leaving it to `finally`.
            try:
                _remove_bucket(objects, bucket)
            except BaseException as cleanup_error:
                if not (
                    isinstance(cleanup_error, S3Error) and cleanup_error.code in _NOTHING_CREATED
                ):
                    error.add_note(
                        "cleanup after make_bucket failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            raise
        bucket_created = True
        objects.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
        ours = Artifact("artifact-page-transcript-1", "job-page-1", "receipt-page-1", TRANSCRIPT)
        foreign = Artifact(
            "artifact-page-transcript-2", "job-page-2", "receipt-page-2", OTHER_TRANSCRIPT
        )
        seeded.append(_seed_committed_job(admin, objects, bucket, ours))
        seeded.append(_seed_committed_job(admin, objects, bucket, foreign))
        # The read surface is the real composition root's: one read-only public-Job pool
        # over the committed rows, opened here so the fixture owns its lifetime.
        pool = speech_pool(
            make_conninfo(url, user=role, password=PASSWORD),
            schema=schema,
            max_size=2,
            name="workbench-page-" + suffix,
        )
        pool.open()
        adapter = PostgresPublicJobs(pool, timeout_seconds=3, storage_bucket=bucket)
        api_app = create_app(
            settings=_api_settings(
                url, schema, role, reader_role, endpoint, bucket, access, secret
            ),
            assets=cast(Any, object()),
            jobs=PublicJobs(adapter, adapter),
            readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
        )
        api_url, api_server, api_thread = _serve(api_app)
        version_override: list[str | None] = [None]
        if image is None:
            front_url, front_server, front_thread = _serve(_web_tier(api_url, version_override))
        else:
            container = "sn951-page-" + suffix
            front_url, container = _run_image_tier(image, api_url, container)
        value = PageSandbox(
            url=front_url,
            api_url=api_url,
            schema=schema,
            role=role,
            admin=admin,
            objects=objects,
            bucket=bucket,
            output=ROOT / ".runtime" / "workbench-page" / suffix,
            version_override=version_override,
        )
        value.output.mkdir(parents=True, exist_ok=True)
        yield value
    finally:
        failures: list[tuple[str, BaseException]] = []

        def cleanup(name: str, action: Callable[[], object]) -> None:
            """Run one cleanup step; a failure must not skip the steps after it."""
            try:
                action()
            except BaseException as error:
                failures.append((name, error))

        if pool is not None:
            cleanup("pool", pool.close)
        if container is not None:
            cleanup(
                "image container",
                lambda: subprocess.run(
                    ["docker", "rm", "--force", container], capture_output=True, check=False
                ),
            )
        if front_server is not None and front_thread is not None:
            cleanup("front server", lambda: _stop_server(front_server, front_thread))
        if api_server is not None and api_thread is not None:
            cleanup("api server", lambda: _stop_server(api_server, api_thread))
        if objects is not None and bucket_created:
            # The versions this fixture wrote are deleted by identity first: a bucket
            # listing that fails must not leave the bucket non-empty and then block
            # its own removal.
            cleanup("seeded objects", lambda: _remove_seeded_objects(objects, bucket, seeded))
            cleanup("bucket listing", lambda: _remove_listed_versions(objects, bucket))
            cleanup("bucket", lambda: objects.remove_bucket(bucket))
        if admin is not None:
            cleanup("schema", lambda: admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            cleanup("roles", lambda: _drop_roles(admin, (role, reader_role)))
            cleanup("admin connection", admin.close)

        if failures:
            report = "; ".join(
                f"{name}: {type(error).__name__}: {error}" for name, error in failures
            )
            original = sys.exc_info()[1]
            if original is not None:
                original.add_note("cleanup failed after the original error: " + report)
            else:
                raise RuntimeError("cleanup failed: " + report) from failures[0][1]


@pytest.fixture
def sandbox() -> Iterator[PageSandbox]:
    with _stack(image=None) as value:
        yield value


def _probe(sandbox: PageSandbox, name: str, **options: str) -> dict[str, Any]:
    argv = [
        "node",
        PROBE,
        "--url",
        sandbox.url,
        "--output",
        str(sandbox.output),
        "--name",
        name,
        *[part for key, value in options.items() for part in (f"--{key}", value)],
    ]
    completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return cast(
        dict[str, Any], json.loads((sandbox.output / f"{name}.json").read_text(encoding="utf-8"))
    )


def _assert_page_read_the_same_artifact(evidence: dict[str, Any], expected: Artifact) -> None:
    """The one judgment: the page's bytes are *this* Job's Artifact, not any other read."""
    download = evidence["download"]
    assert download is not None, "the page handed no downloaded Artifact to the browser"
    assert download["sha256"] == expected.sha256, download
    assert download["bytes"] == expected.bytes, download
    assert download["isValidJson"] is True, download
    content_reads = [
        item
        for item in evidence["responses"]
        if item["path"] == f"/v1/nexus/artifacts/{expected.id}/content"
    ]
    assert len(content_reads) == 1, evidence["responses"]
    assert content_reads[0]["status"] == 200
    assert content_reads[0]["etag"] == f'"{expected.sha256}"'
    assert content_reads[0]["contentLength"] == str(expected.bytes)
    metadata_reads = [
        item
        for item in evidence["responses"]
        if item["path"] == f"/v1/nexus/artifacts/{expected.id}"
    ]
    assert len(metadata_reads) == 1, evidence["responses"]
    assert metadata_reads[0]["status"] == 200
    job_reads = [
        item
        for item in evidence["responses"]
        if item["path"] == f"/v1/nexus/jobs/{expected.job_id}"
    ]
    assert len(job_reads) == 1, evidence["responses"]
    assert job_reads[0]["status"] == 200
    artifacts = evidence["job"]["artifactButtons"]
    assert artifacts == [f"下载原始制品 {expected.id}"], artifacts
    assert evidence["job"]["status"] == "succeeded"
    assert expected.job_id in evidence["job"]["identityRows"]
    assert evidence["alerts"] == [], evidence["alerts"]
    assert evidence["pageErrors"] == [], evidence["pageErrors"]
    assert evidence["consoleErrors"] == [], evidence["consoleErrors"]
    assert evidence["outbound"] == [], evidence["outbound"]


def test_the_page_reads_the_same_jobs_artifact(sandbox: PageSandbox) -> None:
    ours = Artifact("artifact-page-transcript-1", "job-page-1", "receipt-page-1", TRANSCRIPT)
    evidence = _probe(sandbox, "same-job", job=ours.job_id, artifact=ours.id, model=MODEL)

    _assert_page_read_the_same_artifact(evidence, ours)

    # The API's own bytes, read beside the page: three records of one Artifact.
    metadata = sandbox.get(f"/v1/nexus/artifacts/{ours.id}")
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["id"] == ours.id
    assert metadata.json()["sha256"] == ours.sha256
    assert metadata.json()["bytes"] == ours.bytes
    assert metadata.json()["kind"] == "transcript"
    assert metadata.json()["status"] == "ready"
    content = sandbox.get(f"/v1/nexus/artifacts/{ours.id}/content")
    assert content.status_code == 200, content.text
    assert content.content == ours.content
    assert content.headers["etag"] == f'"{ours.sha256}"'
    assert _digest(content.content) == ours.sha256

    job = sandbox.get(f"/v1/nexus/jobs/{ours.job_id}")
    assert job.status_code == 200, job.text
    body = job.json()
    assert body["id"] == ours.job_id
    assert body["status"] == "succeeded"
    assert [item["artifact_id"] for item in body["artifacts"]] == [ours.id]
    assert body["artifacts"][0]["sha256"] == ours.sha256
    assert body["artifacts"][0]["bytes"] == ours.bytes
    assert body["result"]["transcript_artifact_id"] == ours.id
    assert body["result"]["receipt_id"] == ours.receipt_id

    # The two sides of the contract digest: what the served bundle baked in, and what
    # the API this page talks to reports. The page itself checked the same pair.
    bundle = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted((DIST / "assets").glob("*.js"))
    )
    assert CONTRACT_DIGEST in bundle
    version = sandbox.get("/v1/nexus/version")
    assert version.status_code == 200, version.text
    assert version.json()["contract_digest"] == CONTRACT_DIGEST
    page_version = [
        item["body"] for item in evidence["responses"] if item["path"] == "/v1/nexus/version"
    ]
    assert len(page_version) == 1, evidence["responses"]
    assert json.loads(cast(str, page_version[0]))["contract_digest"] == CONTRACT_DIGEST
    assert not any("前后端版本不一致" in text for text in evidence["alerts"])

    # The deployed Web image, when one is staged: the same judgment through its own
    # nginx, CSP and `/v1/` proxy instead of this process's replica of that tier.
    image = os.environ.get(IMAGE_VARIABLE)
    if not image:
        print(
            f"image tier not exercised: set {IMAGE_VARIABLE} to the deployed Web image "
            "(docker build -f deploy/images/web.Dockerfile)"
        )
        return
    with _stack(image=image) as staged:
        staged_evidence = _probe(
            staged, "image-same-job", job=ours.job_id, artifact=ours.id, model=MODEL
        )
        _assert_page_read_the_same_artifact(staged_evidence, ours)
        staged_version = [
            item for item in staged_evidence["responses"] if item["path"] == "/v1/nexus/version"
        ]
        assert len(staged_version) == 1, staged_evidence["responses"]
        assert (
            json.loads(cast(str, staged_version[0]["body"]))["contract_digest"] == CONTRACT_DIGEST
        )
        assert staged.front("/healthz").status_code == 200
        print(f"CHECK image tier image={image} artifact={ours.id} sha256={ours.sha256}")


def test_a_read_of_another_jobs_artifact_is_not_this_ones(sandbox: PageSandbox) -> None:
    """Counter-example 1: the judgment binds to Job identity, not to "some Artifact"."""
    ours = Artifact("artifact-page-transcript-1", "job-page-1", "receipt-page-1", TRANSCRIPT)
    foreign = Artifact(
        "artifact-page-transcript-2", "job-page-2", "receipt-page-2", OTHER_TRANSCRIPT
    )
    evidence = _probe(
        sandbox, "same-job-for-counterexample", job=ours.job_id, artifact=ours.id, model=MODEL
    )
    with pytest.raises(AssertionError):
        _assert_page_read_the_same_artifact(evidence, foreign)

    # The reading itself is unchanged: the same page bytes still describe this Job only.
    _assert_page_read_the_same_artifact(evidence, ours)
    assert ours.sha256 != foreign.sha256


def test_a_changed_sha256_breaks_the_same_artifact_judgment(sandbox: PageSandbox) -> None:
    """Counter-example 2: one edited digest on the server turns the judgment red."""
    ours = Artifact("artifact-page-transcript-1", "job-page-1", "receipt-page-1", TRANSCRIPT)
    forged = _digest(OTHER_TRANSCRIPT)
    sandbox.admin.execute(
        "UPDATE speech_artifacts SET sha256 = %s WHERE artifact_id = %s", (forged, ours.id)
    )
    try:
        evidence = _probe(sandbox, "forged-sha256", job=ours.job_id, artifact=ours.id, model=MODEL)
        with pytest.raises(AssertionError):
            _assert_page_read_the_same_artifact(evidence, ours)
        metadata = sandbox.get(f"/v1/nexus/artifacts/{ours.id}")
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["sha256"] == forged
        assert metadata.json()["sha256"] != ours.sha256
    finally:
        sandbox.admin.execute(
            "UPDATE speech_artifacts SET sha256 = %s WHERE artifact_id = %s",
            (ours.sha256, ours.id),
        )


def test_a_mixed_contract_digest_is_reported_by_the_page(sandbox: PageSandbox) -> None:
    """Counter-example 3: the same-version criterion goes red on an unequal digest pair."""
    sandbox.version_override[0] = "sha256:" + "b" * 64
    try:
        evidence = _probe(sandbox, "mixed-digest", mode="identity")
    finally:
        sandbox.version_override[0] = None
    assert any("前后端版本不一致" in text for text in evidence["alerts"]), evidence
    answers = [
        item["body"] for item in evidence["responses"] if item["path"] == "/v1/nexus/version"
    ]
    assert len(answers) == 1, evidence["responses"]
    assert json.loads(cast(str, answers[0]))["contract_digest"] == "sha256:" + "b" * 64
    assert CONTRACT_DIGEST != "sha256:" + "b" * 64

    # And the aligned deployment answers the same page with silence.
    aligned = _probe(sandbox, "aligned-digest", mode="identity")
    assert aligned["alerts"] == [], aligned["alerts"]


def test_the_page_is_served_under_the_deployed_response_headers(sandbox: PageSandbox) -> None:
    """The page renders under the deployed response headers: the #840 gate."""
    page = sandbox.front("/")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    evidence = _probe(sandbox, "csp", mode="identity")
    assert evidence["pageErrors"] == [], evidence["pageErrors"]
    # An empty console is part of the CSP gate: `unsafe-eval` (or any other console
    # error) must not appear, so the deployed policy renders this page silently.
    assert evidence["consoleErrors"] == [], evidence["consoleErrors"]
