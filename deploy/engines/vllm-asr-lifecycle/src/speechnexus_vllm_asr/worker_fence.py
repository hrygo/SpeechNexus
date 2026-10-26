"""Read native worker state through vLLM's explicit worker extension mechanism.

This method does not abort, pause, free caches, dispatch work or attest a stop.
Its response is deliberately incompatible with gateway VerifiedStopEvidence.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator

from .runtime_pin import verify_pin

MAX_REQUEST_BYTES = 524288


@lru_cache(maxsize=1)
def _validators() -> tuple[Draft202012Validator, Draft202012Validator]:
    schema = json.loads(
        files("speechnexus_vllm_asr").joinpath("worker-fence.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    validators = [
        Draft202012Validator({"$ref": f"#/$defs/{kind}", "$defs": schema["$defs"]})
        for kind in ("request", "response")
    ]
    return validators[0], validators[1]


def validate_request(document: Any) -> tuple[str, tuple[str, ...], str]:
    """Validate against the packaged contract, without reflecting input in errors."""
    if not _validators()[0].is_valid(document):
        raise ValueError("invalid worker fence request")
    try:
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("invalid worker fence request") from None
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("invalid worker fence request")
    request_ids = tuple(sorted(document["request_ids"]))
    canonical = json.dumps(request_ids, ensure_ascii=False, separators=(",", ":")).encode()
    return document["nonce"], request_ids, "sha256:" + hashlib.sha256(canonical).hexdigest()


def validate_response(document: Any, *, nonce: str, request_set_digest: str) -> None:
    """Check contract structure, attempt nonce and the exact internal request set."""
    if (
        not _validators()[1].is_valid(document)
        or document["nonce"] != nonce
        or document["request_set_digest"] != request_set_digest
    ):
        raise ValueError("invalid worker fence response")


def _native_counts(worker: Any, request_ids: tuple[str, ...]) -> dict[str, Any]:
    runner = worker.model_runner
    maps = (runner.requests, runner.input_batch.req_id_to_index, runner.num_prompt_logprobs)
    if not all(isinstance(value, Mapping) for value in maps):
        raise ValueError("unsupported native worker state")
    return {
        "request_state_entries": sum(identity in maps[0] for identity in request_ids),
        "persistent_batch_entries": sum(identity in maps[1] for identity in request_ids),
        "prompt_logprob_entries": sum(identity in maps[2] for identity in request_ids),
        "execution_step_pending": runner.execute_model_state is not None,
        "transport_work_pending": bool(worker._pp_send_work),
    }


def _worker_runtime(worker: Any) -> bool:
    if not getattr(worker, "_speechnexus_worker_pin_verified", False):
        pin = json.loads(files("speechnexus_vllm_asr").joinpath("worker-pin.json").read_text())
        verify_pin(pin)
        worker._speechnexus_worker_pin_verified = True
    native = importlib.import_module("vllm.v1.worker.gpu_model_runner").GPUModelRunner
    return (
        type(worker.model_runner) is native
        and worker.use_v2_model_runner is False
        and worker.device.type == "cuda"
    )


class WorkerFenceExtension:
    """Opt-in mixin; only this named operation is added to the native worker."""

    def speechnexus_observe_worker_fence(self, document: Any) -> dict[str, Any]:
        nonce, request_ids, digest = validate_request(document)
        rank = getattr(self, "rank", None)
        if type(rank) is not int or not 0 <= rank <= 4095:
            rank = None
        response: dict[str, Any] = {
            "protocol": "worker-fence.v1",
            "nonce": nonce,
            "request_set_digest": digest,
            "worker_rank": rank,
            "state": "unsupported",
            "request_state_entries": None,
            "persistent_batch_entries": None,
            "prompt_logprob_entries": None,
            "execution_step_pending": None,
            "transport_work_pending": None,
            "cuda_synchronized": False,
            "release_evidence": False,
        }
        try:
            if rank is None or not _worker_runtime(self):
                return response
            counts = _native_counts(self, request_ids)
            if any(counts.values()):
                response.update(counts, state="pending")
                return response
            # Use the actual worker device, including the worker thread's context.
            # Caller timeout cannot cancel this native action or attest completion.
            torch = importlib.import_module("torch")
            torch.cuda.synchronize(self.device)  # type: ignore[attr-defined]
            # Do not reuse a pre-fence snapshot if a native collaborator changed it.
            counts = _native_counts(self, request_ids)
            if any(counts.values()):
                response.update(counts, state="pending")
            else:
                response.update(counts, state="observed", cuda_synchronized=True)
        except Exception:
            # Do not return vendor exceptions, paths, request IDs or model content.
            response["state"] = "failed"
        validate_response(response, nonce=nonce, request_set_digest=digest)
        return response
