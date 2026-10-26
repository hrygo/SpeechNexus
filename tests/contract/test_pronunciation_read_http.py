"""Fixed-read protocol consumers, not a pronunciation HTTP service or qualification."""

from __future__ import annotations

import copy
import json
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, cast

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from referencing import Registry
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.pronunciation_read_http import (
    contract,
    validate_request,
    validate_response,
)

from tests.support.contracts import document, retrieve

ROOT = Path(__file__).resolve().parents[2]
PATH = "/v1/nexus/pronunciation-sets/{pronunciation_id}/revisions/{revision}"
GET = "getPronunciationRevision"
SOURCE = document(ROOT / "contracts/speech-openapi.yaml")
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/pronunciation-read-http.json").read_text())[
    "examples"
]


def sample(name: str) -> dict[str, Any]:
    return copy.deepcopy(next(x for x in EXAMPLES if x["id"] == name))


def resolve(value: dict[str, Any]) -> dict[str, Any]:
    while value.get("$ref", "").startswith("#/components/"):
        pointer, value = value["$ref"], SOURCE
        for token in pointer[2:].split("/"):
            value = value[token]
    return value


def source_accepts(example: dict[str, Any]) -> bool:
    """Read original source parameters/headers/schemas without the generator."""
    operation, value = SOURCE["paths"][PATH]["get"], example["value"]

    def valid(schema: dict[str, Any], item: Any) -> bool:
        return Draft202012Validator(
            {"components": SOURCE["components"], **schema},
            registry=cast(Any, Registry)(retrieve=retrieve),
            format_checker=FormatChecker(),
        ).is_valid(item)

    def valid_header(schema: dict[str, Any], item: Any) -> bool:
        # Decode a serialized HTTP integer header, not JSON body values or
        # opaque revision strings. Keep safe integer transport parity with TS.
        if schema.get("type") == "integer" and isinstance(item, str):
            if len(item) > 17 or re.fullmatch(r"-?(0|[1-9][0-9]*)", item) is None:
                return False
            item = int(item)
            if abs(item) > 2**53 - 1:
                return False
        return valid(schema, item)

    if example["kind"] == "response":
        response = operation["responses"].get(str(example["status"]))
        if response is None or set(value) - {"body", "headers"}:
            return False
        response = resolve(response)
        if "body" not in value or not valid(
            response["content"]["application/json"]["schema"], value["body"]
        ):
            return False
        headers = value.get("headers", {})
        if not isinstance(headers, dict) or len({k.lower() for k in headers}) != len(headers):
            return False
        headers = {key.lower(): item for key, item in headers.items()}
        for name, raw in response.get("headers", {}).items():
            header = resolve(raw)
            if header.get("required") and name.lower() not in headers:
                return False
            if name.lower() in headers and not valid_header(
                header["schema"], headers[name.lower()]
            ):
                return False
        return True
    if set(value) - {"path", "query", "headers"}:
        return False
    groups = {key: value.get(key, {}) for key in ("path", "query", "headers")}
    if not all(isinstance(group, dict) for group in groups.values()):
        return False
    headers = groups["headers"]
    if len({key.lower() for key in headers}) != len(headers):
        return False
    groups["headers"] = {key.lower(): item for key, item in headers.items()}
    declared: dict[str, set[str]] = {key: set() for key in groups}
    for raw in operation["parameters"]:
        parameter = resolve(raw)
        location = "headers" if parameter["in"] == "header" else parameter["in"]
        name = parameter["name"].lower() if location == "headers" else parameter["name"]
        declared[location].add(name)
        if name not in groups[location]:
            if parameter.get("required"):
                return False
        elif not valid(parameter["schema"], groups[location][name]):
            return False
    return all(set(groups[key]) <= declared[key] for key in ("path", "query"))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda x: x["id"])
