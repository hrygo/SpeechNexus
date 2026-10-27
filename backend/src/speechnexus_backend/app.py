"""ASGI composition root for the speech backend."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import Response

from speechnexus_backend import __version__
from speechnexus_backend.artifact_runtime import ArtifactRuntime
from speechnexus_backend.asset_runtime import AssetRuntime
from speechnexus_backend.binding_runtime import (
    BindingAdminReaderRuntime,
    BindingDefaultsRuntime,
    BindingRuntime,
)
from speechnexus_backend.job_runtime import JobRuntime
from speechnexus_backend.model_catalog_runtime import ModelCatalogRuntime
from speechnexus_backend.profile_runtime import ProfileRuntime
from speechnexus_backend.pronunciation_runtime import PronunciationRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.artifacts import ArtifactService
from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.application.binding_default_drafts import BindingDefaultDrafts
from speechnexus_backend.speech.application.binding_drafts import BindingDrafts
from speechnexus_backend.speech.application.binding_publication import (
    BindingPublicationFacts,
)
from speechnexus_backend.speech.application.capability_snapshot import CapabilityPolicy
from speechnexus_backend.speech.application.profile_drafts import ProfileDrafts
from speechnexus_backend.speech.application.pronunciation_drafts import PronunciationReader
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.readiness import (
    ReadinessProbe,
    ReadinessService,
)
from speechnexus_backend.speech.application.speech_synthesis import SpeechSynthesis
from speechnexus_backend.speech.application.transcription import Transcription
from speechnexus_backend.speech.application.workspaces import WorkspaceService
from speechnexus_backend.speech.infrastructure.readiness import (
    ObjectStorageReadinessProbe,
    PostgresReadinessProbe,
)
from speechnexus_backend.speech.interfaces.http.artifact_errors import artifact_error
from speechnexus_backend.speech.interfaces.http.artifacts import (
    PREFIX as ARTIFACT_PREFIX,
)
from speechnexus_backend.speech.interfaces.http.artifacts import create_artifact_router
from speechnexus_backend.speech.interfaces.http.asset_contract import asset_http_contract
from speechnexus_backend.speech.interfaces.http.asset_errors import asset_error
from speechnexus_backend.speech.interfaces.http.asset_ingress import AssetIngress
from speechnexus_backend.speech.interfaces.http.assets import create_asset_router
from speechnexus_backend.speech.interfaces.http.binding_contract import binding_http_contract
from speechnexus_backend.speech.interfaces.http.binding_ingress import BindingIngress
from speechnexus_backend.speech.interfaces.http.bindings import create_binding_router
from speechnexus_backend.speech.interfaces.http.health import create_health_router
from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from speechnexus_backend.speech.interfaces.http.jobs import PREFIX as JOB_PREFIX
from speechnexus_backend.speech.interfaces.http.jobs import create_job_router
from speechnexus_backend.speech.interfaces.http.middleware import RequestIdMiddleware
from speechnexus_backend.speech.interfaces.http.models import ModelCatalogue, create_model_router
from speechnexus_backend.speech.interfaces.http.profile_contract import profile_http_contract
from speechnexus_backend.speech.interfaces.http.profile_ingress import ProfileIngress
from speechnexus_backend.speech.interfaces.http.profiles import create_profile_router
from speechnexus_backend.speech.interfaces.http.pronunciations import (
    PronunciationIngress,
    create_pronunciation_router,
    pronunciation_http_contract,
)
from speechnexus_backend.speech.interfaces.http.speech import PREFIX as SPEECH_PREFIX
from speechnexus_backend.speech.interfaces.http.speech import create_speech_router
from speechnexus_backend.speech.interfaces.http.speech_errors import speech_error
from speechnexus_backend.speech.interfaces.http.transcriptions import (
    create_transcription_router,
)
from speechnexus_backend.speech.interfaces.http.workspace_contract import workspace_http_contract
from speechnexus_backend.speech.interfaces.http.workspaces import (
    PREFIX as WORKSPACE_PREFIX,
)
from speechnexus_backend.speech.interfaces.http.workspaces import (
    WorkspaceIngress,
    create_workspace_router,
    workspace_error,
)


class _SpeechAPI(FastAPI):
    _business_contracts_added = False
    _binding_defaults_enabled = False

    def openapi(self) -> dict[str, Any]:
        document = super().openapi()
        if not self._business_contracts_added:
            # Project the complete source schemas for implemented methods only.
            # Namespaced admin components coexist with the original asset ones.
            for contract in (
                asset_http_contract(),
                workspace_http_contract(),
                profile_http_contract(),
                pronunciation_http_contract(),
                binding_http_contract(producer=self._binding_defaults_enabled),
            ):
                surface = copy.deepcopy(contract)
                document["paths"].update(surface["paths"])
                components = document.setdefault("components", {})
                for category, definitions in surface["components"].items():
                    namespace = components.setdefault(category, {})
                    for name, definition in definitions.items():
                        if name in namespace and namespace[name] != definition:
                            raise ValueError("Conflicting API contract component")
                        namespace[name] = definition
            self._business_contracts_added = True
        return document


def _build_readiness(settings: Settings) -> ReadinessService:
    probes: list[ReadinessProbe] = [
        PostgresReadinessProbe(
            settings.database_url.get_secret_value(),
            timeout_seconds=settings.readiness_timeout_seconds,
        ),
        ObjectStorageReadinessProbe(
            settings.object_storage_healthcheck_url,
            timeout_seconds=settings.readiness_timeout_seconds,
        ),
    ]
    return ReadinessService(probes, timeout_seconds=settings.readiness_timeout_seconds)


def create_app(
    *,
    settings: Settings | None = None,
    readiness: ReadinessService | None = None,
    assets: AssetService | None = None,
    artifacts: ArtifactService | None = None,
    workspaces: WorkspaceService | None = None,
    profiles: ProfileDrafts | None = None,
    bindings: BindingDrafts | None = None,
    binding_defaults: BindingDefaultDrafts | None = None,
    binding_publication_facts: BindingPublicationFacts | None = None,
    pronunciations: PronunciationReader | None = None,
    catalogue: ModelCatalogue | None = None,
    jobs: PublicJobs | None = None,
    speech: SpeechSynthesis | None = None,
    transcriptions: Transcription | None = None,
) -> FastAPI:
    """Create an API instance with explicit dependencies for tests and deployment."""

    resolved_settings = settings or Settings()
    producer = (
        binding_defaults is not None
        or resolved_settings.binding_defaults_writer_database_url is not None
    )
    if producer and (
        bindings is not None or resolved_settings.binding_writer_database_url is not None
    ):
        raise ValueError("Configure one explicit binding writer mode")
    resolved_readiness = readiness or _build_readiness(resolved_settings)
    runtime = (
        AssetRuntime(resolved_settings)
        if assets is None and resolved_settings.object_storage_access_key is not None
        else None
    )
    asset_service = assets or (runtime.service if runtime is not None else None)
    workspace_service = workspaces or (runtime.workspaces if runtime is not None else None)
    artifact_runtime = (
        ArtifactRuntime(resolved_settings)
        if artifacts is None
        and resolved_settings.artifact_reader_database_url is not None
        and resolved_settings.object_storage_access_key is not None
        else None
    )

    def get_artifacts() -> ArtifactService | None:
        if artifacts is not None:
            return artifacts
        return artifact_runtime.service if artifact_runtime is not None else None

    profile_runtime = (
        ProfileRuntime(resolved_settings)
        if profiles is None and resolved_settings.profile_writer_database_url is not None
        else None
    )

    def get_profiles() -> ProfileDrafts | None:
        if profiles is not None:
            return profiles
        return profile_runtime.provider if profile_runtime is not None else None

    pronunciation_runtime = (
        PronunciationRuntime(resolved_settings)
        if pronunciations is None
        and resolved_settings.pronunciation_reader_database_url is not None
        else None
    )

    def get_pronunciations() -> PronunciationReader | None:
        if pronunciations is not None:
            return pronunciations
        return pronunciation_runtime.provider if pronunciation_runtime is not None else None

    catalogue_runtime = (
        ModelCatalogRuntime(resolved_settings)
        if catalogue is None and resolved_settings.model_catalog_reader_database_url is not None
        else None
    )

    def get_catalogue() -> ModelCatalogue | None:
        if catalogue is not None:
            return catalogue
        return catalogue_runtime.catalogue if catalogue_runtime is not None else None

    capability_policy = CapabilityPolicy(
        deployment_id=(
            f"{resolved_settings.environment}:{resolved_settings.service_name}:"
            f"{resolved_settings.database_schema}"
        ),
        workspace_id=resolved_settings.capability_default_workspace_id,
        policy_revision=resolved_settings.capability_policy_revision,
        max_audio_bytes=resolved_settings.asset_max_bytes,
        max_text_codepoints=resolved_settings.capability_max_text_codepoints,
        max_segment_ms=resolved_settings.capability_max_segment_ms,
        max_realtime_session_ms=resolved_settings.capability_max_realtime_session_ms,
        max_queue_wait_ms=resolved_settings.capability_max_queue_wait_ms,
    )

    job_runtime = (
        JobRuntime(resolved_settings)
        if jobs is None and resolved_settings.job_writer_database_url is not None
        else None
    )

    def get_jobs() -> PublicJobs | None:
        if jobs is not None:
            return jobs
        return job_runtime.jobs if job_runtime is not None else None

    binding_runtime = (
        BindingRuntime(resolved_settings)
        if bindings is None and resolved_settings.binding_writer_database_url is not None
        else None
    )
    binding_defaults_runtime = (
        BindingDefaultsRuntime(resolved_settings)
        if binding_defaults is None
        and resolved_settings.binding_defaults_writer_database_url is not None
        else None
    )

    def get_bindings() -> BindingDrafts | BindingDefaultDrafts | None:
        if producer:
            if binding_defaults is not None:
                return binding_defaults
            return (
                binding_defaults_runtime.provider if binding_defaults_runtime is not None else None
            )
        if bindings is not None:
            return bindings
        return binding_runtime.provider if binding_runtime is not None else None

    binding_admin_runtime = (
        BindingAdminReaderRuntime(resolved_settings)
        if binding_publication_facts is None
        and resolved_settings.binding_admin_reader_database_url is not None
        else None
    )

    def get_binding_publication_facts() -> BindingPublicationFacts | None:
        if binding_publication_facts is not None:
            return binding_publication_facts
        return binding_admin_runtime.provider if binding_admin_runtime is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as resources:
            if transcriptions is not None:
                resources.push_async_callback(run_in_threadpool, transcriptions.close)
            if catalogue_runtime is not None:
                resources.push_async_callback(run_in_threadpool, catalogue_runtime.close)
                await run_in_threadpool(catalogue_runtime.open)
            if pronunciation_runtime is not None:
                resources.push_async_callback(run_in_threadpool, pronunciation_runtime.close)
                await run_in_threadpool(pronunciation_runtime.open)
            if binding_defaults_runtime is not None:
                resources.push_async_callback(run_in_threadpool, binding_defaults_runtime.close)
                await run_in_threadpool(binding_defaults_runtime.open)
            if binding_runtime is not None:
                resources.push_async_callback(run_in_threadpool, binding_runtime.close)
                await run_in_threadpool(binding_runtime.open)
            if binding_admin_runtime is not None:
                resources.push_async_callback(run_in_threadpool, binding_admin_runtime.close)
                await run_in_threadpool(binding_admin_runtime.open)
            if job_runtime is not None:
                resources.push_async_callback(run_in_threadpool, job_runtime.close)
                await run_in_threadpool(job_runtime.open)
            if artifact_runtime is not None:
                resources.push_async_callback(run_in_threadpool, artifact_runtime.close)
                await run_in_threadpool(artifact_runtime.open)
            if profile_runtime is not None:
                resources.push_async_callback(run_in_threadpool, profile_runtime.close)
                await run_in_threadpool(profile_runtime.open)
            if runtime is not None:
                await run_in_threadpool(runtime.open)
                resources.push_async_callback(run_in_threadpool, runtime.close)
                # Replay a bounded original/ledger pass before accepting traffic.
                # Every subsequent read independently enforces persisted expiry.
                await run_in_threadpool(
                    runtime.service.maintain, resolved_settings.asset_maintenance_batch_size
                )
            yield

    app = _SpeechAPI(
        title="SpeechNexus operational, asset, workspace and draft API",
        version=__version__,
        description="Health, assets, workspace reads and immutable configuration drafts.",
        lifespan=lifespan,
    )
    app._binding_defaults_enabled = producer
    app.add_middleware(
        AssetIngress,
        max_bytes=resolved_settings.asset_max_bytes,
        concurrent_uploads=resolved_settings.asset_upload_concurrency,
        receive_timeout_seconds=resolved_settings.asset_receive_timeout_seconds,
        is_enabled=lambda: asset_service is not None,
        allowed_hosts=resolved_settings.asset_allowed_hosts,
        allowed_origins=resolved_settings.asset_allowed_origins,
    )
    app.add_middleware(
        WorkspaceIngress,
        is_enabled=lambda: workspace_service is not None,
        allowed_hosts=resolved_settings.asset_allowed_hosts,
        allowed_origins=resolved_settings.asset_allowed_origins,
    )
    app.add_middleware(
        ProfileIngress,
        max_bytes=resolved_settings.profile_receive_max_bytes,
        concurrency=resolved_settings.profile_receive_concurrency,
        receive_timeout_seconds=resolved_settings.profile_receive_timeout_seconds,
        is_enabled=lambda: get_profiles() is not None,
        allowed_hosts=resolved_settings.asset_allowed_hosts,
        allowed_origins=resolved_settings.asset_allowed_origins,
    )
    app.add_middleware(
        BindingIngress,
        producer=producer,
        max_bytes=resolved_settings.binding_receive_max_bytes,
        concurrency=resolved_settings.binding_receive_concurrency,
        receive_timeout_seconds=resolved_settings.binding_receive_timeout_seconds,
        is_enabled=lambda: get_bindings() is not None,
        allowed_hosts=resolved_settings.asset_allowed_hosts,
        allowed_origins=resolved_settings.asset_allowed_origins,
    )
    # Request IDs wrap early ingress errors as well as route responses.
    app.add_middleware(
        PronunciationIngress,
        concurrency=resolved_settings.pronunciation_read_concurrency,
        is_enabled=lambda: get_pronunciations() is not None,
        allowed_hosts=resolved_settings.asset_allowed_hosts,
        allowed_origins=resolved_settings.asset_allowed_origins,
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(create_asset_router(lambda: asset_service))
    app.include_router(create_artifact_router(get_artifacts))
    app.include_router(create_model_router(get_catalogue, capability_policy))
    app.include_router(
        create_job_router(
            get_jobs,
            max_bytes=resolved_settings.job_receive_max_bytes,
            receive_timeout_seconds=resolved_settings.job_receive_timeout_seconds,
        )
    )
    app.include_router(create_speech_router(lambda: speech))
    app.include_router(create_transcription_router(lambda: transcriptions))
    app.include_router(create_workspace_router(lambda: workspace_service))
    app.include_router(create_profile_router(get_profiles))
    app.include_router(create_pronunciation_router(get_pronunciations))
    app.include_router(
        create_binding_router(
            get_bindings,
            get_admin_reader=get_binding_publication_facts,
            producer=producer,
        )
    )

    @app.exception_handler(HTTPException)
    async def asset_http_error(request: Request, error: HTTPException) -> Response:
        path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
        if (
            path.startswith("/v1/nexus/assets/") or path == "/v1/nexus/assets"
        ) and error.status_code in (404, 405):
            code = "asset_not_found" if error.status_code == 404 else "asset_method_not_allowed"
            headers = (
                {"Allow": error.headers["Allow"]}
                if error.headers and "Allow" in error.headers
                else {}
            )
            return asset_error(code, request.state.request_id, headers=headers)
        if (
            path == ARTIFACT_PREFIX or path.startswith(ARTIFACT_PREFIX + "/")
        ) and error.status_code in (404, 405):
            code = (
                "artifact_method_not_allowed" if error.status_code == 405 else "artifact_not_found"
            )
            headers = (
                {"Allow": error.headers["Allow"]}
                if error.headers and "Allow" in error.headers
                else {}
            )
            return artifact_error(code, request.state.request_id, headers=headers)
        if (
            path == WORKSPACE_PREFIX or path.startswith(WORKSPACE_PREFIX + "/")
        ) and error.status_code in (404, 405):
            code = (
                "workspace_not_found"
                if error.status_code == 404
                else "workspace_method_not_allowed"
            )
            headers = (
                {"Allow": error.headers["Allow"]}
                if error.headers and "Allow" in error.headers
                else {}
            )
            return workspace_error(code, request.state.request_id, headers=headers)
        if (path == JOB_PREFIX or path.startswith(JOB_PREFIX + "/")) and error.status_code in (
            404,
            405,
        ):
            code = "job_not_found" if error.status_code == 404 else "job_method_not_allowed"
            headers = (
                {"Allow": error.headers["Allow"]}
                if error.headers and "Allow" in error.headers
                else {}
            )
            return job_error(code, request.state.request_id, headers=headers)
        if path == SPEECH_PREFIX and error.status_code in (404, 405):
            headers = (
                {"Allow": error.headers["Allow"]}
                if error.headers and "Allow" in error.headers
                else {}
            )
            return speech_error(
                "speech_method_not_allowed",
                request.state.request_id,
                headers=headers,
            )
        return await http_exception_handler(request, error)

    app.include_router(
        create_health_router(
            service_name=resolved_settings.service_name,
            version=__version__,
            readiness=resolved_readiness,
        )
    )
    app.state.settings = resolved_settings
    app.state.profile_runtime = profile_runtime
    app.state.pronunciation_runtime = pronunciation_runtime
    app.state.catalogue_runtime = catalogue_runtime
    app.state.binding_runtime = binding_runtime
    app.state.binding_defaults_runtime = binding_defaults_runtime
    app.state.binding_admin_runtime = binding_admin_runtime
    app.state.job_runtime = job_runtime
    app.state.artifact_runtime = artifact_runtime
    app.state.speech = speech
    app.state.transcriptions = transcriptions
    return app
