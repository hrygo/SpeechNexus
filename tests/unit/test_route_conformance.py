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
    assert contract_ids - registered_ids == set(module.KNOWN_UNIMPLEMENTED_OPERATIONS)
    assert report.known_unimplemented == tuple(sorted(module.KNOWN_UNIMPLEMENTED_OPERATIONS))
    assert len(module.KNOWN_UNDECLARED_STATUSES) == 14


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
from starlette.responses import JSONResponse

_ERRORS = {"bad": (418, "invalid_request_error", "bad")}


def synthetic_error(code: str, request_id: str) -> JSONResponse:
    status, kind, message = _ERRORS[code]
    return JSONResponse({"code": code}, status_code=status)


def handler(request):
    if request.headers.get("x") == "mapped":
        return synthetic_error("bad", "r")
    return JSONResponse({}, status_code=419)
""",
        encoding="utf-8",
    )
    info = module.parse_module(path, "synthetic", tmp_path)
    factories = module.collect_factories({"synthetic": info})
    analyzer = module.Analyzer({"synthetic": info}, factories, "syntheticOp")
    result = analyzer.function(info.scopes["handler"], {})
    assert {418, 419} <= result.statuses


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
