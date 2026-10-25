"""Future lifecycle contracts; no publisher, registry or physical qualification."""

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
from openapi_spec_validator import validate
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.profile_http import contract as draft_contract
from speechnexus_contracts.profile_http import validate_response as validate_fixed_response
from speechnexus_contracts.profile_lifecycle_http import (
    contract,
    validate_request,
    validate_response,
)

from tests.contract.test_binding_entrypoints import generator
from tests.contract.test_binding_http_bindings import SOURCE, source_accepts

ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = ("publishProfileRevision", "activatePublishedProfileRevision")
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/profile-lifecycle-http.json").read_text())[
    "examples"
]


def sample(identity: str) -> dict[str, Any]:
    return copy.deepcopy(next(example for example in EXAMPLES if example["id"] == identity))


@pytest.mark.parametrize("operation", OPERATIONS)
def test_lifecycle_posts_declare_bounded_ingress_and_unknown_original_result(
    operation: str,
) -> None:
    post = next(
        item["post"]
        for item in SOURCE["paths"].values()
        if item.get("post", {}).get("operationId") == operation
    )
    assert set(post["responses"]) >= {
        "200",
        "400",
        "401",
        "403",
        "404",
        "405",
        "408",
        "409",
        "413",
        "415",
        "429",
        "503",
    }


@pytest.mark.parametrize("operation", OPERATIONS)
def test_lifecycle_success_cannot_claim_a_draft_is_published(operation: str) -> None:
    fixtures = json.loads((ROOT / "tests/fixtures/contracts/profile-http.json").read_text())
    draft = copy.deepcopy(next(e for e in fixtures["examples"] if e["id"] == "create-draft"))
    draft.update(operation=operation, status=200)
    assert not source_accepts(draft)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e["id"])
def test_lifecycle_runtime_agrees_with_source_without_mutation(example: dict[str, Any]) -> None:
    assert source_accepts(example) is example["valid"]
    value = copy.deepcopy(example["value"])

    def check() -> Any:
        if example["kind"] == "request":
            return validate_request(example["operation"], value)
        return validate_response(example["operation"], example["status"], value)

    if example["valid"]:
        assert check() is value
    else:
        with pytest.raises(ContractValidationError):
            check()
    assert value == example["value"]


def test_future_lifecycle_surface_does_not_expand_the_existing_draft_surface() -> None:
    manifest = json.loads((ROOT / "contracts/bindings.json").read_text())
    actual = contract()
    assert actual == generator().bundle_http_surface(manifest["http_surfaces"]["profile_lifecycle"])
    validate(actual)
    assert actual["info"]["version"] == "0.1.0.dev4"
    assert all(set(item) == {"post"} for item in actual["paths"].values())
    assert len(actual["paths"]) == 2
    assert not (set(actual["paths"]) & set(draft_contract()["paths"]))
    assert set(draft_contract()["paths"]) == {
        "/v1/nexus/admin/profiles",
        "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}",
    }
    for unknown in ("createProfileRevision", "queryProfileCommand", "retireProfileRevision"):
        with pytest.raises(ValueError, match="Unknown HTTP operation"):
            validate_request(unknown, {})


# Synthetic contract responder, with no database, publisher or trusted evidence.
type Call = tuple[str, str, str | None, bytes]


@contextmanager
def fixture_http(prefix: str) -> Iterator[tuple[str, list[Call]]]:
    calls: list[Call] = []
    request = sample(prefix + "-original")["value"]
    fixed = "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}".format(**request["path"])
    suffix = "/publications" if prefix == "publish" else "/activations"
    read_failure = json.loads((ROOT / "tests/fixtures/contracts/profile-http.json").read_text())
    read_reply = next(
        e
        for e in read_failure["examples"]
        if e["operation"] == "getProfileRevisionForAdministration"
        and e.get("status") == 503
        and e["valid"]
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_: str, *args: Any) -> None:
            pass

        def reply(self, example: dict[str, Any]) -> None:
            data = json.dumps(example["value"]["body"]).encode()
            self.send_response(example["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            size = int(self.headers["Content-Length"])
            assert self.path == fixed + suffix and 0 < size < 16384
            calls.append(
                ("POST", self.path, self.headers.get("Idempotency-Key"), self.rfile.read(size))
            )
            count = sum(call[0] == "POST" for call in calls)
            self.reply(sample(prefix + ("-unknown" if count == 1 else "-published")))

        def do_GET(self) -> None:
            assert self.path == fixed
            calls.append(("GET", self.path, None, b""))
            self.reply(read_reply)

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


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize("prefix", ["publish", "activate"])
@pytest.mark.parametrize("keyed", [True, False])
def test_fixture_http_unknown_preserves_original_path_body_key_and_no_keyless_recovery(
    language: str, prefix: str, keyed: bool
) -> None:
    with fixture_http(prefix) as (base, calls):
        request = sample(prefix + ("-original" if keyed else "-keyless"))["value"]
        if language == "typescript":
            result = subprocess.run(
                [
                    "node",
                    str(ROOT / "tests/fixtures/contracts/profile-lifecycle-http.mjs"),
                    base,
                    prefix,
                    str(keyed).lower(),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            operation = OPERATIONS[0 if prefix == "publish" else 1]
            validate_request(operation, request)
            fixed = "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}".format(
                **request["path"]
            )
            suffix = "/publications" if prefix == "publish" else "/activations"
            with httpx.Client(base_url=base, timeout=5) as client:
                first = client.post(
                    fixed + suffix, json=request["body"], headers=request["headers"]
                )
                validate_response(operation, first.status_code, {"body": first.json()})
                assert first.status_code == 503 and first.json()["error"]["retryable"] is False
                assert "nexus" not in first.json()["error"] and len(calls) == 1
                if keyed:
                    read = client.get(fixed)
                    validate_fixed_response(
                        "getProfileRevisionForAdministration",
                        read.status_code,
                        {"body": read.json()},
                    )
                    assert read.status_code == 503 and len(calls) == 2
                    replay = client.post(
                        fixed + suffix, json=request["body"], headers=request["headers"]
                    )
                    validate_response(operation, replay.status_code, {"body": replay.json()})
                    assert (
                        replay.status_code == 200
                        and replay.json()["publication"]["status"] == "published"
                    )
        assert len(calls) == (3 if keyed else 1)
        if keyed:
            assert calls[0] == calls[2] and calls[1][0] == "GET"
        else:
            assert calls[0][2] is None
        assert json.loads(calls[0][3]) == request["body"]
