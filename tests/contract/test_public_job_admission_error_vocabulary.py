"""Every admission refusal has its own public answer; none falls back to an outage.

#828 review B3: ``rejection_code`` used to keep two named codes and fold every
other admission refusal into ``job_unavailable`` ("Job storage is unavailable.",
``retryable: false``). A healthy store then answered a storage outage for causes
an operator or an ordinary retry repairs -- an elapsed readiness receipt (#828), a
drifted receipt identity, a disabled instance, a recipe mismatch, a lost version
race. The fold is gone: the mapping is total over the declared admission
vocabulary, ``job_unavailable`` is reserved for a store that really refused the
work, and every public code this operation can emit is declared on its response.

The guard is structural rather than a hand-kept list of expectations:

* the public mapping and the declared vocabulary must be the same set, so adding a
  code to either side alone fails;
* every rejection code raised inside the modules that decide or run admission must
  be in the vocabulary, so a new raise site cannot bypass the mapping;
* an unmapped code raises instead of borrowing the outage answer;
* each mapped public code is declared on the status its entry publishes, so the
  contract stays machine-readable (#828 review B2).
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.public_jobs import JobAdmissionRejectedError
from speechnexus_backend.speech.domain.admission import (
    ADMISSION_REJECTION_CODES,
    CAPACITY_REJECTION_CODES,
)
from speechnexus_backend.speech.infrastructure.public_job_admission import (
    _PUBLIC_CODES,
    PublicJobAdmissionRejectedError,
    rejection_code,
)
from speechnexus_backend.speech.interfaces.http.job_errors import _ERRORS, job_error
from tests.support.contracts import validator

SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
ROOT = Path(__file__).resolve().parents[2]
# Every module that raises a rejection error class, frozen. The guard asserts this
# set equals what the scan finds in both directions, so a module that starts raising
# (or stops and hides the literals behind a helper) fails until the list and the
# vocabulary are decided together. The earlier list held six of these thirteen, and
# seven modules could raise an unmapped code while the guard stayed green
# (#828 review M2).
ADMISSION_MODULES = (
    "backend/src/speechnexus_backend/speech/domain/admission.py",
    "backend/src/speechnexus_backend/speech/domain/owners.py",
    "backend/src/speechnexus_backend/speech/domain/runtime_intents.py",
    "backend/src/speechnexus_backend/speech/domain/runtime_observations.py",
    "backend/src/speechnexus_backend/speech/infrastructure/admission_postgres.py",
    "backend/src/speechnexus_backend/speech/infrastructure/admission_recovery_sql.py",
    "backend/src/speechnexus_backend/speech/infrastructure/admission_sql.py",
    "backend/src/speechnexus_backend/speech/infrastructure/builtin_voice_qualification.py",
    "backend/src/speechnexus_backend/speech/infrastructure/owners_sql.py",
    "backend/src/speechnexus_backend/speech/infrastructure/qualification_admission_postgres.py",
    "backend/src/speechnexus_backend/speech/infrastructure/runtime_observation_sql.py",
    "backend/src/speechnexus_backend/speech/infrastructure/runtime_sql.py",
    "backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_sql.py",
)
REJECTION_ERRORS = frozenset(
    {"AdmissionRejectedError", "OwnerRejectedError", "RuntimeRejectedError"}
)


@dataclass(frozen=True, slots=True)
class RaisePoint:
    """One ``<Rejection>Error(...)`` call site and what it can put in ``error.code``."""

    module: str
    function: str
    lineno: int
    argument: str
    literals: frozenset[str]
    producer: str | None
    dynamic: bool
    raised: str


# A raise site that forwards a variable instead of literals. Every entry names the
# exact site, the codes that can reach it and why the guard accepts the indirection,
# so a variable-passed code is never silently trusted: a raise point that forwards a
# variable it does not declare here fails (#828 review M2).
PASSTHROUGH_RAISE_POINTS: dict[tuple[str, str, str, str | None], dict[str, Any]] = {
    (
        "domain/admission.py",
        "require_live_authority",
        "error.code",
        "OwnerRejectedError",
    ): {
        "reason": (
            "Re-raises the owner-authority refusal verbatim; owners.py is scanned, so "
            "every code this can carry is a declared literal there."
        ),
        "producer": "OwnerRejectedError",
    },
    (
        "domain/admission.py",
        "require_live_authority",
        "error.code",
        "RuntimeRejectedError",
    ): {
        "reason": (
            "Re-raises the readiness-receipt refusal verbatim; the runtime modules are "
            "scanned, so every code this can carry is a declared literal there or the "
            "bounded `expired_code` of RuntimeReadiness.require_valid."
        ),
        "producer": "RuntimeRejectedError",
    },
    (
        "infrastructure/admission_sql.py",
        "_runtime",
        "error.code",
        "RuntimeRejectedError",
    ): {
        "reason": (
            "The runtime loader forwards the rejection it loaded with; the same "
            "scanned producers bound it."
        ),
        "producer": "RuntimeRejectedError",
    },
    (
        "infrastructure/admission_sql.py",
        "admit_in_transaction",
        "job.error_code or 'reconciliation_required'",
        None,
    ): {
        "reason": (
            "The in-transaction Job write reports its own refusal; jobs_sql only emits "
            "these codes and test_the_jobs_write_passthrough_is_bounded re-derives that "
            "set from its source."
        ),
        "allowed": ("idempotency_conflict", "reconciliation_required"),
    },
    (
        "domain/runtime_observations.py",
        "require_valid",
        "expired_code",
        None,
    ): {
        "reason": (
            "The caller states which code an elapsed receipt carries so admission can "
            "separate it from a stale identity; the default and every call site are "
            "pinned to the declared pair below."
        ),
        "allowed": ("reconciliation_required", "readiness_expired"),
    },
}


def _modules_raising_rejection_errors() -> set[str]:
    """Every product module whose source raises one of the three rejection errors.

    Read from the tree, not from `ADMISSION_MODULES`: the walk is the invariant, and the
    frozen list is the claim being checked against it.
    """
    found: set[str] = set()
    package = ROOT / "backend" / "src" / "speechnexus_backend"
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
            if name in REJECTION_ERRORS:
                speech = path.relative_to(
                    ROOT / "backend" / "src" / "speechnexus_backend" / "speech"
                )
                found.add(speech.as_posix())
                break
    return found


def _literal_derived(node: ast.AST) -> bool:
    """True when every possible value of this expression is a string literal."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.IfExp):
        # The condition only selects between the branches.
        return _literal_derived(node.body) and _literal_derived(node.orelse)
    if isinstance(node, ast.BoolOp):
        return all(_literal_derived(value) for value in node.values)
    return False


