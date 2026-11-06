"""Speech route conformance must fail on a removed route or a new status."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path
from typing import Any

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
    assert module.KNOWN_UNDECLARED_STATUSES == {}
    assert report.known_status_gaps == ()


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


def test_registered_operation_in_unimplemented_baseline_is_rejected() -> None:
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
    )
    assert any(
        finding.kind == "stale_unimplemented_baseline" and finding.operation_id == "getJob"
        for finding in report.findings
    )


def test_placeholder_with_an_extra_status_is_rejected() -> None:
    module = load()
    base = module.build_report(ROOT)
    injected = tuple(
        dataclasses.replace(route, statuses=route.statuses | {200})
        if route.operation_id == "getVoiceDraft"
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
        and finding.operation_id == "getVoiceDraft"
        and "200" in finding.detail
        for finding in report.findings
    )


def test_placeholder_served_by_another_module_is_rejected() -> None:
    module = load()
    base = module.build_report(ROOT)
    injected = tuple(
        dataclasses.replace(route, module="speechnexus_backend.speech.interfaces.http.jobs")
        if route.operation_id == "getVoiceDraft"
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
        and finding.operation_id == "getVoiceDraft"
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
    remaining = tuple(route for route in base.registered if route.operation_id != "getVoiceDraft")
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
        and finding.operation_id == "getVoiceDraft"
        and "placeholder" in finding.detail
        for finding in report.findings
    )
