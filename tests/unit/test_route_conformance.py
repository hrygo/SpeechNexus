"""Speech route conformance must fail on a removed route or a new status."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import sys
from pathlib import Path
from typing import Any

import fastapi
import httpx
import pytest
import starlette.routing
from fastapi import APIRouter, FastAPI
from starlette.responses import JSONResponse
from starlette.routing import Host, Route
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "route_conformance", ROOT / "scripts/dev-env/check-route-conformance.py"
)
assert SPEC is not None and SPEC.loader is not None


def load() -> Any:
    assert SPEC is not None and SPEC.loader is not None
    module: Any = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = module
    SPEC.loader.exec_module(module)
    return module


def compare(module: Any, registered: tuple[Any, ...]) -> Any:
    base = module.build_report(ROOT)
    return module.evaluate(
        base.contract_operations,
        registered,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
    )


def test_repository_has_no_unbaselined_deviation() -> None:
    module = load()
    report = module.build_report(ROOT)
    assert report.findings == ()
    contract_ids = {operation.operation_id for operation in report.contract_operations}
    registered_ids = {
        route.operation_id for route in report.registered if route.operation_id in contract_ids
    }
    # #473 registers a placeholder for every declared operation, so "declared but
    # unregistered" is now empty; the unimplemented set is proven by route shape.
    assert contract_ids - registered_ids == set()
    assert report.known_unimplemented == tuple(sorted(module.KNOWN_UNIMPLEMENTED_OPERATIONS))
    assert len(module.KNOWN_UNIMPLEMENTED_OPERATIONS) == 12
    assert module.KNOWN_UNDECLARED_STATUSES == {}
    assert report.known_status_gaps == ()
    # Every operation declares 405; each one is probed for the public envelope,
    # not accepted merely because a route exists. The probed set must equal the
    # declared set: silently dropping the probe results may not leave the
    # declared rows looking verified.
    declared_405 = {
        operation.operation_id
        for operation in report.contract_operations
        if 405 in operation.declared_statuses
    }
    probed = {
        row.operation.operation_id
        for row in report.rows
        if row.route is not None and row.envelope_probed
    }
    assert probed == declared_405
    assert all(row.envelope_error is None for row in report.rows)


def test_every_unimplemented_operation_answers_only_the_placeholder_error() -> None:
    module = load()
    report = module.build_report(ROOT)
    rows = {row.operation.operation_id: row for row in report.rows}
    placeholders = {operation_id for operation_id, _, _ in report.unimplemented}
    assert placeholders == set(module.KNOWN_UNIMPLEMENTED_OPERATIONS)
    for operation_id in sorted(placeholders):
        row = rows[operation_id]
        assert row.unimplemented is True
        assert row.route is not None, operation_id
        # 405 is the framework's wrong-method reply on the same registered path;
        # 503 is the only status the handler itself can return.
        assert row.route.statuses == frozenset({405, 503}), operation_id
        assert row.route.module == module.UNIMPLEMENTED_ROUTE_MODULE, operation_id


def test_removed_registration_is_reported() -> None:
    module = load()
    base = module.build_report(ROOT)
    remaining = tuple(route for route in base.registered if route.operation_id != "getJob")
    report = compare(module, remaining)
    assert any(
        finding.kind == "declared_not_registered" and finding.operation_id == "getJob"
        for finding in report.findings
    )


def test_injected_undeclared_status_is_reported() -> None:
    module = load()
    base = module.build_report(ROOT)
    injected = tuple(
        dataclasses.replace(route, statuses=route.statuses | {418})
        if route.operation_id == "getJob"
        else route
        for route in base.registered
    )
    report = compare(module, injected)
    findings = [
        finding
        for finding in report.findings
        if finding.kind == "undeclared_response_status"
        and finding.operation_id == "getJob"
        and "418" in finding.detail
    ]
    assert findings


def test_static_extraction_reads_literal_and_error_map(tmp_path: Path) -> None:
    module = load()
    path = tmp_path / "handlers.py"
    path.write_text(
        """
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

_ERRORS = {"bad": (418, "invalid_request_error", "bad")}


def synthetic_error(code: str, request_id: str) -> JSONResponse:
    status, kind, message = _ERRORS[code]
    return JSONResponse({"code": code}, status_code=status)


def handler(request):
    if request.headers.get("x") == "mapped":
        return synthetic_error("bad", "r")
    if request.headers.get("x") == "positional":
        raise HTTPException(417, "teapot")
    return JSONResponse({}, status_code=419)
