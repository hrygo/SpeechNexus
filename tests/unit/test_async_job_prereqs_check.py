"""The async-Job preflight must mirror the runtime it claims to check.

`scripts/dev-env/check-async-job-prereqs.py` copies the startup probes, the role
guard and the writer column surfaces from production so it can run with the
deployer's plain `python3`. A copy is only safe if something fails when the two
drift, which is what this module does: every constant is compared verbatim with
its production declaration, and the write probes are checked against the grants
the runbook tells operators to install.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_async_job_prereqs", ROOT / "scripts/dev-env/check-async-job-prereqs.py"
)
GRANT = re.compile(r'^GRANT (SELECT|INSERT|UPDATE) \(([^)]*)\) ON "[^"]+"\."?([A-Za-z0-9_]+)"?')


def checker() -> Any:
    assert SPEC is not None and SPEC.loader is not None
    module: Any = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = module
    SPEC.loader.exec_module(module)
    return module


def granted_columns() -> set[tuple[str, str, str]]:
    from speechnexus_backend.speech.infrastructure.coordination_grants import (
        coordination_runtime_grants,
    )
    from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
        cancellation_reconciliation_grants,
    )
    from speechnexus_backend.speech.infrastructure.job_schema import (
        job_dispatch_grants,
        job_runtime_grants,
    )
    from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants

    statements = [
        *job_runtime_grants("speech", "sn_role_x"),
        *job_dispatch_grants("speech", "sn_role_x"),
        *public_job_grants("speech", "sn_role_x"),
        *cancellation_reconciliation_grants("speech", "sn_role_x"),
        *coordination_runtime_grants("speech", speech_role="sn_role_x", gateway_role="sn_gw_x"),
    ]
    granted: set[tuple[str, str, str]] = set()
    for statement in statements:
        match = GRANT.match(statement.strip())
        if match is None:
            continue
        privilege, columns, table = match.groups()
        for column in columns.split(","):
            granted.add((table, privilege, column.strip()))
    return granted


def test_dispatch_startup_probes_match_runtime() -> None:
    from speechnexus_backend import job_dispatch_runtime

    module = checker()
    assert tuple(statement for _, statement in module.DISPATCH_PROBES) == (
        job_dispatch_runtime.DISPATCH_STARTUP_PROBES
    )
    assert tuple(statement for _, statement in module.VOICE_VALIDATION_PROBES) == (
        job_dispatch_runtime.DISPATCH_VOICE_VALIDATION_PROBES
    )
    assert module.DISPATCH_ROLE_GUARD == job_dispatch_runtime.DISPATCH_ROLE_GUARD_SQL


def test_role_guard_parameters_follow_the_placeholder_order() -> None:
    """`?` 没有名字,顺序就是语义;唯一一处说明重复次数的地方必须被钉住."""

    from speechnexus_backend import job_dispatch_runtime
    from speechnexus_backend.job_dispatch_runtime import role_guard_parameters

    params = role_guard_parameters("sn_jobs_role", "speech")
    assert params == (
        "sn_jobs_role",
        "speech",
        "sn_jobs_role",
        "speech",
        "sn_jobs_role",
        "sn_jobs_role",
    )
    assert job_dispatch_runtime.DISPATCH_ROLE_GUARD_SQL.count("?") == len(params)


def test_writer_surfaces_match_runtime() -> None:
    from speechnexus_backend import job_runtime
    from speechnexus_backend.speech.infrastructure.job_writer_columns import (
        JOB_WRITER_INSERT_COLUMNS,
    )

    module = checker()
    assert module.WRITER_REQUIRED_SELECT == job_runtime.REQUIRED_SELECT
    assert module.WRITER_INSERT_COLUMNS == JOB_WRITER_INSERT_COLUMNS
    assert module.WRITER_UPDATE_COLUMNS == job_runtime._JOB_UPDATE_SURFACE
    # Deletion execution is the only writer surface with a DELETE, and it is one
    # table: a copy that drifts would let the preflight pass while the startup
    # gate refuses, or vice versa.
    assert frozenset(module.WRITER_DELETE_TABLES) == job_runtime._JOB_DELETE_TABLES


def test_required_tables_are_declared_by_the_runtime() -> None:
    from speechnexus_backend import job_runtime

    module = checker()
    declared = (
        set(job_runtime._JOB_DECLARED_TABLES)
        | set(job_runtime._ADMISSION_REQUIRED_SELECT)
        | set(job_runtime._ADMISSION_RESERVATION_TABLES)
    )
    assert set(module.REQUIRED_JOB_TABLES) <= declared


def test_dispatch_kinds_match_the_dispatchable_literal() -> None:
    from typing import get_args

    from speechnexus_backend.speech.domain.job_dispatch import DispatchJobKind

    module = checker()
    assert frozenset(get_args(DispatchJobKind)) == module.DISPATCHABLE_KINDS


def gate_module() -> Any:
    """The deployment's startup gate, loaded the way its deployment loads it."""
    path = ROOT / "deploy/job-startup-gate.py"
    spec = importlib.util.spec_from_file_location("job_startup_gate_for_parity", path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_assembled_lanes_are_the_gateway_configs_the_gate_serves() -> None:
    # #1075: the self-check promised "the same lanes as the gate" in a comment and
    # drifted to asr/tts while the gate grew the voice lanes. The promise is now
    # checked: both sides must name the same set or this test is red.
    assert set(checker().ASSEMBLED_KINDS) == set(gate_module().GATEWAY_CONFIGS)


def test_the_resident_variant_rule_is_the_gates_own_function() -> None:
    # Not a copy: the self-check calls the same domain function, so the two emit
    # the same finding for the same selection. A second spelling of the rule is
    # exactly how "the preflight passes, the gate refuses" happens.
    module = checker()
    gate = gate_module()
    for kinds in (("tts", "voice_design"), ("asr", "voice_clone"), ("asr", "tts")):
        assert module.resident_variant_findings(list(kinds)) == gate.gateway_lane_findings(kinds)


def test_dispatch_write_probes_are_covered_by_the_documented_grants() -> None:
    module = checker()
    granted = granted_columns()
    missing = [
        f"{table}.{column} ({privilege})"
        for table, privilege, columns in module.DISPATCH_WRITE_PROBES
        for column in columns
        if (table, privilege, column) not in granted
    ]
    assert missing == [], (
        "the runbook's grant functions do not cover the dispatcher's writes: " + ", ".join(missing)
    )
