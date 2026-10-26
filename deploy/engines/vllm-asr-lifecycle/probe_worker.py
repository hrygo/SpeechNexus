"""Installed-wheel native worker probes with controlled metadata and CUDA events.

No model, GPU device, real transport or budget-release qualification is exercised.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from importlib.metadata import version
from types import SimpleNamespace

import torch
from speechnexus_vllm_asr.runtime_pin import verify_pin
from speechnexus_vllm_asr.worker_fence import (
    WorkerFenceExtension,
    validate_request,
    validate_response,
)
from vllm.v1.executor.multiproc_executor import WorkerProc
from vllm.v1.executor.uniproc_executor import AsyncOutputFuture
from vllm.v1.outputs import ModelRunnerOutput
from vllm.v1.worker import gpu_model_runner, worker_base
from vllm.v1.worker.gpu_input_batch import InputBatch
from vllm.v1.worker.gpu_model_runner import AsyncGPUModelRunnerOutput, GPUModelRunner
from vllm.v1.worker.gpu_worker import Worker
from vllm.v1.worker.worker_base import WorkerWrapperBase

NONCE = "a" * 32


def native_batch():
    batch = InputBatch.__new__(InputBatch)
    batch.req_id_to_index = {"owned-internal": 0, "other-internal": 1}
    batch._req_ids = ["owned-internal", "other-internal"]
    batch.req_output_token_ids = [[], [7]]
    batch.spec_token_ids = [[], []]
    batch.block_table = SimpleNamespace(clear_row=lambda index: None)
    batch.batch_update_builder = SimpleNamespace(removed_append=lambda index: None)
    batch.request_lora_mapping = [0, 0]
    batch.is_pooling_model = False
    for name in (
        "greedy_reqs",
        "random_reqs",
        "top_p_reqs",
        "top_k_reqs",
        "frequency_penalties_reqs",
        "presence_penalties_reqs",
        "repetition_penalties_reqs",
        "has_allowed_token_ids",
        "thinking_token_budget_reqs",
    ):
        setattr(batch, name, set())
    for name in ("generators", "num_logprobs", "logprob_token_ids", "bad_words_token_ids"):
        setattr(batch, name, {})
    batch.prev_req_id_to_index = None
    batch.allowed_token_ids_mask_cpu_tensor = None
    # Storage/metadata collaborators do not initialize model tensors or a device.
    batch.condense = lambda: None
    batch.refresh_metadata = lambda: None
    return batch


class ProbeWorker(Worker):
    """Retain the native methods; constructor metadata is explicitly synthetic."""

    def __init__(self, **kwargs):
        self.rank = 0
        self.device = torch.device("cuda:0")
        self.use_v2_model_runner = False
        self._pp_send_work = []
        runner = GPUModelRunner.__new__(GPUModelRunner)
        runner.requests = {"owned-internal": object(), "other-internal": object()}
        runner.input_batch = native_batch()
        runner.num_prompt_logprobs = {"owned-internal": object()}
        runner.execute_model_state = None
        runner.speculative_config = None
        runner.use_async_spec_decode = False
        runner.encoder_cache = {"owned-mm": object(), "other-mm": object()}
        runner.late_interaction_runner = SimpleNamespace(
            on_requests_finished=lambda identities: None
        )
        runner._may_reorder_batch = lambda output: None
        self.model_runner = runner


def native_worker_observation():
    config = SimpleNamespace(
        enable_trace_function_call_for_thread=lambda: None,
        parallel_config=SimpleNamespace(
            worker_cls="__main__.ProbeWorker",
            worker_extension_cls="speechnexus_vllm_asr.worker_fence.WorkerFenceExtension",
        ),
        model_config=SimpleNamespace(multimodal_config=None),
    )
    wrapper = WorkerWrapperBase()
    original_context = worker_base.set_current_vllm_config
    before = ProbeWorker.execute_model
    try:
        worker_base.set_current_vllm_config = lambda config: nullcontext()
        wrapper.init_worker([{"vllm_config": config}])
    finally:
        worker_base.set_current_vllm_config = original_context
    assert WorkerFenceExtension in ProbeWorker.__bases__
    assert ProbeWorker.execute_model is before
    worker = wrapper.worker
    document = {"protocol": "worker-fence.v1", "nonce": NONCE, "request_ids": ["owned-internal"]}
    nonce, _, digest = validate_request(document)
    syncs = []
    original_sync = torch.cuda.synchronize
    original_pp = gpu_model_runner.get_pp_group
    try:
        torch.cuda.synchronize = lambda device: syncs.append(str(device))
        pending = wrapper.speechnexus_observe_worker_fence(document)
        assert pending["state"] == "pending" and not syncs
        assert pending["request_state_entries"] == pending["persistent_batch_entries"] == 1
        # Exact native update/remove methods, with storage/PP collaborators controlled.
        gpu_model_runner.get_pp_group = lambda: SimpleNamespace(is_last_rank=True)
        output = SimpleNamespace(
            finished_req_ids={"owned-internal"},
            new_block_ids_to_zero=[],
            kv_cache_block_copies=[],
            free_encoder_mm_hashes=["owned-mm"],
            num_scheduled_tokens={"other-internal": 1},
            scheduled_cached_reqs=SimpleNamespace(req_ids=[], resumed_req_ids=set()),
            scheduled_new_reqs=[],
            scheduled_spec_decode_tokens={},
        )
        GPUModelRunner._update_states(worker.model_runner, output)
        observed = wrapper.speechnexus_observe_worker_fence(document)
        validate_response(observed, nonce=nonce, request_set_digest=digest)
        assert observed["state"] == "observed" and syncs == ["cuda:0"]
        assert observed["release_evidence"] is False
        assert "other-internal" in worker.model_runner.requests
        assert worker.model_runner.input_batch.req_id_to_index == {"other-internal": 1}
        assert set(worker.model_runner.encoder_cache) == {"other-mm"}
    finally:
        torch.cuda.synchronize = original_sync
        gpu_model_runner.get_pp_group = original_pp
    return {
        "official_worker_extension_injected": True,
        "native_execute_model_preserved": True,
        "before_finished_notification": pending,
        "after_native_state_update": observed,
        "other_request_and_encoder_entry_retained": True,
        "cuda_synchronizer": "controlled callable, no GPU",
    }


def async_output(events, *, fail=False):
    output = AsyncGPUModelRunnerOutput.__new__(AsyncGPUModelRunnerOutput)
    output._model_runner_output = ModelRunnerOutput(
        req_ids=["synthetic-internal"], req_id_to_index={"synthetic-internal": 0}
    )
    output._sampled_token_ids = object()
    output._logprobs_tensors = None
    output._logprobs_tensors_cpu = None
    output._routed_experts = None
    output._routed_experts_cpu = None
    output._num_nans = None
    output._num_nans_cpu = None
    output._has_fault = None
    output._invalid_req_indices = []
    output.sampled_token_ids_cpu = SimpleNamespace(shape=(1, 1), tolist=lambda: [[7]])

    def synchronize():
        assert hasattr(output, "_sampled_token_ids")
        events.append("event-synchronized")
        if fail:
            raise RuntimeError("synthetic event failure")

    output.async_copy_ready_event = SimpleNamespace(synchronize=synchronize)
    return output


def native_output_completion():
    events = []
    output = async_output(events)
    future = AsyncOutputFuture(output, single_value=True)
    assert not future.done() and not events
    result = future.result()
    assert future.done() and events == ["event-synchronized"]
    assert not hasattr(output, "_sampled_token_ids")
    assert result.sampled_token_ids == [[7]]
    assert future.result() is result and len(events) == 1

    failed_events = []
    failed = AsyncOutputFuture(async_output(failed_events, fail=True), single_value=True)
    try:
        failed.result()
    except RuntimeError as error:
        assert str(error) == "synthetic event failure"
    else:
        raise AssertionError("native future accepted a failed event")
    assert failed.done() and failed.exception() is not None

    multiproc_events = []
    messages = []
    process = SimpleNamespace(
        worker_response_mq=SimpleNamespace(enqueue=lambda value: messages.append(value))
    )
    WorkerProc.enqueue_output(process, async_output(multiproc_events))
    assert multiproc_events == ["event-synchronized"]
    status, message = messages.pop()
    assert status is WorkerProc.ResponseStatus.SUCCESS and message.sampled_token_ids == [[7]]
    return {
        "native_future_consumes_event_before_success": True,
        "native_future_releases_output_tensor_reference_after_event": True,
        "native_future_caches_result": True,
        "event_failure_is_future_failure": True,
        "multiprocess_response_waits_for_native_output": True,
        "events_and_device_tensors": "controlled collaborators, not actual CUDA",
    }


if __name__ == "__main__":
    from importlib.resources import files

    pin = json.loads(files("speechnexus_vllm_asr").joinpath("worker-pin.json").read_text())
    verify_pin(pin)
    print(
        json.dumps(
            {
                "qualification": "installed wheel and pinned native methods; no model/GPU/real IPC",
                "version": pin["version"],
                "commit": pin["commit"],
                "verified_source_files": len(pin["source_sha256"]),
                "jsonschema_version": version("jsonschema"),
                "worker": native_worker_observation(),
                "output": native_output_completion(),
            }
        )
    )
