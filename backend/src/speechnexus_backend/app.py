"""ASGI composition root for the speech backend."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import Response

from speechnexus_backend import __version__
from speechnexus_backend.artifact_runtime import ArtifactRuntime
from speechnexus_backend.asset_runtime import AssetRuntime
from speechnexus_backend.binding_list_runtime import BindingListRuntime
from speechnexus_backend.binding_runtime import (
    BindingAdminReaderRuntime,
    BindingDefaultsRuntime,
    BindingLifecycleRuntime,
    BindingRuntime,
)
from speechnexus_backend.job_runtime import JobRuntime
from speechnexus_backend.model_catalog_runtime import ModelCatalogRuntime
from speechnexus_backend.profile_runtime import ProfileRuntime
from speechnexus_backend.pronunciation_runtime import (
    PronunciationPublisherRuntime,
    PronunciationRuntime,
    PronunciationWriterRuntime,
)
from speechnexus_backend.resource_pool_runtime import ResourcePoolRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.artifacts import ArtifactService
from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.application.binding_default_drafts import BindingDefaultDrafts
from speechnexus_backend.speech.application.binding_drafts import BindingDrafts
from speechnexus_backend.speech.application.binding_publication import (
    BindingPublicationFacts,
)
from speechnexus_backend.speech.application.binding_summaries import BindingSummaryService
from speechnexus_backend.speech.application.capability_snapshot import CapabilityPolicy
from speechnexus_backend.speech.application.job_cancellation import JobCancellationService
from speechnexus_backend.speech.application.profile_drafts import ProfileDrafts
from speechnexus_backend.speech.application.pronunciation_drafts import (
    PronunciationReader,
    PronunciationSets,
    PronunciationWriter,
)
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.readiness import (
    ReadinessProbe,
    ReadinessService,
)
from speechnexus_backend.speech.application.resource_pools import ResourcePoolService
from speechnexus_backend.speech.application.speech_synthesis import SpeechSynthesis
from speechnexus_backend.speech.application.transcription import Transcription
from speechnexus_backend.speech.application.voice_catalogue import VoiceCatalogue
from speechnexus_backend.speech.application.voice_drafts import VoiceDrafts
from speechnexus_backend.speech.application.voice_publications import VoicePublications
from speechnexus_backend.speech.application.voice_retirements import VoiceRetirements
from speechnexus_backend.speech.application.workspaces import WorkspaceService
from speechnexus_backend.speech.infrastructure.gateway_job_cancellation import (
    GatewayJobCancellationStopper,
)
from speechnexus_backend.speech.infrastructure.readiness import (
    ObjectStorageReadinessProbe,
    PostgresReadinessProbe,
)
from speechnexus_backend.speech.interfaces.http.artifact_contract import artifact_http_contract
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
from speechnexus_backend.speech.interfaces.http.binding_lifecycle import (
    BindingLifecycle,
    create_binding_lifecycle_router,
)
from speechnexus_backend.speech.interfaces.http.bindings import create_binding_router
from speechnexus_backend.speech.interfaces.http.health import create_health_router
from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from speechnexus_backend.speech.interfaces.http.jobs import PREFIX as JOB_PREFIX
from speechnexus_backend.speech.interfaces.http.jobs import create_job_router
from speechnexus_backend.speech.interfaces.http.method_errors import method_not_allowed_error
from speechnexus_backend.speech.interfaces.http.middleware import (
    RequestIdMiddleware,
    require_request_abort_capture,
)
from speechnexus_backend.speech.interfaces.http.model_bindings import (
    create_model_binding_list_router,
)
from speechnexus_backend.speech.interfaces.http.models import ModelCatalogue, create_model_router
from speechnexus_backend.speech.interfaces.http.profile_contract import profile_http_contract
from speechnexus_backend.speech.interfaces.http.profile_ingress import ProfileIngress
from speechnexus_backend.speech.interfaces.http.profiles import create_profile_router
from speechnexus_backend.speech.interfaces.http.pronunciation_sets import (
    create_pronunciation_sets_router,
    pronunciation_sets_http_contract,
)
from speechnexus_backend.speech.interfaces.http.pronunciations import (
    PronunciationIngress,
    create_pronunciation_router,
    pronunciation_http_contract,
)
from speechnexus_backend.speech.interfaces.http.resource_pools import (
    create_resource_pool_router,
)
from speechnexus_backend.speech.interfaces.http.speech import PREFIX as SPEECH_PREFIX
from speechnexus_backend.speech.interfaces.http.speech import create_speech_router
from speechnexus_backend.speech.interfaces.http.speech_errors import speech_error
from speechnexus_backend.speech.interfaces.http.transcriptions import (
    create_transcription_router,
)
from speechnexus_backend.speech.interfaces.http.unimplemented import (
    IMPLICIT_METHODS,
    assert_placeholder_ownership,
    create_unimplemented_router,
    is_unimplemented_path,
    methods_for_path,
)
from speechnexus_backend.speech.interfaces.http.voice_drafts import (
    create_voice_drafts_router,
    voice_drafts_http_contract,
)
from speechnexus_backend.speech.interfaces.http.voice_publications import (
    create_voice_publications_router,
)
from speechnexus_backend.speech.interfaces.http.voices import (
    create_voice_router,
    voice_http_contract,
    voice_retirement_http_contract,
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
from speechnexus_backend.workspace_writer_runtime import WorkspaceWriterRuntime


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
                artifact_http_contract(),
                workspace_http_contract(),
                profile_http_contract(),
                pronunciation_http_contract(),
                pronunciation_sets_http_contract(),
                voice_http_contract(),
                voice_drafts_http_contract(),
                voice_retirement_http_contract(),
                binding_http_contract(producer=self._binding_defaults_enabled),
            ):
                surface = copy.deepcopy(contract)
                # Surfaces may describe different methods of one path (for
                # example the binding list GET beside the draft POST). Merge the
                # methods instead of dropping an operation another surface owns.
                for path, item in surface["paths"].items():
                    document["paths"].setdefault(path, {}).update(item)
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
    resource_pools: ResourcePoolService | None = None,
    binding_summaries: BindingSummaryService | None = None,
    profiles: ProfileDrafts | None = None,
    bindings: BindingDrafts | None = None,
    binding_defaults: BindingDefaultDrafts | None = None,
    binding_publication_facts: BindingPublicationFacts | None = None,
    binding_lifecycle: BindingLifecycle | None = None,
    pronunciations: PronunciationReader | None = None,
    pronunciation_writer: PronunciationWriter | None = None,
    pronunciation_publisher: PronunciationWriter | None = None,
    catalogue: ModelCatalogue | None = None,
    voices: VoiceCatalogue | None = None,
    voice_drafts: VoiceDrafts | None = None,
    voice_publications: VoicePublications | None = None,
    voice_retirements: VoiceRetirements | None = None,
    jobs: PublicJobs | None = None,
    job_confirmation: JobCancellationService | None = None,
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
    workspace_writer_runtime = (
        WorkspaceWriterRuntime(resolved_settings)
        if workspaces is None
        and workspace_service is not None
        and resolved_settings.workspace_writer_database_url is not None
        else None
    )
    if workspace_writer_runtime is not None and workspace_service is not None:
        # Reads keep the asset role; only the two writes use the writer identity.
        workspace_service = workspace_service.with_writes(workspace_writer_runtime)
    resource_pool_runtime = (
        ResourcePoolRuntime(resolved_settings)
        if resource_pools is None
        and resolved_settings.resource_pool_reader_database_url is not None
        else None
    )

    def get_resource_pools() -> ResourcePoolService | None:
        if resource_pools is not None:
            return resource_pools
        return resource_pool_runtime.service if resource_pool_runtime is not None else None

    binding_list_runtime = (
        BindingListRuntime(resolved_settings)
        if binding_summaries is None
        and resolved_settings.binding_list_reader_database_url is not None
        else None
    )

    def get_binding_summaries() -> BindingSummaryService | None:
        if binding_summaries is not None:
            return binding_summaries
        return binding_list_runtime.service if binding_list_runtime is not None else None

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

    pronunciation_writer_runtime = (
        PronunciationWriterRuntime(resolved_settings)
        if pronunciation_writer is None
        and resolved_settings.pronunciation_writer_database_url is not None
        else None
    )

    def get_pronunciation_sets() -> PronunciationSets | None:
        if pronunciations is not None:
            # An explicit test port supplies the collection list too; a fake
            # that only implements the fixed GET answers 503 instead of being
            # silently substituted with the production reader.
            return cast(PronunciationSets, pronunciations)
        return pronunciation_runtime.provider if pronunciation_runtime is not None else None

    pronunciation_publisher_runtime = (
        PronunciationPublisherRuntime(resolved_settings)
        if pronunciation_publisher is None
        and resolved_settings.pronunciation_publisher_database_url is not None
        else None
    )

    def get_pronunciation_writer() -> PronunciationWriter | None:
        if pronunciation_writer is not None:
            return pronunciation_writer
        if pronunciation_writer_runtime is not None:
            return pronunciation_writer_runtime.provider
        # The publisher identity can also write drafts; a deployment that only
        # configures atomic publication still serves explicit draft requests.
        return (
            pronunciation_publisher_runtime.provider
            if pronunciation_publisher_runtime is not None
            else None
        )

    def get_pronunciation_publisher() -> PronunciationWriter | None:
        if pronunciation_publisher is not None:
            return pronunciation_publisher
        return (
            pronunciation_publisher_runtime.provider
            if pronunciation_publisher_runtime is not None
            else None
        )

    catalogue_runtime = (
        ModelCatalogRuntime(resolved_settings)
        if catalogue is None and resolved_settings.model_catalog_reader_database_url is not None
        else None
    )

    def get_catalogue() -> ModelCatalogue | None:
        if catalogue is not None:
            return catalogue
        return catalogue_runtime.catalogue if catalogue_runtime is not None else None

    def get_voices() -> VoiceCatalogue | None:
        if voices is not None:
            return voices
        return catalogue_runtime.voices if catalogue_runtime is not None else None

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

    def get_voice_drafts() -> VoiceDrafts | None:
        if voice_drafts is not None:
            return voice_drafts
        return job_runtime.voice_drafts if job_runtime is not None else None

    def get_voice_publications() -> VoicePublications | None:
        if voice_publications is not None:
            return voice_publications
        return job_runtime.voice_publications if job_runtime is not None else None

    def get_job_confirmation() -> JobCancellationService | None:
        if job_confirmation is not None:
            return job_confirmation
        jobs_service = get_jobs()
        if (
            jobs_service is None
            or resolved_settings.execution_gateway_base_url is None
            or resolved_settings.execution_gateway_component_credential is None
        ):
            return None
        return JobCancellationService(
            jobs_service, GatewayJobCancellationStopper(resolved_settings)
        )

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

    binding_lifecycle_runtime = (
        BindingLifecycleRuntime(resolved_settings)
        if binding_lifecycle is None
        and resolved_settings.binding_lifecycle_database_url is not None
        else None
    )

    def get_binding_lifecycle() -> BindingLifecycle | None:
        if binding_lifecycle is not None:
            return binding_lifecycle
        return binding_lifecycle_runtime.provider if binding_lifecycle_runtime is not None else None

    def get_voice_retirements() -> VoiceRetirements | None:
        if voice_retirements is not None:
            return voice_retirements
        return (
            binding_lifecycle_runtime.voice_retirements
            if binding_lifecycle_runtime is not None
            else None
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as resources:
            if speech is not None:
                resources.push_async_callback(run_in_threadpool, speech.close)
            if transcriptions is not None:
                resources.push_async_callback(run_in_threadpool, transcriptions.close)
            if catalogue_runtime is not None:
                resources.push_async_callback(run_in_threadpool, catalogue_runtime.close)
                await run_in_threadpool(catalogue_runtime.open)
            if resource_pool_runtime is not None:
                resources.push_async_callback(run_in_threadpool, resource_pool_runtime.close)
                await run_in_threadpool(resource_pool_runtime.open)
            if binding_list_runtime is not None:
                resources.push_async_callback(run_in_threadpool, binding_list_runtime.close)
                await run_in_threadpool(binding_list_runtime.open)
            if workspace_writer_runtime is not None:
                resources.push_async_callback(run_in_threadpool, workspace_writer_runtime.close)
                await run_in_threadpool(workspace_writer_runtime.open)
            if pronunciation_runtime is not None:
                resources.push_async_callback(run_in_threadpool, pronunciation_runtime.close)
                await run_in_threadpool(pronunciation_runtime.open)
            if pronunciation_writer_runtime is not None:
                resources.push_async_callback(run_in_threadpool, pronunciation_writer_runtime.close)
                await run_in_threadpool(pronunciation_writer_runtime.open)
            if pronunciation_publisher_runtime is not None:
                resources.push_async_callback(
                    run_in_threadpool, pronunciation_publisher_runtime.close
                )
                await run_in_threadpool(pronunciation_publisher_runtime.open)
            if binding_defaults_runtime is not None:
                resources.push_async_callback(run_in_threadpool, binding_defaults_runtime.close)
                await run_in_threadpool(binding_defaults_runtime.open)
            if binding_runtime is not None:
                resources.push_async_callback(run_in_threadpool, binding_runtime.close)
                await run_in_threadpool(binding_runtime.open)
            if binding_admin_runtime is not None:
                resources.push_async_callback(run_in_threadpool, binding_admin_runtime.close)
                await run_in_threadpool(binding_admin_runtime.open)
            if binding_lifecycle_runtime is not None:
                resources.push_async_callback(run_in_threadpool, binding_lifecycle_runtime.close)
                await run_in_threadpool(binding_lifecycle_runtime.open)
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
        is_enabled=lambda: (
            get_bindings() is not None
            or get_binding_lifecycle() is not None
            or get_binding_summaries() is not None
        ),
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
    app.include_router(create_voice_router(get_voices, get_voice_retirements))
    app.include_router(
        create_voice_drafts_router(
            get_voice_drafts,
            allowed_hosts=resolved_settings.asset_allowed_hosts,
            allowed_origins=resolved_settings.asset_allowed_origins,
        )
    )
    app.include_router(
        create_voice_publications_router(
            get_voice_publications,
            allowed_hosts=resolved_settings.asset_allowed_hosts,
            allowed_origins=resolved_settings.asset_allowed_origins,
        )
    )
    app.include_router(
        create_job_router(
            get_jobs,
            get_confirmation=get_job_confirmation,
            max_bytes=resolved_settings.job_receive_max_bytes,
            receive_timeout_seconds=resolved_settings.job_receive_timeout_seconds,
            event_heartbeat_seconds=resolved_settings.job_event_heartbeat_seconds,
            event_send_timeout_seconds=resolved_settings.job_event_send_timeout_seconds,
        )
    )
    app.include_router(create_speech_router(lambda: speech))
    app.include_router(create_transcription_router(lambda: transcriptions))
    app.include_router(create_workspace_router(lambda: workspace_service))
    app.include_router(create_resource_pool_router(get_resource_pools))
    app.include_router(create_model_binding_list_router(get_binding_summaries))
    app.include_router(create_profile_router(get_profiles))
    app.include_router(create_pronunciation_router(get_pronunciations))
    app.include_router(
        create_pronunciation_sets_router(
            get_pronunciation_sets,
            get_pronunciation_writer,
            get_pronunciation_publisher,
            max_bytes=resolved_settings.pronunciation_receive_max_bytes,
            receive_timeout_seconds=resolved_settings.pronunciation_receive_timeout_seconds,
        )
    )
    app.include_router(
        create_binding_router(
            get_bindings,
            get_admin_reader=get_binding_publication_facts,
            producer=producer,
        )
    )
    app.include_router(create_binding_lifecycle_router(get_binding_lifecycle))
    # Declared operations without an owning feature answer one stable 503
    # instead of a framework 404 that reads like "unknown resource".
    app.include_router(create_unimplemented_router())

    def allowed_methods(path: str, reported: str | None) -> dict[str, str]:
        """Merge the framework's Allow with every method registered on this path.

        One path can carry several implemented or placeholder methods, and
        Starlette's 405 header only names the first partially matching route.
        RFC 9110 wants all of
        them, so the registered methods for the same template are merged in.
        """
        methods = {item.strip().upper() for item in (reported or "").split(",") if item.strip()}
        methods.update(methods_for_path(app.router.routes, path))
        # Both sources are filtered, so an implicit HEAD can never leak in.
        methods -= IMPLICIT_METHODS
        return {"Allow": ", ".join(sorted(methods))} if methods else {}

    @app.exception_handler(HTTPException)
    async def asset_http_error(request: Request, error: HTTPException) -> Response:
        path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
        if error.status_code == 405 and is_unimplemented_path(path):
            reported = (
                error.headers["Allow"] if error.headers and "Allow" in error.headers else None
            )
            headers = allowed_methods(path, reported)
            if path == JOB_PREFIX or path.startswith(JOB_PREFIX + "/"):
                return job_error(
                    "job_method_not_allowed", request.state.request_id, headers=headers
                )
            return method_not_allowed_error(request.state.request_id, headers)
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
            reported = (
                error.headers["Allow"] if error.headers and "Allow" in error.headers else None
            )
            # The workspace surface now serves GET and POST at one template;
            # Starlette names only the first partial route, so merge them all.
            headers = allowed_methods(path, reported) if error.status_code == 405 else {}
            return workspace_error(code, request.state.request_id, headers=headers)
        if (path == JOB_PREFIX or path.startswith(JOB_PREFIX + "/")) and error.status_code in (
            404,
            405,
        ):
            code = "job_not_found" if error.status_code == 404 else "job_method_not_allowed"
            reported = (
                error.headers["Allow"] if error.headers and "Allow" in error.headers else None
            )
            headers = allowed_methods(path, reported) if error.status_code == 405 else {}
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
        if error.status_code == 405:
            # Every registered route answers a wrong method with the public
            # envelope. The branches above choose a narrower code for their
            # surface; anything else - including a route added later - falls
            # back here instead of FastAPI's bare {"detail": ...}. Unregistered
            # paths never reach this handler: they answer 404, so the widened
            # fallback cannot advertise methods for an unknown resource.
            reported = (
                error.headers["Allow"] if error.headers and "Allow" in error.headers else None
            )
            return method_not_allowed_error(
                request.state.request_id, allowed_methods(path, reported)
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
    app.state.pronunciation_writer_runtime = pronunciation_writer_runtime
    app.state.pronunciation_publisher_runtime = pronunciation_publisher_runtime
    app.state.catalogue_runtime = catalogue_runtime
    app.state.binding_runtime = binding_runtime
    app.state.binding_defaults_runtime = binding_defaults_runtime
    app.state.binding_admin_runtime = binding_admin_runtime
    app.state.job_runtime = job_runtime
    app.state.artifact_runtime = artifact_runtime
    app.state.speech = speech
    app.state.transcriptions = transcriptions
    require_request_abort_capture(app)
    # Checked last, once every router is mounted: ordering is load-bearing, and
    # a feature router included after the placeholders would be shadowed by them
    # and answer 503 for an operation that ships.
    assert_placeholder_ownership(app.router.routes)
    return app
