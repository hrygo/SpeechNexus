"""Source-level producer contract prerequisites, separate from actual product routes."""

import copy
import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from speechnexus_contracts.binding_defaults_http import (
    contract,
    validate_request,
    validate_response,
)
from speechnexus_contracts.validation import ContractValidationError

from tests.contract.test_binding_http_bindings import SOURCE as LEGACY_SOURCE
from tests.contract.test_binding_http_bindings import source_accepts as accepts_source
from tests.support.contracts import document

ROOT = Path(__file__).resolve().parents[2]
SOURCE = document(ROOT / "contracts/binding-defaults-producer.openapi.yaml")
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/binding-defaults-http.json").read_text())[
    "examples"
]


def sample(identity: str) -> dict[str, Any]:
    return copy.deepcopy(next(e for e in EXAMPLES if e["id"] == identity))


def source_accepts(example: dict[str, Any]) -> bool:
    return accepts_source(example, SOURCE)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e["id"])
def test_separate_http_profile_matches_original_source_without_mutation(
    example: dict[str, Any],
) -> None:
    assert source_accepts(example) is example["valid"]
    value = copy.deepcopy(example["value"])
    before = json.dumps(value, sort_keys=True)
    if example["valid"]:
        result = (
            validate_request(example["operation"], value)
            if example["kind"] == "request"
            else validate_response(example["operation"], example["status"], value)
        )
        assert result is value
    else:
        with pytest.raises(ContractValidationError):
            if example["kind"] == "request":
                validate_request(example["operation"], value)
            else:
                validate_response(example["operation"], example["status"], value)
    assert json.dumps(value, sort_keys=True) == before


def test_future_create_fields_reference_the_exact_legacy_domains_and_requiredness() -> None:
    original = LEGACY_SOURCE["components"]["schemas"]["ModelBindingCreateRequest"]
    request = SOURCE["components"]["schemas"]["BindingDefaultsCreateRequest"]
    assert request["required"] == original["required"]
    assert request["additionalProperties"] is False
    assert set(request["properties"]) == set(original["properties"]) | {"parameter_defaults"}
    prefix = (ROOT / "contracts/admin-openapi.yaml").as_uri()
    for name in original["properties"]:
        assert request["properties"][name] == {
            "$ref": prefix + f"#/components/schemas/ModelBindingCreateRequest/properties/{name}"
        }
    assert not accepts_source(sample("defaults-create-sparse"), LEGACY_SOURCE)
    assert DEFAULTS_PATH not in LEGACY_SOURCE["paths"]
    assert set(contract()["paths"]) == {
        "/v1/nexus/admin/model-bindings",
        DEFAULTS_PATH,
    }


DEFAULTS_PATH = (
    "/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}/parameter-defaults"
)


def test_existing_creation_accepts_explicit_sparse_defaults_without_caller_identity() -> None:
    assert source_accepts(sample("defaults-create-sparse"))
    assert source_accepts(sample("defaults-create-explicit-empty"))
    assert not source_accepts(sample("defaults-create-invalid-caller-parent"))


def test_fixed_defaults_read_is_declared_without_a_mutation_or_latest_route() -> None:
    assert DEFAULTS_PATH in SOURCE["paths"]
    assert set(SOURCE["paths"][DEFAULTS_PATH]) == {"get"}
    assert source_accepts(sample("defaults-get-fixed"))
    assert not source_accepts(sample("defaults-get-invalid-latest"))


# This server returns synthetic contract fixtures. It is not the product writer,
# does not allocate any resource and supplies no database/qualification evidence.

POST = "createModelBindingDraft"
GET = "getModelBindingParameterDefaults"
COLLECTION = "/v1/nexus/admin/model-bindings"
FIXED = COLLECTION + "/asr-quality/revisions/1/parameter-defaults"
MISSING = COLLECTION + "/asr-quality/revisions/2/parameter-defaults"
type Call = tuple[str, str, str | None, bytes]


@contextmanager
def fixture_http() -> Iterator[tuple[str, list[Call]]]:
    calls: list[Call] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_: str, *args: Any) -> None:
            pass

        def reply(self, identity: str) -> None:
            example = sample(identity)
            data = json.dumps(example["value"]["body"]).encode()
            self.send_response(example["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            size = int(self.headers["Content-Length"])
            assert self.path == COLLECTION and 0 < size < 16384
            calls.append(
                ("POST", self.path, self.headers.get("Idempotency-Key"), self.rfile.read(size))
            )
            posts = sum(call[0] == "POST" for call in calls)
            self.reply("create-unknown" if posts == 1 else "create-draft")

        def do_GET(self) -> None:
            assert self.path in {FIXED, MISSING}
            calls.append(("GET", self.path, None, b""))
            if self.path == MISSING:
                self.reply("defaults-get-confirmed-missing")
                return
            reads = sum(call[1] == FIXED for call in calls)
            self.reply("defaults-get-unavailable" if reads == 1 else "defaults-get-snapshot")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


def python_consumer(base: str, calls: list[Call], keyed: bool) -> None:
    request = sample("defaults-create-sparse" if keyed else "defaults-create-keyless")["value"]
    assert validate_request(POST, request) is request
    with httpx.Client(base_url=base, timeout=5) as client:
        reply = client.post(COLLECTION, json=request["body"], headers=request.get("headers"))
        validate_response(POST, reply.status_code, {"body": reply.json()})
        assert reply.status_code == 503 and reply.json()["error"]["retryable"] is False
        assert len(calls) == 1
        if not keyed:
            return
        replay = client.post(COLLECTION, json=request["body"], headers=request["headers"])
        validate_response(POST, replay.status_code, {"body": replay.json()})
        assert replay.status_code == 201 and len(calls) == 2
        assert calls[0] == calls[1]
        path = {"binding_id": "asr-quality", "revision": "1"}
        validate_request(GET, {"path": path})
        first = client.get(FIXED)
        validate_response(GET, first.status_code, {"body": first.json()})
        assert first.status_code == 503 and first.json()["error"]["retryable"] is True
        assert len(calls) == 3
        second = client.get(FIXED)
        validate_response(GET, second.status_code, {"body": second.json()})
        assert second.status_code == 200 and len(calls) == 4
        assert second.json()["parameters"] == request["body"]["parameter_defaults"]
        missing = client.get(MISSING)
        validate_response(GET, missing.status_code, {"body": missing.json()})
        assert missing.status_code == 404 and missing.json()["error"]["retryable"] is False
        assert "parameters" not in missing.json()


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize("keyed", [True, False])
def test_fixture_http_consumers_preserve_defaults_original_key_and_fixed_reads(
    language: str, keyed: bool
) -> None:
    with fixture_http() as (base, calls):
        if language == "python":
            python_consumer(base, calls, keyed)
        else:
            script = ROOT / "tests/fixtures/contracts/binding-defaults-producer-http.mjs"
            result = subprocess.run(
                ["node", str(script), base, str(keyed).lower()],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
        assert len(calls) == (5 if keyed else 1)
        request = sample("defaults-create-sparse")["value"]["body"]
        assert json.loads(calls[0][3]) == request
        if keyed:
            assert calls[0] == calls[1]
            assert calls[2][1] == calls[3][1] == FIXED and calls[4][1] == MISSING
        else:
            assert calls[0][2] is None