def test_original_source_and_python_accept_the_same_envelopes(example: dict[str, Any]) -> None:
    assert source_accepts(example) is example["valid"]
    value = copy.deepcopy(example["value"])

    def check() -> Any:
        if example["kind"] == "request":
            return validate_request(GET, value)
        return validate_response(GET, example["status"], value)

    if example["valid"]:
        assert check() is value
    else:
        with pytest.raises(ContractValidationError):
            check()
    assert value == example["value"]


def test_read_surface_closes_real_references_without_registering_other_operations() -> None:
    actual = contract()
    validate(actual)
    assert set(actual["paths"]) == {PATH}
    assert set(actual["paths"][PATH]) == {"get"}
    assert actual["info"]["version"] == SOURCE["info"]["version"]
    assert set(actual["paths"][PATH]["get"]["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "404",
        "405",
        "429",
        "503",
    }
    original = SOURCE["paths"][PATH]["get"]["responses"]["200"]["content"]["application/json"]
    assert original["schema"]["$ref"].endswith(
        "voice-resource.schema.json#/$defs/PronunciationRevision"
    )
    for unknown in (
        "createPronunciationRevision",
        "listPronunciationSets",
        "queryPronunciationKey",
    ):
        with pytest.raises(ValueError, match="Unknown HTTP operation"):
            validate_request(unknown, {})
    actual["paths"].clear()
    assert set(contract()["paths"]) == {PATH}


@contextmanager
def native_protocol_server() -> Iterator[tuple[str, list[str]]]:
    """A synthetic loopback wire fixture; deliberately not the product router."""
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        timeout = 2

        def do_GET(self) -> None:
            calls.append(self.path)
            name = "unavailable-metadata" if len(calls) == 1 else "draft-representation"
            reply = sample(name)
            raw = json.dumps(reply["value"]["body"], ensure_ascii=False).encode()
            self.send_response(reply["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in reply["value"]["headers"].items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_python_native_tcp_keeps_unavailable_distinct_and_repeats_only_fixed_get() -> None:
    envelope = sample("fixed-big-revision")["value"]
    assert validate_request(GET, envelope) is envelope
    path = PATH.format(**envelope["path"])
    with (
        native_protocol_server() as (base, calls),
        httpx.Client(base_url=base, timeout=2, trust_env=False, follow_redirects=False) as client,
    ):
        first = client.get(path, headers=envelope["headers"])
        validate_response(
            GET, first.status_code, {"body": first.json(), "headers": dict(first.headers)}
        )
        assert first.status_code == 503 and first.json()["error"]["retryable"] is True
        assert "nexus" not in first.json()["error"] and calls == [path]
        # Explicit caller action repeats the same immutable identity, never a create or key query.
        second = client.get(path, headers=envelope["headers"])
        validate_response(
            GET, second.status_code, {"body": second.json(), "headers": dict(second.headers)}
        )
        assert second.status_code == 200 and second.json()["revision"] == "9007199254740993"
        assert calls == [path, path]


def test_typescript_native_tcp_consumes_generated_read_client_without_automatic_retry() -> None:
    script = """
      import assert from 'node:assert/strict';
      import {createValidatedClient} from './packages/contracts/src/pronunciation_read-http.ts';
      const client=createValidatedClient(process.argv[1],4096);
      const path='/v1/nexus/pronunciation-sets/{pronunciation_id}/revisions/{revision}';
      const params={path:{pronunciation_id:'pron-synthetic-read',revision:'9007199254740993'}};
      const first=await client.GET(path,{params});
      assert.equal(first.response.status,503);
      assert.equal(first.error.error.code,'pronunciation_unavailable');
      assert.equal(first.error.error.retryable,true);
      assert.equal(Object.hasOwn(first.error.error,'nexus'),false);
      const second=await client.GET(path,{params});
      assert.equal(second.response.status,200);
      assert.equal(second.data.revision,'9007199254740993');
      process.stdout.write(JSON.stringify({first_status:503,revision:second.data.revision}));
    """
    with native_protocol_server() as (base, calls):
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script, base],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"first_status": 503, "revision": "9007199254740993"}
        assert calls == [PATH.format(**sample("fixed-big-revision")["value"]["path"])] * 2
