"""Real PostgreSQL factory and HTTP with a synthetic gateway and result object."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import socket
import time
import uuid
import wave
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import httpx2
import openai
import pytest
import rfc8785
import uvicorn
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from speechnexus_contracts.execution_canonical import effective_execution_request
from tests.fake.runtime_intents import PLAN
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_transcription_disable_barrier_pg import _seed
from tests.integration.test_tts_durable_pg import database as database
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_transcription_recipe import _document

from speechnexus_backend import app as backend_app
from speechnexus_backend import speech_runtime, transcription_runtime
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
    GatewayRejectedError,
)
from speechnexus_backend.speech.infrastructure.qualified_speech_facts_postgres import (
    speech_synthesis_reader_grants,
)
from speechnexus_backend.speech.infrastructure.transcription_submit import command_digest

pytestmark = pytest.mark.integration


@asynccontextmanager
async def factory_client(app: Any, transport: str) -> AsyncIterator[Any]:
    if transport == "asgi":
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client,
        ):
            yield client
        return

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", lifespan="on", timeout_graceful_shutdown=1)
        )
        existing_tasks = asyncio.all_tasks()
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
    except BaseException:
        listener.close()
        raise
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if server_task.done() or time.monotonic() >= deadline:
                raise RuntimeError("loopback server did not start")
            await asyncio.sleep(0.01)

        class SdkClient:
            async def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
                assert path == "/v1/audio/speech"

                def request() -> httpx.Response:
                    body = dict(json)
                    nexus = dict(body.pop("nexus"))
                    body.setdefault("speed", 1.0)
                    nexus.setdefault("voice_revision", "1")
                    nexus.setdefault("client_reference", "sdk-tts-" + uuid.uuid4().hex)
                    nexus.setdefault("overrides", {}).setdefault("tts", {}).setdefault(
                        "language", "en"
                    )
                    try:
                        with (
                            openai.OpenAI(
                                api_key="synthetic",
                                base_url=f"http://127.0.0.1:{port}/v1",
                                http_client=httpx2.Client(trust_env=False, follow_redirects=False),
                                max_retries=0,
                                timeout=10,
                            ) as client,
                            client.audio.speech.with_streaming_response.create(
                                **body,
                                extra_body={"nexus": nexus},
                                extra_headers={"X-Request-ID": "sdk-tts-" + uuid.uuid4().hex},
                            ) as response,
                        ):
                            return httpx.Response(
                                response.status_code,
                                headers=dict(response.headers),
                                content=response.read(),
                            )
                    except openai.APIStatusError as error:
                        return httpx.Response(
                            error.status_code,
                            headers=dict(error.response.headers),
                            content=error.response.read(),
                        )

                return await asyncio.to_thread(request)

        yield SdkClient()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(server_task), timeout=5)
        except TimeoutError:
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        finally:
            try:
                for owned_server in getattr(server, "servers", ()):
                    owned_server.close()
                    owned_server.abort_clients()
                    await owned_server.wait_closed()
            finally:
                listener.close()
                added = asyncio.all_tasks() - existing_tasks - {asyncio.current_task()}
                for task in added:
                    task.cancel()
                if added:
                    await asyncio.gather(*added, return_exceptions=True)


@pytest.mark.asyncio
async def test_loopback_startup_failure_reclaims_socket_and_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedServer:
        started = False
        should_exit = False
        listener: socket.socket | None = None
        background: asyncio.Task[None] | None = None
        port = 0

        async def serve(self, *, sockets: list[socket.socket]) -> None:
            self.listener = sockets[0]
            self.port = self.listener.getsockname()[1]
            self.background = asyncio.create_task(asyncio.sleep(60))
            raise RuntimeError("synthetic startup failure")

    failed = FailedServer()
    monkeypatch.setattr(uvicorn, "Server", lambda _: failed)
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        async with factory_client(None, "sdk"):
            pytest.fail("a failed server must not accept requests")
    assert failed.should_exit
    assert failed.listener is not None and failed.listener.fileno() == -1
    assert failed.background is not None and failed.background.cancelled()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", failed.port))


def _recipe(path: Path, expiry_us: int) -> None:
    data = _document()
    data.pop("asr")
    data.pop("binding_publication_id")
    data.update(
        protocol="tts_admission_v1",
        model_id="asr-quality",
        binding_revision_id="binding-test",
        manifest_id="manifest-test",
        policy_revision_id="policy-test",
        service_class="realtime",
        admission_expires_at_us=expiry_us,
        stage={
            "stage_id": "stage-main",
            "instance_id": "instance-new",
            "minimum_slots": 1,
            "reserved_slots": 1,
        },
        runtime_plan=asdict(PLAN),
    )
    data["recipe_digest"] = (
        "sha256:"
        + hashlib.sha256(
            rfc8785.dumps({k: v for k, v in data.items() if k != "recipe_digest"})
        ).hexdigest()
    )
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def _voice(admission: AdmissionDatabase, *, audio_format: str = "pcm") -> None:
    db = admission.runtime.foundation.database
    _seed(admission)
    expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    digest = "sha256:" + "1" * 64
    caps = {
        "languages": ["en", "fr"],
        "audio_formats": [audio_format],
        "sample_rates_hz": [24000],
        "modes": ["complete"],
        "max_input_codepoints": 400,
    }
    document = {
        "resource_type": "voice_revision",
        "voice_id": "voice-vivian",
        "revision": "1",
        "object": "nexus.voice_revision",
        "display_name": "Vivian",
        "capabilities": {
            "languages": ["en"],
            "audio_formats": [audio_format],
            "sample_rates_hz": [24000],
            "modes": ["complete"],
            "max_input_codepoints": 400,
        },
        "validation": {
            "validation_id": "report-1",
            "status": "passed",
            "checked_at": "2026-01-01T00:00:00Z",
        },
        "published_at": "2026-01-01T00:00:00Z",
        "retired_at": None,
        "disabled_reason": None,
        "source": {
            "type": "builtin",
            "draft_id": None,
            "draft_revision": None,
            "source_asset_digest": None,
        },
        "status": "published",
        "model_binding": {"id": "asr-quality", "revision": "1"},
    }
    facts = {
        "source_approval_id": "source-1",
        "source_revision": "source-rev",
        "source_digest": digest,
        "qualification_report_id": "report-1",
        "qualification_report_digest": digest,
        "capacity_manifest_id": "manifest-test",
        "capacity_manifest_digest": digest,
        "probe_evidence_digest": digest,
        "verifier_id": "builtin-customvoice-v1",
        "verifier_version": "1",
        "required_checks": [
            "source_approved",
            "artifact_integrity",
            "engine_identity",
            "voice_speaker_binding",
            "wav_complete",
            "output_bounds",
            "native_terminal",
            "stop_release",
        ],
        "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "model_revision": "rev-1",
        "model_artifact_digest": digest,
        "image_digest": digest,
        "generation": "1",
        "instance_id": "instance-new",
        "host_node_id": "host-test",
        "gateway_id": "gateway-test",
        "gateway_policy_revision_id": "policy-test",
        "gateway_policy_digest": digest,
        "binding_id": "asr-quality",
        "binding_revision": "1",
        "binding_revision_id": "binding-test",
        "native_speaker_id": "Vivian",
        "checked_at_us": 1_767_225_600_000_000,
        "expires_at_us": int(expiry.timestamp()) * 1_000_000,
        "public_document": document,
        "qualified_capabilities": caps,
    }
    db.query(
        "INSERT INTO speech_workspaces (workspace_id,display_name,workspace_state,created_at_us) "
        "VALUES ('workspace-test','Synthetic','active',1)"
    )
    definition = {
        "tasks": ["tts_file"],
        "parameter_domains": {
            "tts": {
                "languages": ["en", "fr"],
                "audio_formats": [audio_format],
                "sample_rates_hz": [24000],
                "text_commit_modes": ["explicit"],
                "audio_delivery_modes": ["complete"],
                "max_input_codepoints": 400,
            }
        },
    }
    db.query(
        "UPDATE speech_binding_revisions SET definition_json=%s WHERE revision_id='binding-test'",
        (json.dumps(definition),),
    )
    db.query(
        "INSERT INTO speech_builtin_voice_source_approvals (source_approval_id,voice_id,"
        "source_revision,source_digest,native_speaker_id,expires_at) "
        "VALUES ('source-1','voice-vivian','source-rev',%s,'Vivian',%s)",
        (digest, expiry),
    )
    db.query(
        "INSERT INTO speech_builtin_voice_approvals (approval_id,voice_id,revision,revision_id,"
        "binding_revision_id,source_approval_id,facts,expires_at) "
        "VALUES ('approval-1','voice-vivian','1','voice-rev-1',"
        "'binding-test','source-1',%s::jsonb,%s)",
        (json.dumps(facts), expiry),
    )
    db.query("INSERT INTO speech_publications VALUES ('voice','voice-vivian','1','voice-rev-1',1)")
    db.query(
        "INSERT INTO speech_publication_heads VALUES ('voice','voice-vivian','1','voice-rev-1',1)"
    )
    db.query(
        "INSERT INTO speech_builtin_voice_qualifications VALUES "
        "('voice-vivian','1','voice-rev-1','binding-test','approval-1',%s::jsonb,'Vivian')",
        (json.dumps(document),),
    )
    for grant in speech_synthesis_reader_grants(
        db.schema, admission.runtime.foundation.speech_role
    ):
        db.query(grant)


@pytest.fixture
def settlement_database_url(admission_database: AdmissionDatabase) -> Iterator[str]:
    from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
        disable_settlement_grants,
    )
    from speechnexus_backend.speech.infrastructure.tts_settlement_schema import (
        tts_settlement_worker_grants,
    )

    db = admission_database.runtime.foundation.database
    role = "sn_tts_api_" + uuid.uuid4().hex[:12]
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    try:
        for grant in (
            *disable_settlement_grants(db.schema, role),
            *tts_settlement_worker_grants(db.schema, role),
        ):
            db.query(grant)
        yield make_conninfo(db.url, user=role, password="synthetic_settlement_test")
    finally:
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_format", ["pcm", "wav"])
@pytest.mark.parametrize("transport", ["asgi", "sdk"])
async def test_factory_http_uses_real_postgres_qualification_and_admission(
    admission_database: AdmissionDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settlement_database_url: str,
    audio_format: str,
    transport: str,
) -> None:
    _voice(admission_database, audio_format=audio_format)
    db = admission_database.runtime.foundation.database
    path = tmp_path / "tts.json"
    _recipe(path, admission_database.runtime.now() + 60_000_000)
    settings = _settings().model_copy(
        update={
            "database_url": SecretStr(
                make_conninfo(
                    db.url,
                    user=admission_database.runtime.foundation.speech_role,
                    password="synthetic_foundation_test",
                )
            ),
            "database_schema": db.schema,
            "speech_recipe_file": path,
            "speech_default_language": "en",
            "speech_max_output_audio_ms": 10000,
            "speech_settlement_database_url": SecretStr(settlement_database_url),
            "execution_gateway_base_url": "http://127.0.0.1:9",
            "execution_gateway_component_credential": SecretStr("synthetic-gateway-token"),
            "object_storage_access_key": SecretStr("synthetic"),
            "object_storage_secret_key": SecretStr("synthetic"),
        }
    )
    pcm = b"\0\0" * 2400
    payload = pcm
    if audio_format == "wav":
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as encoder:
            encoder.setnchannels(1)
            encoder.setsampwidth(2)
            encoder.setframerate(24000)
            encoder.writeframes(pcm)
        payload = buffer.getvalue()

    class Gateway:
        request: dict[str, Any] | None = None
        submits = 0

        async def query(self, identity: str) -> Any:
            if self.request is not None:
                return {**self.request, "execution_state": "succeeded"}
            raise GatewayRejectedError("not found", request_may_have_run=False, status_code=404)

        async def query_tts_configuration_evidence(self) -> Any:
            return {
                "gateway_policy_digest": "sha256:" + "1" * 64,
                "native_speaker_id": "Vivian",
                "voice_id": "voice-vivian",
                "voice_revision": "1",
                "voice_revision_id": "voice-rev-1",
                "policy_revision_id": "policy-test",
                "binding_revision_id": "binding-test",
                "node_id": "host-test",
                "gateway_id": "gateway-test",
                "instance_id": "instance-new",
                "instance_generation": 1,
            }

        async def submit(self, request: Any) -> Any:
            self.submits += 1
            self.request = dict(request)
            return {**request, "execution_state": "succeeded"}

        async def audio_staging_result(self, identity: str) -> Any:
            assert self.request is not None
            digest = command_digest(effective_execution_request(self.request))
            return {
                "submit_intent_id": identity,
                "request_digest": digest,
                "staging_result": {
                    "request_digest": digest,
                    "reference": {"workspace_id": "workspace-test", "byte_length": len(payload)},
                    "storage_version": "synthetic-version",
                    "media_type": "audio/wav" if audio_format == "wav" else "audio/pcm",
                    "audio": {"sample_rate_hz": 24000, "channels": 1, "duration_ms": 100},
                },
            }

        async def aclose(self) -> None:
            pass

    class Results:
        def read(self, reference: Any, storage_version: str) -> bytes:
            assert storage_version == "synthetic-version"
            return payload

        def close(self) -> None:
            pass

    class Assets:
        class Service:
            def maintain(self, count: int) -> None:
                pass

        service = Service()
        workspaces = None

        def __init__(self, config: Any) -> None:
            pass

        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

    gateway = Gateway()
    monkeypatch.setattr(backend_app, "AssetRuntime", Assets)
    monkeypatch.setattr(transcription_runtime, "Settings", lambda: settings)
    monkeypatch.setattr(transcription_runtime, "build_transcription", lambda settings: None)
    monkeypatch.setattr(GatewayExecutionClient, "from_settings", lambda settings: gateway)
    monkeypatch.setattr(speech_runtime, "MinioExecutionResultObjects", lambda **kwargs: Results())
    app = transcription_runtime.create_app_from_env()
    assert app.state.speech is not None
    async with factory_client(app, transport) as client:
        response = await client.post(
            "/v1/audio/speech",
            json={
                "model": "asr-quality",
                "voice": "voice-vivian",
                "input": "Hello, this is a short voice qualification test.",
                "response_format": audio_format,
                "speed": 1.0,
                "nexus": {
                    "workspace_id": "workspace-test",
                    "voice_revision": "1",
                    "client_reference": "sdk-tts-" + uuid.uuid4().hex,
                    "overrides": {"tts": {"language": "en"}},
                },
            },
        )
        from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
        from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
            require_builtin_voice,
        )

        with db.admin.cursor() as cursor, pytest.raises(AdmissionRejectedError):
            require_builtin_voice(
                cursor,
                voice_id="voice-vivian",
                revision=1,
                revision_id="voice-rev-1",
                binding_revision_id="binding-test",
                instance_id="instance-new",
                instance_generation=1,
                host_node_id="host-test",
                gateway_id="gateway-test",
                policy_revision_id="policy-test",
                gateway_policy_digest="sha256:" + "f" * 64,
                native_speaker_id="Vivian",
            )
        db.query(
            "INSERT INTO speech_publications VALUES ('voice','voice-vivian','2','voice-rev-2',2)"
        )
        db.query(
            "UPDATE speech_publication_heads SET revision='2',revision_id='voice-rev-2' "
            "WHERE kind='voice' AND object_id='voice-vivian'"
        )
        reader = app.state.speech._facts
        assert reader.latest_publication("voice", "voice-vivian") is None
        assert reader.publication("voice", "voice-vivian", 1).revision_id == "voice-rev-1"
        db.query(
            "UPDATE speech_publication_heads SET revision='1',revision_id='voice-rev-1' "
            "WHERE kind='voice' AND object_id='voice-vivian'"
        )
        forbidden = await client.post(
            "/v1/audio/speech",
            json={
                "model": "asr-quality",
                "voice": "voice-vivian",
                "input": "synthetic two",
                "response_format": audio_format,
                "nexus": {
                    "workspace_id": "workspace-test",
                    "overrides": {"tts": {"language": "fr"}},
                },
            },
        )
        assert forbidden.status_code == 503
        assert forbidden.json()["error"]["code"] == "backend_unavailable"
        assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == 1
        db.query(
            "INSERT INTO speech_builtin_voice_revocations (approval_id,reason) "
            "VALUES ('approval-1','synthetic revocation')"
        )
        revoked = await client.post(
            "/v1/audio/speech",
            json={
                "model": "asr-quality",
                "voice": "voice-vivian",
                "input": "synthetic three",
                "response_format": audio_format,
                "nexus": {"workspace_id": "workspace-test"},
            },
        )
        assert revoked.status_code == 404
        assert revoked.json()["error"]["code"] == "voice_not_found"

        # Rebuild the supported recovery entry after revocation without a recipe or catalog.
        from speechnexus_backend import recover_tts

        monkeypatch.setattr(recover_tts, "GatewayExecutionClient", lambda **kwargs: gateway)
        monkeypatch.setattr(recover_tts, "MinioExecutionResultObjects", lambda **kwargs: Results())
        recovery_settings = recover_tts.TtsRecoverySettings.model_validate(
            {
                "SPEECHNEXUS_DATABASE_URL": settings.database_url,
                "SPEECHNEXUS_DATABASE_SCHEMA": db.schema,
                "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": "http://127.0.0.1:9",
                "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "synthetic-gateway-token",
                "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:9",
                "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "synthetic-bucket",
                "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": "synthetic-access",
                "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": "synthetic-secret",
            }
        )
        original_id = db.query(
            "SELECT request_document::jsonb->'plan'->>'request_id' AS id "
            "FROM admission_operations WHERE request_document::jsonb->>'kind'='tts_file'"
        )[0]["id"]
        before = db.query(
            "SELECT held_attempts, quarantined_attempts FROM workspace_quotas "
            "WHERE workspace_id='workspace-test'"
        )
        recovered_audio = recover_tts.recover(recovery_settings, original_id, timeout_seconds=2)
        output = tmp_path / "recovered.pcm"
        summary = recover_tts.write_private_audio(output, recovered_audio)
        assert output.read_bytes() == payload
        assert summary["sha256"] == hashlib.sha256(payload).hexdigest()
        assert summary["bytes"] == len(payload)
        assert (
            db.query(
                "SELECT held_attempts, quarantined_attempts FROM workspace_quotas "
                "WHERE workspace_id='workspace-test'"
            )
            == before
        )
        assert gateway.submits == 1
        assert gateway.request is not None
        assert (
            db.query(
                "SELECT count(*) AS n FROM admission_leases WHERE operation_id=%s",
                (gateway.request["operation_id"],),
            )[0]["n"]
            == 1
        )
    assert response.status_code == 200, response.text
    if audio_format == "pcm":
        assert response.content == payload
    else:
        with wave.open(io.BytesIO(response.content), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
                1,
                2,
                24000,
            )
            assert audio.getnframes() == 2400
            assert audio.readframes(audio.getnframes()) == pcm
            assert audio.readframes(1) == b""
    assert gateway.request is not None
    assert gateway.request["max_output_audio_ms"] == 10000
    assert gateway.request["command"]["parameters"]["response_format"] == audio_format
    assert gateway.request["command"]["parameters"]["language"] == "en"
    stored = db.query(
        "SELECT request_document::jsonb->'plan' AS plan FROM admission_operations "
        "WHERE request_document::jsonb->>'kind'='tts_file'"
    )[0]["plan"]
    assert stored["gateway_policy_digest"] == "sha256:" + "1" * 64
    assert stored["max_audio_ms"] == 10000
    assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == 1
    assert db.query("SELECT count(*) AS n FROM coordination_jobs")[0]["n"] == 1


def test_real_postgres_factory_stays_closed_without_qualification(
    admission_database: AdmissionDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    class FakeSettlement:
        def __init__(self, settings: Any) -> None:
            pass

        def open(self) -> Any:
            return object()

        def close(self) -> None:
            pass

    monkeypatch.setattr(speech_runtime, "TtsSettlementRuntime", FakeSettlement)
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    for grant in speech_synthesis_reader_grants(
        db.schema, admission_database.runtime.foundation.speech_role
    ):
        db.query(grant)
    path = tmp_path / "tts.json"
    _recipe(path, admission_database.runtime.now() + 60_000_000)
    settings = _settings().model_copy(
        update={
            "database_url": SecretStr(
                make_conninfo(
                    db.url,
                    user=admission_database.runtime.foundation.speech_role,
                    password="synthetic_foundation_test",
                )
            ),
            "database_schema": db.schema,
            "speech_recipe_file": path,
            "speech_default_language": "en",
            "speech_settlement_database_url": SecretStr("postgresql://synthetic@127.0.0.1/test"),
            "execution_gateway_base_url": "http://127.0.0.1:9",
            "execution_gateway_component_credential": SecretStr("synthetic"),
            "object_storage_access_key": SecretStr("synthetic"),
            "object_storage_secret_key": SecretStr("synthetic"),
        }
    )

    def forbid(settings: Any) -> None:
        raise AssertionError("unqualified factory must not build a gateway")

    monkeypatch.setattr(GatewayExecutionClient, "from_settings", forbid)
    assert speech_runtime.build_speech(settings) is None


@pytest.mark.parametrize("blocking_lock", ["pool", "settlement"])
@pytest.mark.parametrize("expired_authority", ["qualification", "source"])
def test_tts_admission_rechecks_expiry_after_final_lock(
    admission_database: AdmissionDatabase,
    tmp_path: Path,
    blocking_lock: str,
    expired_authority: str,
) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from tests.unit.backend.test_tts_durable_intent import plan

    from speechnexus_backend.speech.application.admission import AdmissionService
    from speechnexus_backend.speech.application.speech_synthesis import SpeechUnavailableError
    from speechnexus_backend.speech.infrastructure.admission_postgres import (
        PostgresAdmissionTransactions,
    )
    from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
        require_builtin_voice,
    )
    from speechnexus_backend.speech.infrastructure.tts_admission import (
        PostgresOriginalTtsCommands,
        PostgresTtsAdmission,
        PostgresTtsFacts,
        _identities,
    )
    from speechnexus_backend.speech.infrastructure.tts_recipe import load_tts_recipe

    _voice(admission_database)
    db = admission_database.runtime.foundation.database
    path = tmp_path / "tts-expiry.json"
    _recipe(path, admission_database.runtime.now() + 60_000_000)
    pool = admission_database.runtime.pool
    assert pool is not None
    service = PostgresTtsAdmission(
        PostgresTtsFacts(pool, timeout_seconds=10),
        AdmissionService(
            PostgresAdmissionTransactions(
                pool,
                max_command_bytes=1_048_576,
                timeout_seconds=10,
                require_builtin_voice=require_builtin_voice,
            )
        ),
        PostgresOriginalTtsCommands(pool),
        load_tts_recipe(path),
    )
    selected = replace(
        plan(),
        model_id="asr-quality",
        voice_id="voice-vivian",
        voice_revision=1,
        voice_revision_id="voice-rev-1",
        native_speaker_id="Vivian",
        gateway_policy_digest="sha256:" + "1" * 64,
    )
    if expired_authority == "qualification":
        db.query(
            "UPDATE speech_builtin_voice_approvals SET expires_at=date_trunc('second', "
            "clock_timestamp()) + interval '5 seconds', facts=jsonb_set(facts,'{expires_at_us}', "
            "to_jsonb((extract(epoch from (date_trunc('second', clock_timestamp()) "
            "+ interval '5 seconds')) * 1000000)::bigint)) WHERE approval_id='approval-1'"
        )
        table, key = "speech_builtin_voice_approvals", "approval_id"
        value = "approval-1"
    else:
        db.query(
            "UPDATE speech_builtin_voice_source_approvals SET expires_at="
            "date_trunc('second', clock_timestamp()) + interval '5 seconds' "
            "WHERE source_approval_id='source-1'"
        )
        table, key = "speech_builtin_voice_source_approvals", "source_approval_id"
        value = "source-1"
    before = admission_database.snapshot()
    blocker = db.blocker()
    try:
        if blocking_lock == "pool":
            blocker.execute(
                "SELECT * FROM resource_pools WHERE pool_id=%s FOR UPDATE", (PLAN.binding.pool_id,)
            )
        else:
            blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('speechnexus:tts-cancel:' "
                "|| current_schema() || ':' || %s, 0))",
                (_identities(selected.request_id)[0].operation_id,),
            )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(service.admit, selected)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if db.query(
                    "SELECT pid FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                    (blocker.info.backend_pid,),
                ):
                    break
                if pending.done():
                    pending.result()
                    pytest.fail("admission finished without the expected lock wait")
                time.sleep(0.01)
            else:
                pytest.fail("admission did not wait on the expected lock")
            while not db.query(
                f"SELECT expires_at <= clock_timestamp() AS expired FROM {table} WHERE {key}=%s",
                (value,),
            )[0]["expired"]:
                if time.monotonic() >= deadline:
                    pytest.fail("authority did not expire while admission was blocked")
                time.sleep(0.02)
            blocker.commit()
            with pytest.raises(SpeechUnavailableError):
                pending.result(timeout=10)
        assert admission_database.snapshot() == before
        assert db.query("SELECT count(*) AS n FROM speech_tts_settlement")[0]["n"] == 0
    finally:
        blocker.close()


def test_original_tts_admission_replays_after_qualification_expires(
    admission_database: AdmissionDatabase, tmp_path: Path
) -> None:
    from dataclasses import replace

    from tests.unit.backend.test_tts_durable_intent import plan

    from speechnexus_backend.speech.application.admission import AdmissionService
    from speechnexus_backend.speech.domain.admission import AdmissionResult, AdmitRecipe
    from speechnexus_backend.speech.infrastructure.admission_postgres import (
        PostgresAdmissionTransactions,
    )
    from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
        require_builtin_voice,
    )
    from speechnexus_backend.speech.infrastructure.tts_admission import (
        PostgresOriginalTtsCommands,
        PostgresTtsAdmission,
        PostgresTtsFacts,
    )
    from speechnexus_backend.speech.infrastructure.tts_recipe import load_tts_recipe

    _voice(admission_database)
    db = admission_database.runtime.foundation.database
    path = tmp_path / "tts-replay.json"
    _recipe(path, admission_database.runtime.now() + 60_000_000)
    pool = admission_database.runtime.pool
    assert pool is not None

    class RecordingAdmission(AdmissionService):
        command: AdmitRecipe | None = None

        def admit(self, command: AdmitRecipe) -> AdmissionResult:
            self.command = command
            return super().admit(command)

    admission = RecordingAdmission(
        PostgresAdmissionTransactions(
            pool, max_command_bytes=1_048_576, require_builtin_voice=require_builtin_voice
        )
    )
    service = PostgresTtsAdmission(
        PostgresTtsFacts(pool), admission, PostgresOriginalTtsCommands(pool), load_tts_recipe(path)
    )
    selected = replace(
        plan(),
        model_id="asr-quality",
        voice_id="voice-vivian",
        voice_revision=1,
        voice_revision_id="voice-rev-1",
        native_speaker_id="Vivian",
        gateway_policy_digest="sha256:" + "1" * 64,
    )
    service.admit(selected)
    assert admission.command is not None
    before = admission_database.snapshot()
    db.query(
        "UPDATE speech_builtin_voice_approvals SET "
        "expires_at=clock_timestamp()-interval '1 second' WHERE approval_id='approval-1'"
    )
    replay = admission.admit(admission.command)
    assert replay.outcome == "replayed"
    assert admission_database.snapshot() == before
    assert db.query("SELECT count(*) AS n FROM speech_tts_settlement")[0]["n"] == 1
