"""Declared-but-unimplemented operations answer one stable, distinguishable error.

#473: every operation the frozen speech contract declares and this deployment
does not implement must be registered centrally and answer 503
``operation_not_implemented`` through the public error envelope. A consumer must
be able to tell that reply apart from a real ``backend_unavailable`` 503, a
confirmed missing resource and a wrong method.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route as StarletteRoute

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from speechnexus_backend.speech.interfaces.http.unimplemented import (
    UNIMPLEMENTED_OPERATIONS,
    assert_placeholder_ownership,
    create_unimplemented_router,
)
from speechnexus_backend.speech.interfaces.http.unimplemented_errors import (
    OPERATION_NOT_IMPLEMENTED,
)
from tests.contract.test_speech_http_statuses import assert_error_contract
from tests.support.contracts import validator
from tests.unit.backend.test_artifact_http import service
from tests.unit.backend.test_health import _settings

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend/src/speechnexus_backend"
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
PLACEHOLDER_RESPONSE = "#/components/responses/OperationNotImplemented"
REQUEST_ID = "unimplemented-473"
# These five operations already declared `ServiceUnavailable` before #473.
# Replacing that declaration would drop the compatible `Retry-After` header
# from the versioned surface, which both structural gates reject as breaking,
# so they keep it: that schema accepts the same placeholder body.
PREDECLARED_SERVICE_UNAVAILABLE = frozenset(
    {
        "listPronunciationSets",
        "listJobs",
        "getOperation",
        "createRealtimeSession",
        "connectRealtime",
    }
)
# Every placeholder answers at runtime, including the compatible-surface one.
CASES = sorted(
    (operation, method, path) for operation, (method, path) in UNIMPLEMENTED_OPERATIONS.items()
)


def declared_operations() -> list[tuple[str, str, str]]:
    """Every operation whose 503 is the placeholder component, straight from the contract."""
    found: list[tuple[str, str, str]] = []
    for path, item in DOCUMENT["paths"].items():
        for method, specification in item.items():
            responses = specification.get("responses", {})
            reference = responses.get("503", {}).get("$ref")
            if reference == PLACEHOLDER_RESPONSE:
                found.append((specification["operationId"], method.upper(), path))
    return sorted(found)


def _code_keys(node: ast.Dict, constants: dict[str, str]) -> set[str]:
    codes: set[str] = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            codes.add(key.value)
        elif isinstance(key, ast.Name) and key.id in constants:
            codes.add(constants[key.id])
    return codes


def _is_error_map(node: ast.Dict) -> bool:
    """A mapping of code -> (status, public type, message[, retryable])."""
    if not node.values:
        return False
    for value in node.values:
        if not isinstance(value, ast.Tuple) or len(value.elts) < 3:
            return False
        status, kind = value.elts[0], value.elts[1]
        if not (isinstance(status, ast.Constant) and type(status.value) is int):
            return False
        if not (isinstance(kind, ast.Constant) and isinstance(kind.value, str)):
            return False
    return True


def error_maps(root: Path = BACKEND_ROOT) -> dict[str, set[str]]:
    """Every module-level error map below `root`, keyed by module path.

    The map name is not enough on its own: `binding_errors` and
    `profile_errors` call theirs `ERRORS`, and `pronunciations`,
    `binding_lifecycle` and `workspaces` keep unannotated `_ERRORS`. The shape
    of the values decides, so a renamed or new map is still scanned.

    The key is the repository-relative module path, not `path.stem`: the
    backend already ships several same-named modules (`workspaces`, `jobs`,
    `binding_lifecycle`), and a stem key would silently let a second map
    overwrite the first, which the set assertions below cannot notice.
    """
    maps: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants: dict[str, str] = {}
        for statement in tree.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            value = statement.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                constants[target.id] = value.value
        key = path.relative_to(root).with_suffix("").as_posix()
        for statement in tree.body:
            if isinstance(statement, ast.AnnAssign):
                targets: list[ast.expr] = [statement.target]
            elif isinstance(statement, ast.Assign):
                targets = list(statement.targets)
            else:
                continue
            named = [item.id for item in targets if isinstance(item, ast.Name)]
            mapping_node = statement.value
            if not named or not isinstance(mapping_node, ast.Dict):
                continue
            if named[0] not in {"_ERRORS", "ERRORS"} and not _is_error_map(mapping_node):
                continue
            codes = _code_keys(mapping_node, constants)
            if codes:
                # Union rather than replace: a module may declare `ERRORS` and
                # a narrower `_ERRORS`, and both still own their codes.
                maps.setdefault(key, set()).update(codes)
    return maps


def concrete(path: str) -> str:
    while "{" in path:
        start = path.index("{")
        end = path.index("}", start)
        path = path[:start] + "synthetic-id" + path[end + 1 :]
    return path


def test_contract_and_placeholder_table_agree() -> None:
    assert len(CASES) == 21
    assert {operation for operation, _, _ in CASES} == set(UNIMPLEMENTED_OPERATIONS)
    declared = {operation for operation, _, _ in declared_operations()}
    assert len(declared) == 16
    # Every remaining operation points at the dedicated placeholder component;
    # the five with an older 503 declaration keep it rather than lose a header.
    assert declared == set(UNIMPLEMENTED_OPERATIONS) - PREDECLARED_SERVICE_UNAVAILABLE


@pytest.mark.parametrize("case", CASES, ids=lambda case: case[0])
async def test_unimplemented_operation_answers_the_stable_error(
    case: tuple[str, str, str],
) -> None:
    operation, method, path = case
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        response = await http.request(
            method,
            concrete(path),
            headers={"X-Request-ID": REQUEST_ID, "Content-Type": "application/json"},
            content=b"{}" if method in {"POST", "PATCH", "PUT"} else None,
        )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == OPERATION_NOT_IMPLEMENTED
    assert body["error"]["type"] == "service_unavailable_error"
    assert body["error"]["retryable"] is False
    assert body["error"]["param"] is None
    assert body["error"]["request_id"] == REQUEST_ID
    # Correlation reaches the transport header as well as the envelope.
    assert response.headers["x-request-id"] == REQUEST_ID
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-should-retry"] == "false"
    # The declared placeholder response pins code, type and retryable.
    assert_error_contract(operation, response)


async def test_unimplemented_error_is_not_a_real_failure() -> None:
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        unimplemented = await http.get(
            "/v1/nexus/voice-drafts/synthetic-id", headers={"X-Request-ID": REQUEST_ID}
        )
        unavailable = await http.get("/v1/nexus/artifacts/synthetic-id")
    assert unimplemented.status_code == unavailable.status_code == 503
    assert unimplemented.json()["error"]["type"] == unavailable.json()["error"]["type"]
    # The code is the only reliable discriminator: status and type are shared,
    # and a real outage can also be non-retryable (see the next test).
    assert unimplemented.json()["error"]["code"] == OPERATION_NOT_IMPLEMENTED
    assert unavailable.json()["error"]["code"] != OPERATION_NOT_IMPLEMENTED


def test_status_type_and_retryable_cannot_identify_the_placeholder() -> None:
    # `job_unavailable` is 503 + service_unavailable_error + retryable=false, so
    # only `error.code` separates a real failure from "not implemented".
    body = json.loads(bytes(job_error("job_unavailable", REQUEST_ID).body))
    assert body["error"]["type"] == "service_unavailable_error"
    assert body["error"]["retryable"] is False
    assert body["error"]["code"] != OPERATION_NOT_IMPLEMENTED


def test_no_other_error_map_reuses_the_placeholder_code() -> None:
    maps = error_maps()
    # The scan must really cover the maps that name theirs differently, or a
    # narrow glob could pass this test by looking in the wrong place.
    assert {"binding_errors", "profile_errors", "job_errors", "unimplemented_errors"} <= {
        Path(key).name for key in maps
    }
    owners = {module for module, entries in maps.items() if OPERATION_NOT_IMPLEMENTED in entries}
    assert owners == {"speech/interfaces/http/unimplemented_errors"}


def test_same_stem_error_maps_are_all_collected(tmp_path: Path) -> None:
    """Two same-named modules must both be scanned instead of collapsing into one key."""
    for package, code in (("alpha", "alpha_code"), ("beta", "beta_code")):
        directory = tmp_path / package
        directory.mkdir()
        (directory / "workspace_errors.py").write_text(
            f'WORKSPACE_ERRORS = {{"{code}": (409, "conflict_error", "{code}")}}\n',
            encoding="utf-8",
        )

    maps = error_maps(tmp_path)

    # The fixture is adversarial only because both modules share one stem: a
    # stem key would have kept one of them and silently dropped the other.
    assert {Path(name).stem for name in maps} == {"workspace_errors"}
    assert maps["alpha/workspace_errors"] == {"alpha_code"}
    assert maps["beta/workspace_errors"] == {"beta_code"}


async def test_unimplemented_error_is_not_a_missing_resource_or_bad_method() -> None:
    artifacts, _, _ = service()
    app = create_app(settings=_settings(), artifacts=artifacts)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        missing = await http.get("/v1/nexus/artifacts/synthetic-id")
        wrong_method = await http.put(
            "/v1/nexus/voice-designs", headers={"X-Request-ID": REQUEST_ID}
        )
        unregistered = await http.get("/v1/nexus/operations/synthetic-id/unknown-child")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "artifact_not_found"
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"
    # A path with no route is still the framework's 404, not a placeholder.
    assert unregistered.status_code == 404
    assert "error" not in unregistered.json()


@pytest.mark.parametrize(
    ("path", "allow", "code"),
    [
        # GET listJobs is a placeholder while POST createJob is implemented: the
        # header must merge both, not repeat whichever route matched first.
        ("/v1/nexus/jobs", "GET, POST", "job_method_not_allowed"),
        # Both methods live on a parameterised template, so a literal path
        # comparison used to report only the first of them.
        ("/v1/nexus/voice-drafts/synthetic-id", "GET, PATCH", "method_not_allowed"),
        ("/v1/nexus/pronunciation-sets", "GET, POST", "method_not_allowed"),
        ("/v1/nexus/voices/voice-demo/revisions/1/retirements", "POST", "method_not_allowed"),
    ],
)
async def test_allow_header_lists_every_registered_method(path: str, allow: str, code: str) -> None:
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        response = await http.delete(path)
    assert response.status_code == 405
    assert response.headers["allow"] == allow
    # The Job prefix keeps its own 405 code; only the Allow list grows.
    assert response.json()["error"]["code"] == code


async def test_allow_header_drops_implicit_methods_from_the_framework() -> None:
    """HEAD/OPTIONS reported by the framework must never reach the merged Allow.

    FastAPI's APIRoute does not add HEAD to a GET route, so the placeholder
    surface alone cannot exercise this filter. A raw Starlette route does add
    HEAD, and a route may declare OPTIONS explicitly, so both are injected at
    the front of the router table where the framework's 405 reads its Allow.
    """
    app = create_app(settings=_settings())

    async def injected(request: Request) -> Response:
        raise AssertionError("the injected route must never dispatch")

    app.router.routes.insert(
        0,
        StarletteRoute("/v1/nexus/voice-drafts/{draft_id}", injected, methods=["GET", "OPTIONS"]),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        response = await http.delete("/v1/nexus/voice-drafts/synthetic-id")
    assert response.status_code == 405
    allow = {method.strip() for method in response.headers["allow"].split(",")}
    assert allow == {"GET", "PATCH"}


def test_the_placeholder_router_alone_owns_its_routes() -> None:
    api = FastAPI()
    api.include_router(create_unimplemented_router())
    assert_placeholder_ownership(api.routes)


def test_a_real_route_cannot_share_a_placeholder_method() -> None:
    api = FastAPI()
    api.include_router(create_unimplemented_router())

    @api.get("/v1/nexus/voice-drafts/{draft_id}", include_in_schema=False)
    async def shadowed(draft_id: str) -> dict[str, str]:
        return {"draft_id": draft_id}

    with pytest.raises(RuntimeError) as raised:
        assert_placeholder_ownership(api.routes)
    # Exactly the duplicated template is reported; the other twenty placeholders
    # must not be swept in, or the assertion would pass on a wholesale failure.
    listed = raised.value.args[0].split("include the placeholder router last: ", 1)[1]
    assert listed == "GET /v1/nexus/voice-drafts/{draft_id}"


@pytest.mark.parametrize(
    "template",
    [
        "/v1/nexus/voice-drafts/{voice_draft_id}",
        "/v1/nexus/voice-drafts/{draft_id:int}",
    ],
)
def test_a_renamed_or_converted_parameter_still_collides(template: str) -> None:
    """The guard compares normalized templates, so respelling a parameter cannot hide a conflict."""
    api = FastAPI()
    api.include_router(create_unimplemented_router())

    async def shadowed(**params: str) -> dict[str, str]:
        return params

    api.add_api_route(template, shadowed, methods=["GET"], include_in_schema=False)

    with pytest.raises(RuntimeError) as raised:
        assert_placeholder_ownership(api.routes)
    listed = raised.value.args[0].split("include the placeholder router last: ", 1)[1]
    # The message keeps the template the placeholder table declares, not the
    # competing spelling, so the report names the operation that must move.
    assert listed == "GET /v1/nexus/voice-drafts/{draft_id}"


def test_served_openapi_does_not_advertise_placeholders() -> None:
    paths = create_app(settings=_settings()).openapi()["paths"]
    for path in (
        "/v1/nexus/voice-designs",
        "/v1/nexus/voice-clones",
        "/v1/nexus/voice-drafts/{draft_id}",
        "/v1/nexus/pronunciation-sets",
        "/v1/realtime",
    ):
        assert path not in paths