def _rejection_points(module: str) -> list[RaisePoint]:
    """Every rejection raise point of one module, with its enclosing context."""
    path = ROOT / module
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    points: list[RaisePoint] = []
    stack: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        stack.append(node)
        try:
            if isinstance(node, ast.Call):
                function = node.func
                name = (
                    function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
                )
                if name in REJECTION_ERRORS and node.args:
                    argument = node.args[0]
                    literals = frozenset(
                        sub.value
                        for sub in ast.walk(argument)
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    )
                    # A conditional that *selects* between literals is still
                    # literal-derived; a name, attribute or call as the value means
                    # the code is decided elsewhere.
                    dynamic = not _literal_derived(argument)
                    owner = next(
                        (
                            item
                            for item in reversed(stack)
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        ),
                        None,
                    )
                    handler = next(
                        (item for item in reversed(stack) if isinstance(item, ast.ExceptHandler)),
                        None,
                    )
                    producer = None
                    if handler is not None and handler.type is not None:
                        producer = (
                            handler.type.id
                            if isinstance(handler.type, ast.Name)
                            else getattr(handler.type, "attr", None)
                        )
                    points.append(
                        RaisePoint(
                            module=module.split("speech/", 1)[1],
                            function=owner.name if owner else "<module>",
                            lineno=node.lineno,
                            argument=ast.unparse(argument),
                            literals=literals,
                            producer=producer,
                            dynamic=dynamic,
                            raised=name,
                        )
                    )
            for child in ast.iter_child_nodes(node):
                visit(child)
        finally:
            stack.pop()

    visit(tree)
    return points


