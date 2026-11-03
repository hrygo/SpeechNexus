"""Authenticated observation of the configuration actually installed in the worker."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from speechnexus_contracts.execution import parse

from speechnexus_gateway.application.tts_configuration_evidence import TtsConfigurationEvidence
from speechnexus_gateway.interfaces.http.control_errors import control_response
from speechnexus_gateway.interfaces.http.control_schemas import wire_schema
from speechnexus_gateway.interfaces.http.control_security import ComponentCredential


def create_tts_configuration_router(*, credential: ComponentCredential) -> APIRouter:
    router = APIRouter(tags=["execution-control"], dependencies=[Depends(credential)])
    headers = {"X-Request-ID": {"schema": {"type": "string"}}}
    errors: dict[int | str, dict[str, Any]] = {
        code: {
            "description": description,
            "headers": headers,
            "content": {
                "application/json": {"schema": wire_schema("ExecutionControlErrorResponse")}
            },
        }
        for code, description in {
            401: "Invalid component credential.",
            503: "No configured TTS worker is running.",
        }.items()
    }

    @router.get(
        "/internal/v1/tts-configuration-evidence",
        operation_id="getTtsConfigurationEvidence",
        summary="Read the loaded TTS configuration snapshot from the running gateway",
        responses={
            200: {
                "description": "Loaded configuration.",
                "headers": headers,
                "content": {
                    "application/json": {"schema": wire_schema("TtsConfigurationEvidence")}
                },
            },
            **errors,
        },
    )
    async def get_tts_configuration_evidence(
        request: Request,
        _request_id: Annotated[
            str | None, Header(alias="X-Request-ID", min_length=1, max_length=128)
        ] = None,
    ) -> JSONResponse:
        configuration = request.app.state.tts_configuration_evidence
        runtime = request.app.state.file_execution_runtime
        if (
            not isinstance(configuration, TtsConfigurationEvidence)
            or runtime is None
            or not runtime.running
        ):
            return control_response(
                "control_store_unavailable",
                "TTS configuration evidence is unavailable.",
                503,
                "retry_transaction",
            )
        return JSONResponse(parse("TtsConfigurationEvidence", configuration.observe()))

    return router
