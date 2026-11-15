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
from pathlib import Path
from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.admission import (
    ADMISSION_REJECTION_CODES,
    CAPACITY_REJECTION_CODES,
)
from speechnexus_backend.speech.infrastructure.public_job_admission import (
    _PUBLIC_CODES,
    rejection_code,
)
from speechnexus_backend.speech.interfaces.http.job_errors import _ERRORS
from tests.support.contracts import validator

SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
ROOT = Path(__file__).resolve().parents[2]
# Modules that decide admission or run it inside the create transaction: every
# ``*RejectedError`` raised here can reach ``rejection_code``.
ADMISSION_MODULES = (
    "backend/src/speechnexus_backend/speech/domain/admission.py",
    "backend/src/speechnexus_backend/speech/domain/owners.py",
    "backend/src/speechnexus_backend/speech/domain/runtime_observations.py",
    "backend/src/speechnexus_backend/speech/infrastructure/admission_sql.py",
    "backend/src/speechnexus_backend/speech/infrastructure/admission_recovery_sql.py",
    "backend/src/speechnexus_backend/speech/infrastructure/admission_postgres.py",
)
REJECTION_ERRORS = frozenset(
    {"AdmissionRejectedError", "OwnerRejectedError", "RuntimeRejectedError"}
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


def test_the_scan_reaches_the_raise_sites() -> None:
    # A refactor that hides the literals behind a helper would silently disarm the
    # per-module guards below, so their union is asserted to be non-empty.
    assert set().union(*(_raised_codes(ROOT / module) for module in ADMISSION_MODULES))


@pytest.mark.parametrize("module", ADMISSION_MODULES)
def test_every_raised_admission_code_is_declared(module: str) -> None:
    # ``runtime_observations.require_valid`` raises the ``expired_code`` it is given,
    # so that module legitimately contributes no literal; the others must stay inside
    # the vocabulary so a new raise site cannot bypass the public mapping.
    raised = _raised_codes(ROOT / module)
    assert raised <= ADMISSION_REJECTION_CODES, raised - ADMISSION_REJECTION_CODES


def test_an_unmapped_code_fails_loudly_instead_of_becoming_an_outage() -> None:
    # The whole point of the change: there is no default. A future code reaches this
    # boundary as a defect, never as a false "Job storage is unavailable.".
    with pytest.raises(KeyError):
        rejection_code("readiness_receipt_expired_typo")


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
    }