""",
        encoding="utf-8",
    )
    info = module.parse_module(path, "synthetic", tmp_path)
    factories = module.collect_factories({"synthetic": info})
    analyzer = module.Analyzer({"synthetic": info}, factories, "syntheticOp")
    result = analyzer.function(info.scopes["handler"], {})
    assert {417, 418, 419} <= result.statuses


def test_single_entry_factory_outside_the_placeholder_reports_undetermined(
    tmp_path: Path,
) -> None:
    """Only the placeholder factory may answer an unevaluable code from one entry.

    A single-entry map is not a static answer when the code comes from a
    runtime value: the gate must report the uncertainty and list the candidate
    statuses instead of silently assuming the only entry.
    """
    module = load()
    path = tmp_path / "synthetic_errors.py"
    path.write_text(
        """
from starlette.responses import JSONResponse

_SYNTHETIC_ERRORS = {"only_code": (409, "conflict_error", "only")}


def _error(code: str, request_id: str) -> JSONResponse:
    status, kind, message = _SYNTHETIC_ERRORS[code]
    return JSONResponse({"code": code}, status_code=status)


def handler(request, runtime_code):
    return _error(runtime_code, "request-id")
""",
        encoding="utf-8",
    )
    info = module.parse_module(path, "synthetic_errors", tmp_path)
    factories = module.collect_factories({"synthetic_errors": info})
    analyzer = module.Analyzer({"synthetic_errors": info}, factories, "syntheticOp")
    result = analyzer.function(info.scopes["handler"], {})
    assert result.statuses == set()
    assert [item.detail for item in result.undetermined] == [
        "错误码来自运行时表达式（如 error.code），未静态求值"
    ]
    assert result.undetermined[0].possible_statuses == (409,)


def test_golden_observed_statuses_pin_indirect_and_dynamic_branches() -> None:
    module = load()
    report = module.build_report(ROOT)
    rows = {row.operation.operation_id: row for row in report.rows}
    assert rows["getJob"].route is not None
    assert rows["getJob"].route.statuses == frozenset({200, 404, 405, 503})
    assert rows["downloadArtifactContent"].route is not None
    assert rows["downloadArtifactContent"].route.statuses == frozenset(
        {200, 206, 304, 404, 405, 416, 503}
    )
    # #441 wraps _open_stream in functools.partial; #443 routes voice reads
    # through a module-level _error whose name collides with binding_lifecycle.
    assert rows["streamJobEvents"].route.statuses == frozenset({200, 400, 404, 405, 503})
    assert rows["listVoices"].route.statuses == frozenset({200, 400, 405, 503})
    assert rows["getVoiceRevision"].route.statuses == frozenset({200, 400, 404, 405, 503})


def test_unresolved_endpoint_is_a_finding() -> None:
    module = load()
    base = module.build_report(ROOT)
    existing = next(route for route in base.registered if route.operation_id == "getJob")
    broken = dataclasses.replace(
        existing,
        statuses=frozenset({405}),
        undetermined=(),
        extraction_error="endpoint moved outside the indexed source tree",
    )
    report = compare(
        module,
        tuple(broken if route is existing else route for route in base.registered),
    )
    assert any(
        finding.kind == "status_extraction_failed" and finding.operation_id == "getJob"
        for finding in report.findings
    )


def test_stale_status_baseline_is_rejected() -> None:
    module = load()
    base = module.build_report(ROOT)
    baseline = dict(module.KNOWN_UNDECLARED_STATUSES)
    baseline["getJob"] = frozenset({405, 418})
    report = module.evaluate(
        base.contract_operations,
        base.registered,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=baseline,
    )
    assert any(
        finding.kind == "stale_status_baseline"
        and finding.operation_id == "getJob"
        and "418" in finding.detail
        for finding in report.findings
    )


def test_registered_operation_cannot_join_the_unimplemented_baseline() -> None:
    # `getJob` has a real route and no placeholder, so the baseline itself is
    # what is wrong: the check must reject it through the table, not through
    # "it stopped being unregistered".
    module = load()
    base = module.build_report(ROOT)
    baseline = dict(module.KNOWN_UNIMPLEMENTED_OPERATIONS)
    baseline["getJob"] = "stale baseline entry"
    report = module.evaluate(
        base.contract_operations,
        base.registered,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=baseline,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
        unimplemented=module.load_unimplemented_surface(ROOT),
    )
    assert any(
        finding.kind == "stale_unimplemented_baseline"
        and finding.operation_id == "getJob"
        and "placeholder table does not declare it" in finding.detail
        for finding in report.findings
    )
    assert not any(
        finding.kind == "unimplemented_table_unknown" and finding.operation_id == "getJob"
        for finding in report.findings
    )


def test_placeholder_with_an_extra_status_is_rejected() -> None:
    module = load()
    base = module.build_report(ROOT)
    injected = tuple(
        dataclasses.replace(route, statuses=route.statuses | {200})
        if route.operation_id == "getOperation"
        else route
        for route in base.registered
    )
    report = module.evaluate(
        base.contract_operations,
        injected,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
        unimplemented=module.load_unimplemented_surface(ROOT),
    )
    assert any(
        finding.kind == "unimplemented_placeholder_drift"
        and finding.operation_id == "getOperation"
        and "200" in finding.detail
        for finding in report.findings
    )


def test_placeholder_served_by_another_module_is_rejected() -> None:
    module = load()
    base = module.build_report(ROOT)
    injected = tuple(
        dataclasses.replace(route, module="speechnexus_backend.speech.interfaces.http.jobs")
        if route.operation_id == "getOperation"
        else route
        for route in base.registered
    )
    report = module.evaluate(
        base.contract_operations,
        injected,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
        unimplemented=module.load_unimplemented_surface(ROOT),
    )
    assert any(
        finding.kind == "unimplemented_placeholder_drift" and finding.operation_id == "getOperation"
        for finding in report.findings
    )


def test_placeholder_table_entry_outside_the_baseline_is_rejected() -> None:
    module = load()
    base = module.build_report(ROOT)
    surface = module.load_unimplemented_surface(ROOT)
    extended = dataclasses.replace(
        surface,
        operations={**surface.operations, "getJob": ("GET", "/v1/nexus/jobs/{job_id}")},
    )
    report = module.evaluate(
        base.contract_operations,
        base.registered,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
        unimplemented=extended,
    )
    assert any(
        finding.kind == "unimplemented_table_unknown" and finding.operation_id == "getJob"
        for finding in report.findings
    )


def test_placeholder_error_map_must_stay_single_entry() -> None:
    module = load()
    base = module.build_report(ROOT)
    surface = module.load_unimplemented_surface(ROOT)
    widened = dataclasses.replace(
        surface,
        error_map={**surface.error_map, "backend_unavailable": 503},
    )
    report = module.evaluate(
        base.contract_operations,
        base.registered,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
        unimplemented=widened,
    )
    assert any(finding.kind == "unimplemented_error_map_drift" for finding in report.findings)


def test_missing_placeholder_route_is_reported() -> None:
    module = load()
    base = module.build_report(ROOT)
    remaining = tuple(route for route in base.registered if route.operation_id != "getOperation")
    report = module.evaluate(
        base.contract_operations,
        remaining,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
        unimplemented=module.load_unimplemented_surface(ROOT),
    )
    assert any(
        finding.kind == "declared_not_registered"
        and finding.operation_id == "getOperation"
        and "placeholder" in finding.detail
        for finding in report.findings
    )


def test_declared_405_without_an_envelope_fails_the_gate() -> None:
    # A route can exist and still answer the framework's bare 405 body. The
    # gate must reject it instead of treating the registered route as proof.
    module = load()
    app = FastAPI()

    @app.get("/v1/nexus/widgets", operation_id="listWidgets")
    def list_widgets() -> dict[str, bool]:
        return {"ok": True}

    operation = module.ContractOperation(
        "listWidgets", "GET", "/v1/nexus/widgets", frozenset({200, 405})
    )

    async def execute() -> tuple[httpx.Response, dict[str, str | None]]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            reply = await http.put("/v1/nexus/widgets")
        envelopes = await module.probe_method_not_allowed_envelopes(app, (operation,))
        return reply, envelopes

    reply, envelopes = asyncio.run(execute())
    assert reply.status_code == 405
    assert reply.json() == {"detail": "Method Not Allowed"}
    assert envelopes["listWidgets"] is not None

    route = module.RegisteredRoute(
        method="GET",
        path="/v1/nexus/widgets",
        operation_id="listWidgets",
        statuses=frozenset({200, 405}),
    )
    report = module.evaluate(
        (operation,),
        (route,),
        other_contract_ids=frozenset(),
        baseline_unimplemented={},
        baseline_statuses={},
        unimplemented=module.UnimplementedSurface(),
        method_not_allowed_envelopes=envelopes,
    )
    assert any(
        finding.kind == "method_not_allowed_envelope_missing"
        and finding.operation_id == "listWidgets"
        and "not the public error envelope" in finding.detail
        for finding in report.findings
    )


def test_a_new_route_gets_the_envelope_without_an_app_allowlist() -> None:
    # The falsifiable case from the issue: a route added after the fact must
    # answer the framework 405 through the public envelope, with no path tuple
    # in app.py deciding which paths are covered.
    from speechnexus_backend.app import create_app
    from tests.unit.backend.test_health import _settings

    def widgets() -> dict[str, list[object]]:
        return {"items": []}

    async def execute() -> tuple[httpx.Response, str]:
        app = create_app(settings=_settings())
        app.add_api_route("/v1/nexus/widgets", widgets, methods=["GET"])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            response = await http.put("/v1/nexus/widgets")
        return response, response.json()["error"]["code"]

    response, code = asyncio.run(execute())
    assert response.status_code == 405
    assert code == "method_not_allowed"
    assert response.headers["allow"] == "GET"


def test_unprobed_405_is_a_gate_failure_not_a_pass() -> None:
    # A missing probe mapping may not evaluate to OK: every declared 405 must
    # carry a finding, the report must say FAILED, and no OK tail may print.
    module = load()
    base = module.build_report(ROOT)
    report = module.evaluate(
        base.contract_operations,
        base.registered,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
        unimplemented=module.load_unimplemented_surface(ROOT),
        method_not_allowed_envelopes=None,
    )
    assert all(not row.envelope_probed for row in report.rows)
    assert all(
        row.envelope_error == "the 405 response envelope was not probed"
        for row in report.rows
        if row.route is not None
    )
    assert any(
        finding.kind == "method_not_allowed_envelope_missing"
        and finding.operation_id == "getJob"
        and "405 response envelope was not probed" in finding.detail
        for finding in report.findings
    )
    text = module.format_report(report)
    assert "405-envelope=FAILED" in text
    assert "405-envelope=verified" not in text
    assert "OK: every registered speech route" not in text


def test_dropping_the_probe_results_fails_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Injecting an empty probe is the reviewer's falsification: build_report
    # must fail instead of evaluating with no envelope evidence.
    module = load()

    async def empty_probe(_app: Any, _operations: Any) -> dict[str, str | None]:
        return {}

    monkeypatch.setattr(module, "probe_method_not_allowed_envelopes", empty_probe)
    with pytest.raises(RuntimeError, match="405 envelope probe returned no result"):
        module.build_report(ROOT)


def test_partial_405_probe_mapping_fails_only_the_missing_operation() -> None:
    module = load()
    base = module.build_report(ROOT)
    envelopes = {
        row.operation.operation_id: row.envelope_error
        for row in base.rows
        if row.envelope_probed and row.operation.operation_id != "getJob"
    }
    report = module.evaluate(
        base.contract_operations,
        base.registered,
        other_contract_ids=base.other_contract_ids,
        baseline_unimplemented=module.KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=module.KNOWN_UNDECLARED_STATUSES,
        unimplemented=module.load_unimplemented_surface(ROOT),
        method_not_allowed_envelopes=envelopes,
    )
    assert [(finding.kind, finding.operation_id) for finding in report.findings] == [
        ("method_not_allowed_envelope_missing", "getJob")
    ]
    missing = next(row for row in report.rows if row.operation.operation_id == "getJob")
    assert missing.envelope_probed is False
    assert missing.envelope_error == "the 405 response envelope was not probed"
    text = module.format_report(report)
    assert text.count("405-envelope=FAILED") == 1
    assert text.count("405-envelope=verified") == len(envelopes)
    assert "OK: every registered speech route" not in text


@pytest.mark.parametrize("evidence", ["none", "empty", "partial"])
def test_incomplete_probe_fails_cli_before_reporting(
    evidence: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load()

    async def incomplete_probe(_app: Any, operations: Any) -> Any:
        if evidence == "none":
            return None
        if evidence == "empty":
            return {}
        return {
            operation.operation_id: None
            for operation in operations
            if operation.operation_id != "getJob"
        }

    monkeypatch.setattr(module, "probe_method_not_allowed_envelopes", incomplete_probe)
    assert module.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "405 envelope probe returned no result for " in captured.err
    assert "getJob" in captured.err
    assert "TypeError" not in captured.err


@pytest.mark.parametrize("evidence", ["none", "partial"])
def test_lost_evaluate_evidence_fails_cli_with_failed_rows(
    evidence: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load()
    real_evaluate = module.evaluate

    def miswired_evaluate(*args: Any, **kwargs: Any) -> Any:
        # Coverage receives the complete probe. Lose evidence only at the
        # evaluate call to prove its independent fallback controls the CLI.
        if evidence == "none":
            kwargs["method_not_allowed_envelopes"] = None
        else:
            kwargs["method_not_allowed_envelopes"] = {
                key: value
                for key, value in kwargs["method_not_allowed_envelopes"].items()
                if key != "getJob"
            }
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(module, "evaluate", miswired_evaluate)
    assert module.main([]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("405-envelope=FAILED") == (42 if evidence == "none" else 1)
    assert "getJob declares 405 but the 405 response envelope was not probed" in captured.out
    assert "405-envelope=not probed" not in captured.out
    assert "OK: every registered speech route" not in captured.out


@pytest.mark.parametrize("path", ["/v1/nexus/assets", "/v1/nexus/workspaces"])
def test_probe_enables_service_gated_prefixes_without_calling_services(path: str) -> None:
    module = load()
    operation = module.ContractOperation("serviceGated", "GET", path, frozenset({405}))

    async def execute() -> dict[str, str | None]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.load_app()), base_url="http://localhost"
        ) as client:
            unavailable = await client.put(path)
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"].endswith("service_unavailable")
        result: dict[str, str | None] = await module.probe_method_not_allowed_envelopes(
            module.probe_app(), (operation,)
        )
        return result

    replies = asyncio.run(execute())
    assert replies == {"serviceGated": None}


def test_probe_service_refuses_business_calls() -> None:
    module = load()
    with pytest.raises(AssertionError, match="must not call a service"):
        module.ProbeService().list()


@pytest.mark.parametrize("allow", [None, "", "   "])
def test_complete_405_envelope_without_allow_is_rejected(allow: str | None) -> None:
    module = load()
    response = httpx.Response(
        405,
        headers={} if allow is None else {"Allow": allow},
        json={
            "error": {
                "code": "method_not_allowed",
                "type": "invalid_request_error",
                "message": "Method not allowed",
                "param": None,
                "request_id": module.PROBE_REQUEST_ID,
                "retryable": False,
            }
        },
    )
    assert "Allow" in (module.envelope_problem(response, module.PROBE_REQUEST_ID) or "")


@pytest.mark.parametrize("template", ["/widgets/{id:int}", "/widgets/{rest:path}"])
def test_converter_is_rejected_before_simplified_probe(template: str) -> None:
    module = load()
    app = FastAPI()
    app.add_api_route(template, lambda: {}, methods=["GET"])
    operation = module.ContractOperation("converted", "GET", template, frozenset({405}))
    with pytest.raises(ValueError, match=r"unsupported.*converter"):
        module.fill_path_parameters(template)
    # A plain contract template can still point at a converter in the real app.
    operation = dataclasses.replace(operation, path="/widgets/{id}")
    with pytest.raises(ValueError, match=r"unsupported.*converter"):
        asyncio.run(module.probe_method_not_allowed_envelopes(app, (operation,)))


@pytest.mark.parametrize("template", ["/a{x}b", "/widgets/item-{id}"])
def test_non_segment_parameter_is_rejected_before_simplified_probe(template: str) -> None:
    module = load()
    app = FastAPI()
    app.add_api_route(template, lambda: {}, methods=["GET"])
    operation = module.ContractOperation("nonSegment", "GET", template, frozenset({405}))
    with pytest.raises(ValueError, match=r"unsupported non-segment path parameter") as error:
        asyncio.run(module.probe_method_not_allowed_envelopes(app, (operation,)))
    assert template in str(error.value)
    # The contract side must fail too instead of rendering the embedded placeholder.
    with pytest.raises(ValueError, match=r"unsupported non-segment path parameter") as error:
        module.fill_path_parameters(template)
    assert template in str(error.value)


@pytest.mark.parametrize("mount", ["/widgets", "/widgets/{id}"])
@pytest.mark.parametrize("child", ["/items", "/{id:int}", "/{id}"])
def test_mount_children_fail_closed_before_any_probe(mount: str, child: str) -> None:
    # The verified gap: a mounted sub-application owns routes that
    # methods_for_path cannot see, so the probe would pick a method the child
    # already serves and run that endpoint. The gate must refuse the whole
    # application before it sends a request - including the parameterized mount
    # prefix and a plain static child, which the earlier parameter-only check
    # accepted.
    module = load()
    invoked: list[str] = []
    sub = FastAPI()
    sub.add_api_route(child, lambda: invoked.append(child), methods=["PUT"])
    app = FastAPI()
    app.mount(mount, sub)
    template = mount + child
    operation = module.ContractOperation("mounted", "GET", template, frozenset({405}))
    with pytest.raises(ValueError, match=r"unsupported Mount") as error:
        asyncio.run(module.probe_method_not_allowed_envelopes(app, (operation,)))
    assert mount in str(error.value)
    # Discovery must fail too; a Mount is not flattened into matchable routes.
    with pytest.raises(ValueError, match=r"unsupported Mount") as error:
        tuple(module.flatten_routes(app.routes))
    assert mount in str(error.value)
    assert invoked == []


def test_static_files_mount_stays_supported(tmp_path: Path) -> None:
    # StaticFiles is the one mounted shape the gate can reason about: it serves
    # no route below the prefix and answers only GET/HEAD, so the probe cannot
    # reach business code through it.
    module = load()
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=tmp_path))
    flattened = list(module.flatten_routes(app.routes))
    assert [getattr(route, "path", None) for route in flattened][-1] == "/static"
    assert app.routes[-1].routes == []  # type: ignore[attr-defined]
    operation = module.ContractOperation("widgets", "GET", "/v1/nexus/widgets", frozenset({405}))
    replies = asyncio.run(module.probe_method_not_allowed_envelopes(app, (operation,)))
    # The probe still runs; the empty mount neither blocks nor dispatches.
    assert set(replies) == {"widgets"}


def test_the_repository_probe_dispatches_into_no_endpoint() -> None:
    # The normal corpus must keep the dispatch count at zero: every probe
    # request is answered by the framework's method rejection, not by a handler.
    module = load()
    app = module.probe_app()
    _, contract_operations = module.load_contract(ROOT)
    operations = module.declared_method_not_allowed_operations(contract_operations)
    guard = module.DispatchGuard()
    route_handle = starlette.routing.Route.handle
    mount_handle = starlette.routing.Mount.handle
    contexts = [
        context
        for route in app.router.routes
        if getattr(route, "original_router", None) is not None
        for context in route.effective_route_contexts()
        if context.starlette_route is None
    ]
    handlers = [context.app for context in contexts]
    assert handlers

    envelopes = asyncio.run(
        module.probe_method_not_allowed_envelopes(app, operations, dispatch_guard=guard)
    )

    assert guard.count == 0
    assert guard.calls == []
    assert set(envelopes) == {operation.operation_id for operation in operations}
    assert all(value is None for value in envelopes.values())
    # Every hook is scoped to the probe window and restored afterwards.
    assert starlette.routing.Route.handle is route_handle
    assert starlette.routing.Mount.handle is mount_handle
    for context, handler in zip(contexts, handlers, strict=True):
        assert context.app is handler


def test_a_probe_that_reaches_an_endpoint_fails_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A raw Starlette route with an ASGI endpoint and no methods declaration
    # registers no method that methods_for_path can see, yet Starlette matches it
    # for every method. The probe would run it; the dispatch guard must fail the
    # gate with an explicit reason instead.
    from speechnexus_backend.speech.interfaces.http.unimplemented import methods_for_path

    module = load()
    invoked: list[str] = []

    class AnyMethodEndpoint:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            invoked.append(str(scope["path"]))
            await JSONResponse({"called": True})(scope, receive, send)

    app = FastAPI()
    # getJob declares 405, so the probe asks for that concrete path with a
    # method the matcher reports as unregistered.
    app.router.routes.append(Route("/v1/nexus/jobs/{job_id}", AnyMethodEndpoint()))
    probe_path = "/v1/nexus/jobs/" + module.PROBE_PATH_PARAMETER
    assert methods_for_path(app.router.routes, probe_path) == frozenset()
    monkeypatch.setattr(module, "probe_app", lambda _root=ROOT: app)

    assert module.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "endpoint was invoked during probe" in captured.err
    assert invoked == [probe_path]


def test_dispatch_guard_counts_a_mount_child_the_matcher_cannot_see() -> None:
    # The reported shape: a parameterized mount prefix hides the child route
    # from the matcher, so a wrong-method probe would run the child endpoint.
    # Fail-closed keeps the gate from ever probing it; the guard proves the same
    # request would still be caught if such a route were ever modelled.
    module = load()
    invoked: list[str] = []

    def mounted_endpoint() -> None:
        invoked.append("items")

    sub = FastAPI()
    sub.add_api_route("/items", mounted_endpoint, methods=["PUT"])
    app = FastAPI()
    app.mount("/widgets/{id}", sub)
    guard = module.DispatchGuard()

    async def execute() -> None:
        with guard.installed(app.routes):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                assert (await client.put("/widgets/probe/items")).status_code == 200
        guard.require_no_dispatch()

    with pytest.raises(RuntimeError, match="endpoint was invoked during probe") as error:
        asyncio.run(execute())

    # The child handler is wrapped, so the dispatch names the request and route.
    assert "PUT /widgets/probe/items" in str(error.value)
    assert "route /items" in str(error.value)
    assert invoked == ["items"]


@pytest.mark.parametrize("mount", ["/raw", "/raw/{id}"])
def test_bare_asgi_mount_fails_closed_before_any_probe(mount: str) -> None:
    # Mount.routes is getattr(base, "routes", []), so a bare ASGI application
    # has an empty child list while still accepting every path below the
    # prefix. An empty list is not evidence of safety: the gate must refuse the
    # shape by name and send nothing.
    module = load()
    invoked: list[str] = []

    async def raw_app(scope: Any, receive: Any, send: Any) -> None:
        invoked.append(str(scope["path"]))
        await JSONResponse({"raw": True})(scope, receive, send)

    app = FastAPI()
    app.mount(mount, raw_app)
    operation = module.ContractOperation("rawOp", "GET", "/raw/items", frozenset({405}))
    with pytest.raises(ValueError, match=r"unsupported Mount") as error:
        asyncio.run(module.probe_method_not_allowed_envelopes(app, (operation,)))
    assert mount in str(error.value)
    assert "raw_app" in str(error.value)
    assert "0 child route(s)" in str(error.value)
    with pytest.raises(ValueError, match=r"unsupported Mount"):
        tuple(module.flatten_routes(app.routes))
    assert invoked == []


def test_dispatch_guard_counts_a_bare_asgi_mount() -> None:
    # Fail-closed keeps this shape from ever being probed; if an allowlist ever
    # admits a mounted application, the mount itself must record the dispatch so
    # the count still fails the gate.
    module = load()
    invoked: list[str] = []

    async def raw_app(scope: Any, receive: Any, send: Any) -> None:
        invoked.append(str(scope["path"]))
        await JSONResponse({"raw": True})(scope, receive, send)

    app = FastAPI()
    app.mount("/raw", raw_app)
    guard = module.DispatchGuard()

    async def execute() -> None:
        with guard.installed(app.routes):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                assert (await client.put("/raw/items")).status_code == 200
        guard.require_no_dispatch()

    with pytest.raises(RuntimeError, match="endpoint was invoked during probe") as error:
        asyncio.run(execute())
    assert "PUT /raw/items" in str(error.value)
    assert "mount /raw" in str(error.value)
    assert invoked == ["/raw/items"]


def test_host_routing_is_rejected_before_any_probe() -> None:
    module = load()
    app = FastAPI()
    app.router.routes.append(Host("example.com", FastAPI()))
    with pytest.raises(ValueError, match=r"unsupported Host") as error:
        tuple(module.flatten_routes(app.routes))
    assert "example.com" in str(error.value)


def test_a_generator_endpoint_is_counted_and_fails_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A generator endpoint is invoked directly through dependant.call, not
    # through run_endpoint_function, and APIRoute bypasses the Route.handle
    # hook. Wrapping the ASGI handler must still see the dispatch.
    from speechnexus_backend.speech.interfaces.http.unimplemented import methods_for_path

    module = load()
    ran: list[str] = []

    async def stream_items() -> Any:
        ran.append("generator")
        yield {"chunk": 1}

    app = FastAPI()
    route = fastapi.routing.APIRoute("/v1/nexus/jobs/{job_id}", stream_items, methods=["GET"])
    app.router.routes.append(route)
    # Invisible to the simplified matcher, matched by Starlette for every method.
    route.methods = None
    probe_path = "/v1/nexus/jobs/" + module.PROBE_PATH_PARAMETER
    assert methods_for_path(app.router.routes, probe_path) == frozenset()
    monkeypatch.setattr(module, "probe_app", lambda _root=ROOT: app)

    assert module.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "endpoint was invoked during probe" in captured.err
    assert ran == ["generator"]


def test_dispatch_guard_counts_an_included_generator_endpoint() -> None:
    module = load()
    ran: list[str] = []

    async def stream_items() -> Any:
        ran.append("generator")
        yield {"chunk": 1}

    router = APIRouter()
    router.add_api_route("/gen", stream_items, methods=["GET"])
    app = FastAPI()
    app.include_router(router)
    guard = module.DispatchGuard()

    async def execute() -> None:
        with guard.installed(app.router.routes):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                assert (await client.get("/gen")).status_code == 200
        guard.require_no_dispatch()

    with pytest.raises(RuntimeError, match="endpoint was invoked during probe") as error:
        asyncio.run(execute())
    assert "GET /gen" in str(error.value)
    assert ran == ["generator"]


def test_unenumerable_lazy_include_fails_closed_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A lazy include whose effective contexts cannot be enumerated would leave
    # the dispatch guard blind, so it must fail closed - and the failed window
    # must roll back without leaking hooks or an active guard.
    module = load()
    executed: list[str] = []

    router = APIRouter()

    @router.get("/v1/nexus/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, str]:
        executed.append(job_id)
        return {"job_id": job_id}

    app = FastAPI()
    app.include_router(router)
    included = app.router.routes[-1]
    assert getattr(included, "original_router", None) is not None
    monkeypatch.setattr(type(included), "effective_route_contexts", None, raising=False)
    route_handle = starlette.routing.Route.handle
    mount_handle = starlette.routing.Mount.handle

    guard = module.DispatchGuard()
    with (
        pytest.raises(RuntimeError, match="cannot enumerate the effective routes"),
        guard.installed(app.router.routes),
    ):
        pass
    assert guard.count == 0
    assert module._active_guards == []
    assert module._installed_hooks is None
    assert starlette.routing.Route.handle is route_handle
    assert starlette.routing.Mount.handle is mount_handle

    requests: list[str] = []
    original_request = httpx.AsyncClient.request

    async def counting_request(self: Any, method: str, url: Any, **kwargs: Any) -> Any:
        requests.append(f"{method} {url}")
        return await original_request(self, method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", counting_request)
    monkeypatch.setattr(module, "probe_app", lambda _root=ROOT: app)

    assert module.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot enumerate the effective routes" in captured.err
    assert requests == []
    assert executed == []
    assert module._active_guards == []
    assert starlette.routing.Route.handle is route_handle


def test_dispatch_hooks_restore_after_reentrant_and_interleaved_use() -> None:
    module = load()
    app = FastAPI()
    app.add_api_route("/items", lambda: {}, methods=["GET"])
    route = app.router.routes[-1]
    assert isinstance(route, fastapi.routing.APIRoute)
    original_app = route.app
    route_handle = starlette.routing.Route.handle
    mount_handle = starlette.routing.Mount.handle

    guard = module.DispatchGuard()
    with guard.installed(app.router.routes), guard.installed(app.router.routes):
        assert starlette.routing.Route.handle is not route_handle
        assert route.app is not original_app
    assert starlette.routing.Route.handle is route_handle
    assert starlette.routing.Mount.handle is mount_handle
    assert route.app is original_app
    assert module._installed_hooks is None
    assert module._active_guards == []

    # Exit out of order: hooks stay until the last active guard leaves.
    first = module.DispatchGuard()
    second = module.DispatchGuard()
    first_window = first.installed(app.router.routes)
    second_window = second.installed(app.router.routes)
    first_window.__enter__()
    second_window.__enter__()
    first_window.__exit__(None, None, None)
    assert starlette.routing.Route.handle is not route_handle
    second_window.__exit__(None, None, None)
    assert starlette.routing.Route.handle is route_handle
    assert route.app is original_app
    assert module._installed_hooks is None


@pytest.mark.parametrize("prefix", ["/api", "/widgets"])
def test_include_prefix_is_rejected_before_simplified_probe(prefix: str) -> None:
    module = load()
    router = APIRouter()
    router.add_api_route("/widgets", lambda: {}, methods=["PUT"])
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    operation = module.ContractOperation("prefixed", "PUT", prefix + "/widgets", frozenset({405}))
    with pytest.raises(ValueError, match=r"unsupported.*include_router prefix"):
        asyncio.run(module.probe_method_not_allowed_envelopes(app, (operation,)))
    # Discovery must fail too, and must never mutate the original router paths.
    with pytest.raises(ValueError, match=r"unsupported.*include_router prefix"):
        tuple(module.flatten_routes(app.router.routes))
    assert router.routes[0].path == "/widgets"  # type: ignore[attr-defined]


@pytest.mark.parametrize("path", ["/docs", "/healthz"])
def test_framework_and_health_405_are_enveloped(path: str) -> None:
    module = load()
    operation = module.ContractOperation("operational", "GET", path, frozenset({405}))
    assert asyncio.run(
        module.probe_method_not_allowed_envelopes(module.probe_app(), (operation,))
    ) == {"operational": None}


def test_gate_docs_describe_observed_and_conditional_405() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    audit = (ROOT / "docs/operations/contract-conformance-audit.md").read_text(encoding="utf-8")
    section = audit.split("## 10.", 1)[1].split("## 11.", 1)[0]
    assert "in-process ASGI" in makefile
    assert "405 信封对所有注册路由生效" not in section
    for requirement in ("/docs", "/healthz", "workspace_service_unavailable", "503", "405", "ASGI"):
        assert requirement in section
    assert "这些路径仍由集成/验收测试覆盖" not in section


def test_ci_docs_list_every_405_envelope_condition() -> None:
    document = (ROOT / "docs/validation/ci-validation.md").read_text(encoding="utf-8")
    section = document.split("## 公开语音路由一致性门禁", 1)[1].split("## Design Token", 1)[0]
    for requirement in ("Allow", "retryable=False", "request_id", "回显", "不携带", "detail"):
        assert requirement in section


@pytest.mark.parametrize("defect", ["retryable", "request_id", "detail"])
def test_405_envelope_rejects_retryable_correlation_and_detail(defect: str) -> None:
    module = load()
    body: dict[str, Any] = {
        "error": {
            "code": "method_not_allowed",
            "type": "invalid_request_error",
            "message": "Method not allowed",
            "param": None,
            "request_id": module.PROBE_REQUEST_ID,
            "retryable": False,
        }
    }
    if defect == "detail":
        body["detail"] = "Method Not Allowed"
    else:
        body["error"][defect] = True if defect == "retryable" else "wrong-request-id"
    response = httpx.Response(405, headers={"Allow": "GET"}, json=body)
    assert module.envelope_problem(response, module.PROBE_REQUEST_ID) is not None
