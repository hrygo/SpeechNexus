"""Run in the pinned image after installing the overlay wheel, with no model or GPU.

Actual vendor classes and its entry-point loader are used. IPC collaborators are
controlled, so this proves installation/native-method integration, not real stop.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from contextlib import suppress
from types import SimpleNamespace

from fastapi import FastAPI
from speechnexus_vllm_asr.scope import CURRENT_SCOPE
from vllm.plugins import load_endpoint_plugins
from vllm.sampling_params import SamplingParams
from vllm.v1.engine import EngineCoreRequest
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.v1.engine.input_processor import InputProcessor
from vllm.v1.engine.output_processor import OutputProcessor


async def probe(samples: int) -> dict:
    plugins = load_endpoint_plugins(("transcription",))
    selected = [plugin for plugin in plugins if plugin.name == "speechnexus_asr_scope"]
    assert len(selected) == 1
    plugin = selected[0]
    app = FastAPI()
    paths = [route.path for route in app.routes]
    plugin.attach_router(app)
    assert [route.path for route in app.routes] == paths
    assert any(item.cls.__name__ == "AsrScopeMiddleware" for item in app.user_middleware)
    entered = asyncio.Event()
    allow_reply = asyncio.Event()
    registered = []
    collectors = []
    collector_tasks = []
    aborts = []
    frontend = OutputProcessor.__new__(OutputProcessor)
    frontend.request_states = {}
    frontend.external_req_ids = defaultdict(list)
    frontend.parent_requests = {}
    frontend.lora_states = SimpleNamespace(request_finished=lambda *args: None)

    def frontend_add(request, prompt, parent, index, queue):
        registered.append(request.request_id)
        collectors.append(queue)
        if queue._input_stream_task is None:
            queue._input_stream_task = asyncio.create_task(asyncio.Event().wait())
            collector_tasks.append(queue._input_stream_task)
        frontend.request_states[request.request_id] = SimpleNamespace(
            external_req_id=request.external_req_id,
            lora_name=None,
            queue=queue,
            detokenizer=object(),
            make_request_output=lambda **kwargs: None,
        )
        frontend.external_req_ids[request.external_req_id].append(request.request_id)
        if parent is not None:
            frontend.parent_requests[parent.request_id] = parent

    async def core_add(request):
        entered.set()
        await allow_reply.wait()

    async def core_abort(identities):
        aborts.extend(identities)

    engine = AsyncLLM.__new__(AsyncLLM)
    engine.output_handler = None
    engine.log_requests = False
    engine.vllm_config = SimpleNamespace(
        cache_config=SimpleNamespace(kv_sharing_fast_prefill=False)
    )
    engine.input_processor = SimpleNamespace(assign_request_id=InputProcessor.assign_request_id)
    engine._run_output_handler = lambda: None
    frontend.add_request = frontend_add
    engine.output_processor = frontend
    engine.engine_core = SimpleNamespace(
        resources=SimpleNamespace(engine_dead=False),
        add_request_async=core_add,
        abort_requests_async=core_abort,
        shutdown=lambda **kwargs: None,
    )
    state = SimpleNamespace(openai_serving_transcription=SimpleNamespace(engine_client=engine))
    await plugin.init_state(engine, state, SimpleNamespace())
    controller = state.speechnexus_asr_scopes
    scope = controller.open()
    params = SamplingParams(n=samples, max_tokens=1)
    request = EngineCoreRequest(
        request_id="transcribe-synthetic",
        prompt_token_ids=[1],
        mm_features=None,
        sampling_params=params,
        pooling_params=None,
        arrival_time=0.0,
        lora_request=None,
        cache_salt=None,
        data_parallel_rank=None,
    )

    async def consume():
        async for _ in state.openai_serving_transcription.engine_client.generate(
            request, params, "transcribe-synthetic"
        ):
            raise AssertionError("the controlled pending add must not produce a transcript")

    token = CURRENT_SCOPE.set(scope)
    try:
        consumer = asyncio.create_task(consume())
    finally:
        CURRENT_SCOPE.reset(token)
    await asyncio.wait_for(entered.wait(), 5)
    consumer.cancel()
    with suppress(asyncio.CancelledError):
        await consumer
    if samples == 1:
        # An already removed frontend map must not suppress the final core abort.
        frontend.request_states.clear()
        frontend.external_req_ids.clear()
    controller.seal(scope)
    assert not await controller.wait_closed(wait_timeout_seconds=0.01)
    assert not aborts
    allow_reply.set()
    assert await controller.wait_closed(wait_timeout_seconds=5)
    assert len(registered) == 1
    assert registered[0] != "transcribe-synthetic"
    assert aborts == registered
    assert collectors[0]._input_stream_task is None
    await asyncio.gather(*collector_tasks, return_exceptions=True)
    assert all(task.cancelled() for task in collector_tasks)
    assert not frontend.request_states
    assert not frontend.external_req_ids
    assert not frontend.parent_requests
    return {
        "native_n": samples,
        "submitted_children": len(registered),
        "actual_native_parent_and_frontend_cleanup": True,
        "actual_native_collector_close_observed": True,
        "pending_scopes_after_cleanup": controller.pending_scopes,
    }


async def main() -> None:
    allowlist = os.environ.pop("VLLM_PLUGINS")
    try:
        assert load_endpoint_plugins(("transcription",)) == []
    finally:
        os.environ["VLLM_PLUGINS"] = allowlist
    assert load_endpoint_plugins(("render",)) == []
    results = [await probe(1), await probe(2)]
    print(
        json.dumps(
            {
                "installed_entry_point_loaded": True,
                "native_runtime_pin_verified": True,
                "no_new_routes_and_asgi_middleware_attached": True,
                "explicit_allowlist_and_transcription_task_required": True,
                "actual_native_generate_add_and_collector": True,
                "cancelled_add_waited_before_abort": True,
                "removed_frontend_map_did_not_drop_core_abort": True,
                "partial_native_fanout_did_not_send_late_child": True,
                "cases": results,
                "model_loaded": False,
                "gpu_used": False,
                "real_ipc_or_stop_qualification": False,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
