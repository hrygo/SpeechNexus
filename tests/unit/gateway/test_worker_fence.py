"""Worker observations preserve uncertainty and never free another request."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
NONCE = "a" * 32


@pytest.fixture
def fence_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(ROOT / "deploy/engines/vllm-asr-lifecycle/src"))
    return importlib.import_module("speechnexus_vllm_asr.worker_fence")


@pytest.fixture
def worker(fence_module: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    class NativeRunner:
        pass

    runner: Any = NativeRunner()
    runner.requests = {"other-request": object()}
    runner.input_batch = SimpleNamespace(req_id_to_index={"other-request": 0})
    runner.num_prompt_logprobs = {"other-request": object()}
    runner.execute_model_state = None
    result = fence_module.WorkerFenceExtension()
    result.rank = 2
    result.model_runner = runner
    result.use_v2_model_runner = False
    result.device = SimpleNamespace(type="cuda", index=2)
    result._pp_send_work = []
    result.sync_devices = []
    result.actual_runtime_check = fence_module._worker_runtime
    monkeypatch.setattr(fence_module, "_worker_runtime", lambda target: True)
    original_import = importlib.import_module
    torch = SimpleNamespace(cuda=SimpleNamespace(synchronize=result.sync_devices.append))
    monkeypatch.setattr(
        fence_module.importlib,
        "import_module",
        lambda name: torch if name == "torch" else original_import(name),
    )
    result.synthetic_torch = torch
    return result


def request(*request_ids: str) -> dict[str, Any]:
    return {"protocol": "worker-fence.v1", "nonce": NONCE, "request_ids": list(request_ids)}


def observe(worker: Any, *request_ids: str) -> dict[str, Any]:
    response = worker.speechnexus_observe_worker_fence(request(*request_ids))
    assert isinstance(response, dict)
    return response


def test_observation_syncs_actual_worker_device_and_keeps_other_request(
    fence_module: Any, worker: Any
) -> None:
    other = worker.model_runner.requests["other-request"]
    response = observe(worker, "original-internal")
    nonce, _, digest = fence_module.validate_request(request("original-internal"))
    fence_module.validate_response(response, nonce=nonce, request_set_digest=digest)
    assert response["state"] == "observed"
    assert response["worker_rank"] == 2
    assert response["cuda_synchronized"] is True
    assert response["release_evidence"] is False
    assert worker.sync_devices == [worker.device]
    assert worker.model_runner.requests["other-request"] is other
    assert worker.model_runner.input_batch.req_id_to_index == {"other-request": 0}
    assert "original-internal" not in json.dumps(response)


@pytest.mark.parametrize(
    "pending",
    ["request", "batch", "prompt_logprob", "execution_step", "transport"],
)
def test_unconsumed_native_state_stays_pending_without_gpu_sync(worker: Any, pending: str) -> None:
    runner = worker.model_runner
    if pending == "request":
        runner.requests["original-internal"] = object()
    elif pending == "batch":
        runner.input_batch.req_id_to_index["original-internal"] = 1
    elif pending == "prompt_logprob":
        runner.num_prompt_logprobs["original-internal"] = object()
    elif pending == "execution_step":
        runner.execute_model_state = object()
    else:
        worker._pp_send_work.append(object())
    response = observe(worker, "original-internal")
    assert response["state"] == "pending"
    assert response["cuda_synchronized"] is False
    assert response["release_evidence"] is False
    assert worker.sync_devices == []


def test_sync_failure_does_not_expose_vendor_error_or_keep_partial_success(worker: Any) -> None:
    def fail(device: Any) -> None:
        raise RuntimeError("/synthetic/private-model: synthetic vendor failure")

    worker.synthetic_torch.cuda.synchronize = fail
    response = observe(worker, "original-internal")
    assert response["state"] == "failed"
    assert response["cuda_synchronized"] is False
    assert response["request_state_entries"] is None
    assert response["release_evidence"] is False
    assert "private-model" not in json.dumps(response)
    assert "failure" not in json.dumps(response)


def test_state_that_appears_during_synchronization_invalidates_pre_fence_snapshot(
    worker: Any,
) -> None:
    def changed(device: Any) -> None:
        worker.model_runner.requests["original-internal"] = object()

    worker.synthetic_torch.cuda.synchronize = changed
    response = observe(worker, "original-internal")
    assert response["state"] == "pending"
    assert response["request_state_entries"] == 1
    assert response["cuda_synchronized"] is False
    assert response["release_evidence"] is False


def test_unknown_id_is_only_worker_absence_and_cannot_parse_as_stop_evidence(worker: Any) -> None:
    response = observe(worker, "never-submitted")
    assert response["state"] == "observed"
    assert response["release_evidence"] is False
    shared = json.loads(
        (ROOT / "contracts/internal/coordination-transactions.schema.json").read_text()
    )
    stop_validator = Draft202012Validator(
        {"$ref": "#/$defs/VerifiedStopEvidence", "$defs": shared["$defs"]}
    )
    assert not stop_validator.is_valid(response)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"protocol": "worker-fence.v0", "nonce": NONCE, "request_ids": ["internal"]},
        {"protocol": "worker-fence.v1", "nonce": "stale", "request_ids": ["internal"]},
        {"protocol": "worker-fence.v1", "nonce": NONCE + "\n", "request_ids": ["internal"]},
        {"protocol": "worker-fence.v1", "nonce": NONCE, "request_ids": []},
        {"protocol": "worker-fence.v1", "nonce": NONCE, "request_ids": ["x", "x"]},
        {"protocol": "worker-fence.v1", "nonce": NONCE, "request_ids": ["x" * 1025]},
        {"protocol": "worker-fence.v1", "nonce": NONCE, "request_ids": [False]},
        {"protocol": "worker-fence.v1", "nonce": NONCE, "request_ids": ["internal"], "url": "x"},
        {"protocol": "worker-fence.v1", "nonce": NONCE, "request_ids": list(map(str, range(4097)))},
        object(),
    ],
)
def test_invalid_input_is_rejected_before_native_or_gpu_work(worker: Any, bad: Any) -> None:
    with pytest.raises(ValueError, match="invalid worker fence request"):
        worker.speechnexus_observe_worker_fence(bad)
    assert worker.sync_devices == []


def test_utf8_document_byte_limit_is_separate_from_id_character_limit(worker: Any) -> None:
    oversized = request(*(str(i) + "音" * 1000 for i in range(200)))
    with pytest.raises(ValueError, match="invalid worker fence request"):
        worker.speechnexus_observe_worker_fence(oversized)
    assert worker.sync_devices == []


def test_request_set_digest_is_order_independent_and_bound_to_exact_ids(fence_module: Any) -> None:
    first = fence_module.validate_request(request("a-internal", "b-internal"))[2]
    assert first == fence_module.validate_request(request("b-internal", "a-internal"))[2]
    assert first != fence_module.validate_request(request("a-internal", "c-internal"))[2]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nonce", "b" * 32),
        ("request_set_digest", "sha256:" + "0" * 64),
        ("request_state_entries", 1),
        ("state", "pending"),
        ("cuda_synchronized", False),
        ("release_evidence", True),
        ("worker_rank", True),
        ("uncontracted_endpoint", "synthetic"),
    ],
)
def test_consumer_rejects_foreign_or_contradictory_reply(
    fence_module: Any, worker: Any, field: str, value: Any
) -> None:
    response = observe(worker, "original-internal")
    nonce, _, digest = fence_module.validate_request(request("original-internal"))
    changed = copy.deepcopy(response)
    changed[field] = value
    with pytest.raises(ValueError, match="invalid worker fence response"):
        fence_module.validate_response(changed, nonce=nonce, request_set_digest=digest)


@pytest.mark.parametrize("rank", [None, -1, 4096, True])
def test_unidentified_worker_stays_unsupported(worker: Any, rank: Any) -> None:
    worker.rank = rank
    response = observe(worker, "original-internal")
    assert response["state"] == "unsupported"
    assert response["worker_rank"] is None
    assert response["request_state_entries"] is None
    assert worker.sync_devices == []


def test_changed_native_layout_fails_without_clearing_any_state(worker: Any) -> None:
    worker.model_runner.requests = None
    response = observe(worker, "original-internal")
    assert response["state"] == "failed"
    assert response["request_state_entries"] is None
    assert worker.model_runner.requests is None
    assert worker.sync_devices == []


def test_runtime_or_runner_difference_cannot_produce_worker_completion(
    fence_module: Any, worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fence_module, "_worker_runtime", lambda target: False)
    response = observe(worker, "original-internal")
    assert response["state"] == "unsupported"
    assert response["cuda_synchronized"] is False
    assert worker.sync_devices == []


@pytest.mark.parametrize("difference", ["none", "runner", "v2", "device"])
def test_runtime_checks_native_class_and_pin_once_per_worker(
    fence_module: Any, worker: Any, monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    pinned: list[dict[str, Any]] = []
    monkeypatch.setattr(fence_module, "verify_pin", pinned.append)
    original_import = fence_module.importlib.import_module
    native = SimpleNamespace(GPUModelRunner=type(worker.model_runner))
    monkeypatch.setattr(
        fence_module.importlib,
        "import_module",
        lambda name: native if name == "vllm.v1.worker.gpu_model_runner" else original_import(name),
    )
    if difference == "runner":
        worker.model_runner = SimpleNamespace()
    elif difference == "v2":
        worker.use_v2_model_runner = True
    elif difference == "device":
        worker.device.type = "cpu"
    assert worker.actual_runtime_check(worker) is (difference == "none")
    assert worker.actual_runtime_check(worker) is (difference == "none")
    assert len(pinned) == 1
    assert len(pinned[0]["source_sha256"]) == 21


def test_runtime_pin_failure_stays_failed_and_does_not_cache_verification(
    fence_module: Any, worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(pin: dict[str, Any]) -> None:
        raise RuntimeError("synthetic source mismatch")

    monkeypatch.setattr(fence_module, "verify_pin", fail)
    monkeypatch.setattr(fence_module, "_worker_runtime", worker.actual_runtime_check)
    response = observe(worker, "original-internal")
    assert response["state"] == "failed"
    assert not getattr(worker, "_speechnexus_worker_pin_verified", False)
    assert response["cuda_synchronized"] is False
    assert worker.sync_devices == []