ALL_RAISE_POINTS: tuple[RaisePoint, ...] = tuple(
    point for module in ADMISSION_MODULES for point in _rejection_points(module)
)


def _point_label(point: RaisePoint) -> str:
    return f"{point.module}:{point.function}:{point.lineno} ({point.argument})"


def _entry_for(point: RaisePoint) -> dict[str, Any] | None:
    return PASSTHROUGH_RAISE_POINTS.get(
        (point.module, point.function, point.argument, point.producer)
    )


def _create_job() -> dict[str, Any]:
    for item in cast(dict[str, Any], DOCUMENT["paths"]).values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == "createJob":
                return specification
    raise AssertionError("the speech contract declares no createJob operation")


def _declared_codes(status: str) -> list[str]:
    schema = _create_job()["responses"][status]["content"]["application/json"]["schema"]
    return cast(list[str], schema["allOf"][1]["properties"]["error"]["properties"]["code"]["enum"])


def _raised_codes(path: Path) -> set[str]:
    """String literals passed to a rejection error constructor in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    raised: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        if name not in REJECTION_ERRORS:
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                raised.add(argument.value)
    return raised


def test_the_public_mapping_is_total_over_the_declared_vocabulary() -> None:
    # A code in the vocabulary without a public answer would raise at request time,
    # and a public entry outside it would be unreachable: the sets must match.
    assert CAPACITY_REJECTION_CODES <= ADMISSION_REJECTION_CODES
    assert set(_PUBLIC_CODES) == ADMISSION_REJECTION_CODES - CAPACITY_REJECTION_CODES


def test_the_frozen_module_list_is_every_module_that_can_raise_a_rejection() -> None:
    # The scan must be anchored to the repository, never to the list it checks: comparing
    # `ADMISSION_MODULES` with the modules it parses is a list agreeing with itself, so a
    # fourteenth module that raises an undeclared code stayed invisible (#828 review F4).
    # This walks all product code instead and compares the two directions, so both a new
    # module and a listed module that stopped raising are red.
    found = sorted(_modules_raising_rejection_errors())
    declared = sorted(module.split("speech/", 1)[1] for module in ADMISSION_MODULES)
    assert found == declared, {
        "unlisted": sorted(set(found) - set(declared)),
        "listed_but_quiet": sorted(set(declared) - set(found)),
    }


def test_every_raise_point_contributes_only_declared_codes() -> None:
    # Per raise point, not per module union: one new literal anywhere is red even
    # when the module's other raise sites are fine.
    offenders = [
        f"{_point_label(point)} carries {sorted(point.literals - ADMISSION_REJECTION_CODES)}"
        for point in ALL_RAISE_POINTS
        if point.literals - ADMISSION_REJECTION_CODES
    ]
    assert offenders == []


def test_every_variable_passed_code_is_declared_as_a_passthrough() -> None:
    # A raise point that forwards a name, attribute or call instead of literals can
    # carry any code the guard cannot see, so it needs an explicit entry.
    offenders = [
        _point_label(point)
        for point in ALL_RAISE_POINTS
        if point.dynamic and _entry_for(point) is None
    ]
    assert offenders == []


def test_every_passthrough_entry_matches_a_real_raise_point() -> None:
    # A stale entry would licence a raise site that no longer exists.
    live = {
        (point.module, point.function, point.argument, point.producer)
        for point in ALL_RAISE_POINTS
        if point.dynamic
    }
    stale = sorted(set(PASSTHROUGH_RAISE_POINTS) - live)
    assert stale == [], stale


def test_every_passthrough_entry_names_its_reason_and_bound() -> None:
    for key, entry in PASSTHROUGH_RAISE_POINTS.items():
        assert entry.get("reason"), f"{key} has no reason"
        assert bool(entry.get("allowed")) != bool(entry.get("producer")), (
            f"{key} must bound its codes by `allowed` or by `producer`, not both"
        )
        allowed = entry.get("allowed") or ()
        assert set(allowed) <= ADMISSION_REJECTION_CODES, (key, allowed)


def test_the_producer_bounded_passthroughs_stay_inside_the_vocabulary() -> None:
    # `producer` means "whatever that class raises can reach this site". That bound
    # is only real while the class cannot raise an undeclared code, which the
    # per-raise-point test proves; this one ties the entry to the class and shows the
    # forwarded set is non-empty, so a misnamed producer is red instead of vacuous.
    producers = {
        cast(str, entry["producer"])
        for entry in PASSTHROUGH_RAISE_POINTS.values()
        if entry.get("producer")
    }
    assert producers, "no producer-bounded entry to check"
    for producer in producers:
        carried = set().union(
            *(point.literals for point in ALL_RAISE_POINTS if point.raised == producer)
        )
        assert carried, producer
        assert carried <= ADMISSION_REJECTION_CODES, (producer, carried)


def test_the_declared_expired_code_is_the_only_bound_on_the_receipt_wrapper() -> None:
    allowed = set(
        PASSTHROUGH_RAISE_POINTS[
            (
                "domain/runtime_observations.py",
                "require_valid",
                "expired_code",
                None,
            )
        ]["allowed"]
    )
    source = (
        ROOT / "backend/src/speechnexus_backend/speech/domain/runtime_observations.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    signature_default = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "require_valid"
    )
    names = [argument.arg for argument in signature_default.args.kwonlyargs]
    defaults = signature_default.args.kw_defaults
    default = defaults[names.index("expired_code")]
    assert default is not None
    assert isinstance(default, ast.Constant) and default.value in allowed

    offenders: list[str] = []
    for module in ADMISSION_MODULES:
        path = ROOT / module
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
            if name != "require_valid":
                continue
            for keyword in node.keywords:
                if keyword.arg != "expired_code":
                    continue
                if not (isinstance(keyword.value, ast.Constant) and keyword.value.value in allowed):
                    offenders.append(f"{module}:{node.lineno}")
    assert offenders == []


def test_the_jobs_write_passthrough_is_bounded_by_its_source() -> None:
    allowed = set(
        PASSTHROUGH_RAISE_POINTS[
            (
                "infrastructure/admission_sql.py",
                "admit_in_transaction",
                "job.error_code or 'reconciliation_required'",
                None,
            )
        ]["allowed"]
    )
    source = (ROOT / "backend/src/speechnexus_backend/speech/infrastructure/jobs_sql.py").read_text(
        encoding="utf-8"
    )
    emitted = set()
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        if name != "JobResult":
            continue
        for keyword in node.keywords:
            if keyword.arg != "error_code":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                emitted.add(value.value)
            else:
                # A code decided at runtime is not bounded by this module's literals: only
                # collecting constants let a value read from the database pass the whole
                # guard (#828 review F3). It must be a literal, or the site must be
                # registered as a passthrough with its own producer bound.
                offenders.append(
                    f"jobs_sql.py:{keyword.value.lineno} reports a non-literal error code "
                    f"({ast.unparse(value)})"
                )
    assert offenders == [], offenders
    assert emitted, "the jobs write must report its own refusal codes"
    assert emitted <= allowed, emitted - allowed


def test_the_vocabulary_is_exactly_what_the_raise_points_carry() -> None:
    # The other direction: a vocabulary code that no raise point can carry is dead
    # weight, and it is how a typo survives a spelling change.
    carried = set().union(
        *(point.literals for point in ALL_RAISE_POINTS),
        *(set(entry.get("allowed") or ()) for entry in PASSTHROUGH_RAISE_POINTS.values()),
    )
    assert carried >= ADMISSION_REJECTION_CODES
    assert carried <= ADMISSION_REJECTION_CODES


def test_an_unmapped_code_answers_the_declared_defect_envelope() -> None:
    # There is no default and no bare exception: an unmapped vocabulary code becomes
    # the declared 500 `admission_state_invalid` answer, so the client meets a
    # contract-valid envelope instead of a bare 500 or a false storage outage
    # (#828 review M1).
    with pytest.raises(PublicJobAdmissionRejectedError) as raised:
        rejection_code("readiness_receipt_expired_typo")
    assert raised.value.code == "admission_state_invalid"

    response = job_error("admission_state_invalid", "rid-unmapped")
    assert response.status_code == 500
    body = cast(dict[str, Any], json.loads(bytes(response.body)))
    assert body["error"]["code"] == "admission_state_invalid"
    assert body["error"]["type"] == "api_error"
    assert body["error"]["retryable"] is False


def test_capacity_keeps_the_lease_answer() -> None:
    assert rejection_code("capacity_exhausted") == "job_capacity_exhausted"
    assert rejection_code("quota_exceeded") == "job_capacity_exhausted"


def test_the_storage_answer_is_reserved_for_the_store() -> None:
    # No admission code may map to the outage answer any more: that answer is only
    # produced by the store itself refusing the read or the write.
    assert "job_unavailable" not in set(_PUBLIC_CODES.values())


@pytest.mark.parametrize(
    ("admission_code", "public"),
    [
        ("idempotency_conflict", "idempotency_conflict"),
        ("readiness_expired", "engine_not_ready"),
        ("reconciliation_required", "engine_state_mismatch"),
        ("not_found", "engine_state_mismatch"),
        ("policy_or_resource_disabled", "engine_resource_disabled"),
        ("recipe_mismatch", "recipe_not_admitted"),
        ("version_conflict", "job_admission_contended"),
        ("stale_fencing_token", "job_admission_contended"),
        ("owner_busy", "job_admission_contended"),
        ("lease_not_active", "job_request_expired"),
        ("invalid_counter", "admission_state_invalid"),
        ("invalid_execution_request", "admission_state_invalid"),
    ],
)
def test_each_admission_cause_names_its_own_recovery(admission_code: str, public: str) -> None:
    assert rejection_code(admission_code) == public
    assert public in _ERRORS


# The statuses this operation narrows to an explicit code list. 409 still points at
# the shared ``Conflict`` component, whose body accepts any declared code, so it has
# no per-operation vocabulary to check.
NARROWED_STATUSES = ("422", "500", "503")


def test_every_mapped_public_code_is_declared_on_its_status() -> None:
    # The contract is the machine-readable surface: a mapped code that its status
    # does not enumerate would only be discoverable by reading the backend.
    declared = {status: set(_declared_codes(status)) for status in NARROWED_STATUSES}
    for public in set(_PUBLIC_CODES.values()):
        status = str(_ERRORS[public][0])
        if status in declared:
            assert public in declared[status], (public, status)


def test_the_narrowed_statuses_do_not_advertise_codes_they_cannot_return() -> None:
    # The other direction: the enum is the operation's vocabulary, so every entry
    # must be a code this backend really emits with that status.
    for status in NARROWED_STATUSES:
        for public in _declared_codes(status):
            assert public in _ERRORS, public
            assert str(_ERRORS[public][0]) == status, (public, status)


def test_the_retry_rule_follows_the_published_semantics() -> None:
    # `retryable` is true only when the same request can succeed without an
    # out-of-band change: the contention races, a capacity delay and the unknown
    # execution outcome. Operator-step recoveries are false and say so in `message`
    # (#828 review B1).
    import json

    from speechnexus_backend.speech.interfaces.http.job_errors import job_error

    retryable = set()
    for code in _ERRORS:
        body = cast(dict[str, Any], json.loads(bytes(job_error(code, "contract-vocabulary").body)))
        if body["error"]["retryable"]:
            retryable.add(code)
    assert retryable == {
        "job_outcome_unknown",
        "job_capacity_exhausted",
        "job_admission_contended",
        # The cleanup wait needs no out-of-band step: the same request succeeds once
        # the original execution confirms stop (#828 review M5).
        "execution_cleanup_pending",
    }


def test_the_error_table_never_invents_a_storage_answer() -> None:
    # Before #828 review M1 the table rewrote an undeclared code to `job_unavailable`,
    # which is exactly how a healthy store answered "Job storage is unavailable.".
    response = job_error("engine_retired", "rid-undeclared")
    assert response.status_code == 500
    body = cast(dict[str, Any], json.loads(bytes(response.body)))
    assert body["error"]["code"] == "admission_state_invalid"
    assert body["error"]["type"] == "api_error"
    assert body["error"]["retryable"] is False
    assert "engine_retired" in body["error"]["message"]
    assert "storage is unavailable" not in body["error"]["message"].lower()


def test_the_acceptance_set_is_derived_from_the_mapping() -> None:
    from speechnexus_backend.speech.application.public_jobs import (
        _ADMISSION_REJECTION_CODES,
        _CREATE_REFUSAL_CODES,
        _SUBMIT_REFUSAL_CODES,
    )

    expected = (
        frozenset(_PUBLIC_CODES.values()) | CAPACITY_REJECTION_CODES | {"job_capacity_exhausted"}
    )
    assert expected == _ADMISSION_REJECTION_CODES
    assert _CREATE_REFUSAL_CODES == _ADMISSION_REJECTION_CODES | _SUBMIT_REFUSAL_CODES


@pytest.mark.parametrize(
    "code",
    [
        "job_capacity_exhausted",
        "engine_not_ready",
        "admission_state_invalid",
        "asset_not_ready",
        "workspace_quota_missing",
        None,
    ],
)
def test_no_store_refusal_returns_silently(code: str | None) -> None:
    # A silent return used to drop the caller into `JobRejectedError("contract")`,
    # which answered the storage outage. Every code now either names its own public
    # answer or becomes the declared defect (#828 review M1).
    from speechnexus_backend.speech.application.public_jobs import (
        _raise_admission_rejection,
    )

    with pytest.raises(JobAdmissionRejectedError) as raised:
        _raise_admission_rejection(code)
    expected = "admission_state_invalid" if code in {None, "workspace_quota_missing"} else str(code)
    assert raised.value.code == expected


def test_the_store_mechanism_codes_are_exactly_what_the_reason_mapping_declares() -> None:
    """A store refusal either has its own reason or must name a public answer.

    #828 review F2: the set used to be a second hand-written literal beside the reason
    mapping, so `engine_retired` could be added to it and pass the whole guard -- the
    store's refusal then answered `503 job_unavailable`, "Job storage is unavailable.",
    which is the #828 response verbatim. The set is now derived from the mapping, and
    this test proves the two directions: every mechanism code carries a non-`contract`
    reason, and every code that reaches `_raise_admission_rejection` as a declared answer
    is *not* a mechanism code (so it cannot be dropped into that branch).
    """
    from speechnexus_backend.speech.application import public_jobs
    from speechnexus_backend.speech.application.public_jobs import _ADMISSION_REJECTION_CODES

    mechanism = public_jobs._STORE_MECHANISM_CODES
    assert mechanism, "the store must report at least one refusal of its own"
    # Direction one: each member is exactly the set of codes with a declared reason.
    assert {code for code in mechanism if public_jobs._rejection_reason(code) != "contract"} == set(
        mechanism
    )
    # Direction two: the mapping is the only source, so every code outside the set is
    # classified as `contract` -- the caller then has to name a public answer instead.
    assert public_jobs._rejection_reason("engine_retired") == "contract"
    assert "engine_retired" not in mechanism
    # And no mechanism code may also be a public admission answer: that overlap is what
    # would let a store code stand in for an admission cause.
    assert not (mechanism & _ADMISSION_REJECTION_CODES)
