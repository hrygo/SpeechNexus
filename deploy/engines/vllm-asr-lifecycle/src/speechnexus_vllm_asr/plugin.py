"""Opt-in vLLM endpoint plugin. No new routes or engine access paths."""

from __future__ import annotations

import importlib
import json
from importlib.resources import files
from typing import Any

from .middleware import AsrScopeMiddleware
from .runtime_pin import verify_pin
from .scope import AsrScopeController


def verify_runtime() -> Any:
    """Fail startup before installing hooks if the native implementation differs."""
    pin = json.loads(files("speechnexus_vllm_asr").joinpath("runtime-pin.json").read_text())
    verify_pin(pin)
    native = importlib.import_module("vllm.v1.engine.async_llm")
    return native.AsyncLLM


class AsrScopePlugin:
    name = "speechnexus_asr_scope"
    required_tasks = ("transcription",)

    def attach_router(self, app: Any) -> None:
        app.add_middleware(AsrScopeMiddleware, root_app=app)

    async def init_state(self, engine_client: Any, state: Any, args: Any) -> None:
        native_type = verify_runtime()
        handler = getattr(state, "openai_serving_transcription", None)
        if (
            type(engine_client) is not native_type
            or handler is None
            or handler.engine_client is not engine_client
            or engine_client.log_requests
            or getattr(handler, "request_logger", None) is not None
        ):
            raise RuntimeError("ASR cleanup requires its native ASR serving and private logging")
        controller = AsrScopeController(engine_client)
        handler.engine_client = controller.engine
        state.speechnexus_asr_scopes = controller
