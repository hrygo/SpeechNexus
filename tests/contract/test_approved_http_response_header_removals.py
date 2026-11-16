"""Only an exact approved response-header removal passes the gate.

The gate had no approval slot for "a response header was removed", so a header the
contract must stop promising could only be changed by disabling the check. This
module pins the slot that was added for #852: it is keyed to one source, path,
method, status, version pair and header, it must carry an issue and a reason, it
is honoured only when the response's other metadata is unchanged, and it rejects
on both sides -- a removal without the entry, and an entry that does not name the
removal that actually happened.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/speech-openapi.yaml"
PATH = "/v1/nexus/jobs/{job_id}/cancellations"
POINTER = f"{SOURCE}:paths/{PATH}/post/responses/409"
VERSION = "0.1.0.dev18"
HEADER = "retry-after"
FINDING = f"{POINTER}: response header removed: {HEADER}"
REASON = (
    "Only `execution_cleanup_pending` sent this header. The code is no longer "
    "retryable and no longer sends it, because nothing finishes that cleanup on "
    "its own; keeping the declaration would promise a header that never arrives."
)


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "http_change_cli_header_removals",
        ROOT / "scripts/dev-env/check-http-contract-changes.py",
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def document(*, headers: dict[str, Any] | None, description: str = "Conflict.") -> dict[str, Any]:
    response: dict[str, Any] = {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }
    if headers is not None:
        response["headers"] = headers
    return {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": VERSION},
        "paths": {PATH: {"post": {"responses": {"409": response}}}},
    }


def apply(
    module_: ModuleType, base: dict[str, Any], current: dict[str, Any], findings: list[str]
) -> tuple[list[str], list[str]]:
    return cast(
        tuple[list[str], list[str]],
        module_._apply_approved_response_header_removals(
            SOURCE, base, current, VERSION, VERSION, list(findings)
        ),
    )


def test_the_header_removal_registry_is_frozen() -> None:
    table = module()._APPROVED_RESPONSE_HEADER_REMOVALS
    assert table == {
        (SOURCE, PATH, "post", "409", VERSION, VERSION, HEADER): {
            "issue": "#852",
            "reason": REASON,
        }
    }


def test_an_exact_entry_filters_the_removal_it_names() -> None:
    loaded = module()
    base = document(headers={"Retry-After": {"$ref": "#/components/headers/RetryAfter"}})
    current = document(headers=None)

    remaining, approved = apply(loaded, base, current, [FINDING])

    assert remaining == []
    assert approved == [f"{FINDING} [#852: {REASON}]"]


def test_a_removal_without_an_entry_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = module()
    monkeypatch.setattr(loaded, "_APPROVED_RESPONSE_HEADER_REMOVALS", {})
    base = document(headers={"Retry-After": {}})
    current = document(headers=None)

    assert apply(loaded, base, current, [FINDING]) == ([FINDING], [])


def test_an_entry_that_names_another_header_cannot_approve_the_real_removal() -> None:
    """The approval is per header: naming one header never covers another's removal.

    The removed header is `x-other` here, so the registered `retry-after` entry has
    nothing to say and the finding survives: a generalised key cannot wave through a
    removal it does not name.
    """
    loaded = module()
    base = document(headers={"X-Other": {}})
    current = document(headers=None)

    remaining, approved = apply(loaded, base, current, [FINDING])

    assert approved == []
    assert remaining == [FINDING]


def test_an_entry_is_inert_when_the_comparison_contains_no_such_removal() -> None:
    """An identical-sides comparison (or `main` after the merge) must stay green.

    The entry is registered on the version pair the change lands in, so a
    comparison whose baseline already lost the header has nothing to approve and
    must not report a mismatch.
    """
    loaded = module()
    assert apply(loaded, document(headers=None), document(headers=None), []) == ([], [])
    assert apply(
        loaded,
        document(headers={"Retry-After": {}}),
        document(headers={"Retry-After": {}}),
        [],
    ) == ([], [])


def test_an_entry_is_inactive_for_another_version_pair() -> None:
    loaded = module()
    base = document(headers={"Retry-After": {}})
    current = document(headers=None)

    remaining, approved = cast(
        tuple[list[str], list[str]],
        loaded._apply_approved_response_header_removals(
            SOURCE, base, current, "0.1.0.dev17", VERSION, [FINDING]
        ),
    )

    assert (remaining, approved) == ([FINDING], [])


def test_an_entry_cannot_carry_another_response_edit() -> None:
    loaded = module()
    base = document(headers={"Retry-After": {}})
    current = copy.deepcopy(document(headers=None))
    current["paths"][PATH]["post"]["responses"]["409"]["content"] = {
        "application/json": {"schema": {"$ref": "#/components/schemas/Other"}}
    }

    remaining, approved = apply(loaded, base, current, [FINDING])

    assert approved == []
    assert remaining == [
        FINDING,
        f"{POINTER}: approved response header removal entry does not match: "
        "response metadata other than the removed header changed",
    ]


def test_an_entry_without_an_issue_or_reason_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = module()
    key = (SOURCE, PATH, "post", "409", VERSION, VERSION, HEADER)
    base = document(headers={"Retry-After": {}})
    current = document(headers=None)
    for broken in ({"issue": "", "reason": REASON}, {"issue": "#852", "reason": ""}):
        monkeypatch.setattr(loaded, "_APPROVED_RESPONSE_HEADER_REMOVALS", {key: dict(broken)})
        remaining, approved = apply(loaded, base, current, [FINDING])
        assert approved == []
        assert remaining == [
            FINDING,
            f"{POINTER}: approved response header removal entry does not match: "
            "entry must include a non-empty issue and reason",
        ]


def test_the_approval_only_covers_the_declared_header_on_a_component_response() -> None:
    """The live contract declares the response as a component reference.

    Resolving that ``$ref`` is what makes the approval verifiable at all: without
    it the entry would silently never match and the removal would stay rejected.
    """
    loaded = module()
    base: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": VERSION},
        "paths": {
            PATH: {"post": {"responses": {"409": {"$ref": "#/components/responses/Conflict"}}}}
        },
        "components": {
            "responses": {
                "Conflict": {
                    "description": "Conflict.",
                    "headers": {"Retry-After": {"$ref": "#/components/headers/RetryAfter"}},
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                        }
                    },
                }
            }
        },
    }
    current = copy.deepcopy(base)
    del current["components"]["responses"]["Conflict"]["headers"]

    remaining, approved = apply(loaded, base, current, [FINDING])

    assert remaining == []
    assert approved == [f"{FINDING} [#852: {REASON}]"]
