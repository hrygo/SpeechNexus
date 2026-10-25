"""Pinned-image installed-wheel probes, using controlled IPC and no model or GPU."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from contextlib import suppress
from types import SimpleNamespace

from fastapi import FastAPI
from speechnexus_vllm_asr.scope import CURRENT_SCOPE
from vllm.entrypoints.speech_to_text.base.serving import SpeechToTextBaseServing
from vllm.outputs import CompletionOutput, RequestOutput
from vllm.plugins import load_endpoint_plugins
from vllm.sampling_params import SamplingParams
from vllm.v1.engine import EngineCoreRequest
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.v1.engine.input_processor import InputProcessor
from vllm.v1.engine.output_processor import OutputProcessor


def core_request(external_id: str, samples: int = 1):
    return EngineCoreRequest(
        request_id=external_id,
        prompt_token_ids=[1],
        mm_features=None,
        sampling_params=SamplingParams(n=samples, max_tokens=1),
        pooling_params=None,
        arrival_time=0.0,
        lora_request=None,
        cache_salt=None,
        data_parallel_rank=None,
    )


async def install_runtime(core_add):
    selected = [
        plugin
        for plugin in load_endpoint_plugins(("transcription",))
        if plugin.name == "speechnexus_asr_scope"
    ]
    assert len(selected) == 1
    plugin = selected[0]
    app = FastAPI()
    paths = [route.path for route in app.routes]
    plugin.attach_router(app)
    assert [route.path for route in app.routes] == paths
    assert any(item.cls.__name__ == "AsrScopeMiddleware" for item in app.user_middleware)
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
    return SimpleNamespace(
        controller=state.speechnexus_asr_scopes,
        proxy=state.openai_serving_transcription.engine_client,
        frontend=frontend,
        registered=registered,
        collectors=collectors,
        collector_tasks=collector_tasks,
        aborts=aborts,
    )


async def probe_pending_add(samples: int) -> dict:
    entered = asyncio.Event()
    allow_reply = asyncio.Event()

    async def core_add(request):
        entered.set()
        await allow_reply.wait()

    runtime = await install_runtime(core_add)
    controller = runtime.controller
    scope = controller.open()
    request = core_request("transcribe-synthetic", samples)

    async def consume():
        async for _ in runtime.proxy.generate(request, request.params, "transcribe-synthetic"):
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
        runtime.frontend.request_states.clear()
        runtime.frontend.external_req_ids.clear()
    controller.seal(scope)
    assert not await controller.wait_closed(wait_timeout_seconds=0.01)
    assert not runtime.aborts
    allow_reply.set()
    assert await controller.wait_closed(wait_timeout_seconds=5)
    assert len(runtime.registered) == 1
    assert runtime.registered[0] != "transcribe-synthetic"
    assert runtime.aborts == runtime.registered
    assert runtime.collectors[0]._input_stream_task is None
    await asyncio.gather(*runtime.collector_tasks, return_exceptions=True)
    assert all(task.cancelled() for task in runtime.collector_tasks)
    assert not runtime.frontend.request_states
    assert not runtime.frontend.external_req_ids
    assert not runtime.frontend.parent_requests
    return {
        "native_n": samples,
        "submitted_children": len(runtime.registered),
        "actual_native_parent_and_frontend_cleanup": True,
        "actual_native_collector_close_observed": True,
        "pending_scopes_after_cleanup": controller.pending_scopes,
    }


async def probe_duplicate_external_ids() -> dict:
    accepted = asyncio.Queue()
    gates = {}

    async def core_add(request):
        gates[request.request_id] = asyncio.Event()
        accepted.put_nowait(request.request_id)
        await gates[request.request_id].wait()

    runtime = await install_runtime(core_add)
    controller = runtime.controller
    external_id = "same-external-language-request"
    abort_entered = asyncio.Event()
    original_abort = runtime.proxy.abort

    async def observed_abort(*args, **kwargs):
        abort_entered.set()
        await original_abort(*args, **kwargs)

    runtime.proxy.abort = observed_abort
    # Execute the actual native serving cancellation handler, not a copied AST
    # branch or a facade-only call. Only model prompt/parse and IPC are controlled.
    serving = SimpleNamespace(
        engine_client=runtime.proxy,
        asr_config=SimpleNamespace(),
        tokenizer=SimpleNamespace(),
        model_cls=SimpleNamespace(
            get_language_detection_prompt=lambda *args: core_request(external_id),
            get_language_token_ids=lambda *args: [1],
            parse_language_detection_output=lambda tokens, tokenizer: "zh" if tokens == [1] else "",
        ),
    )
    scopes = [controller.open(), controller.open()]
    tasks = []
    owned = []
    for scope in scopes:
        token = CURRENT_SCOPE.set(scope)
        try:
            tasks.append(
                asyncio.create_task(
                    SpeechToTextBaseServing._detect_language(serving, object(), external_id)
                )
            )
        finally:
            CURRENT_SCOPE.reset(token)
        owned.append(await asyncio.wait_for(accepted.get(), 5))
    try:
        assert owned[0] != owned[1]
        tasks[0].cancel()
        await asyncio.wait_for(abort_entered.wait(), 5)
        if set(runtime.aborts) - {owned[0]}:
            print(json.dumps({"duplicate_external_id_aborted_other_scope": True}), flush=True)
        assert not runtime.aborts, "serving abort overtook an unresolved native add"
        gates[owned[0]].set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(tasks[0], 5)
        assert set(runtime.aborts) == {owned[0]}, "duplicate external ID crossed request scope"
        assert not tasks[1].done() and not gates[owned[1]].is_set()
        assert runtime.frontend.external_req_ids[external_id] == [owned[1]]
        token = CURRENT_SCOPE.set(scopes[0])
        try:
            await runtime.proxy.abort(external_id)
            await runtime.proxy.abort(owned[1], internal=True)
        finally:
            CURRENT_SCOPE.reset(token)
        assert set(runtime.aborts) == {owned[0]}
        gates[owned[1]].set()
        runtime.collectors[1].put(
            RequestOutput(
                request_id=external_id,
                prompt=None,
                prompt_token_ids=[1],
                prompt_logprobs=None,
                outputs=[
                    CompletionOutput(
                        index=0,
                        text="synthetic",
                        token_ids=[1],
                        cumulative_logprob=None,
                        logprobs=None,
                        finish_reason="stop",
                    )
                ],
                finished=True,
            )
        )
        assert await asyncio.wait_for(tasks[1], 5) == "zh"
    finally:
        for gate in gates.values():
            gate.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        for scope in scopes:
            controller.seal(scope)
        await asyncio.gather(*tasks, return_exceptions=True)
        await controller.wait_closed(wait_timeout_seconds=5)
    assert controller.pending_scopes == 0
    return {
        "actual_native_serving_cancel_handler": True,
        "duplicate_external_id_accepted_without_rewrite": True,
        "abort_waited_only_own_add": True,
        "other_scope_completed_native_language_detection": True,
        "repeated_abort_and_foreign_internal_id_confined": True,
    }


async def main() -> None:
    allowlist = os.environ.pop("VLLM_PLUGINS")
    try:
        assert load_endpoint_plugins(("transcription",)) == []
    finally:
        os.environ["VLLM_PLUGINS"] = allowlist
    assert load_endpoint_plugins(("render",)) == []
    results = [await probe_pending_add(1), await probe_pending_add(2)]
    isolation = await probe_duplicate_external_ids()
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
                "cross_scope_isolation": isolation,
                "model_loaded": False,
                "gpu_used": False,
                "real_ipc_or_stop_qualification": False,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
