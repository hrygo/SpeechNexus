"""Operation coverage must fail when a binding, consumer or test leg disappears."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "operation_coverage", ROOT / "scripts/dev-env/check-operation-coverage.py"
)
assert SPEC is not None and SPEC.loader is not None


def load() -> Any:
    assert SPEC is not None and SPEC.loader is not None
    module: Any = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


def operation(identifier: str = "getProject", **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": identifier,
        "method": "get",
        "path": "/v1/workbench/projects/{project_id}",
        "binding": {
            "path_entry": True,
            "http_operation_union": True,
            "python_http_schema": True,
        },
        "consumers": ["web/workbench/src/manuscripts/workbench-client.ts"],
        "tests": ["web/workbench/src/manuscripts/workbench-client.test.ts"],
        "status": "bound",
    }
    value.update(overrides)
    return value


def derived(*operations: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "scope": "fixture",
        "contract": "contracts/workbench-openapi.yaml",
        "contract_sha256": "0" * 64,
        "operations": list(operations),
    }


def committed(*operations: dict[str, Any], digest: str = "0" * 64) -> dict[str, Any]:
    value = derived(*operations)
    value["contract_sha256"] = digest
    return value


def codes(module: Any, reviewed: dict[str, Any], fresh: dict[str, Any]) -> set[str]:
    return {finding["code"] for finding in module.differences(reviewed, fresh)}


def test_committed_map_is_current() -> None:
    module = load()
    reviewed = module.json.loads(module.DEFAULT_MAP.read_text())
    assert module.differences(reviewed, module.derive(module.ROOT)) == []


def test_declared_operation_without_mapping_fails() -> None:
    module = load()
    reviewed = committed(operation())
    fresh = derived(operation(), operation("getChapter", path="/v1/workbench/chapters/{id}"))
    assert "operation_missing" in codes(module, reviewed, fresh)


def test_mapping_for_undeclared_operation_fails() -> None:
    module = load()
    reviewed = committed(operation(), operation("retiredProject"))
    assert "operation_extra" in codes(module, reviewed, derived(operation()))


def test_removed_consumer_is_detected() -> None:
    module = load()
    reviewed = committed(operation(consumers=[]))
    assert "consumers_drift" in codes(module, reviewed, derived(operation()))


def test_removed_test_is_detected() -> None:
    module = load()
    reviewed = committed(operation(tests=[]))
    assert "tests_drift" in codes(module, reviewed, derived(operation()))


def test_binding_flag_drift_is_detected() -> None:
    module = load()
    stale = operation()
    stale["binding"]["path_entry"] = False
    assert "binding_drift" in codes(module, committed(stale), derived(operation()))


def test_consumer_present_makes_registration_stale() -> None:
    module = load()
    registered = operation(
        status="consumer_pending",
        consumers=[],
        pending={
            "owner": "C",
            "artifact": "later",
            "unblock_condition": "later",
            "impact": "later",
        },
    )
    assert "stale_registration" in codes(module, committed(registered), derived(operation()))


def test_bound_without_consumer_fails() -> None:
    module = load()
    unbound = operation(consumers=[], status="bound")
    fresh = derived(operation(consumers=[], status="consumer_pending"))
    assert "consumer_missing" in codes(module, committed(unbound), fresh)


@pytest.mark.parametrize("missing", ["owner", "artifact", "unblock_condition", "impact"])
def test_incomplete_registration_fails(missing: str) -> None:
    module = load()
    pending = {
        "owner": "C",
        "artifact": "later",
        "unblock_condition": "later",
        "impact": "later",
    }
    del pending[missing]
    registered = operation(status="consumer_pending", consumers=[], pending=pending)
    fresh = derived(operation(consumers=[], status="consumer_pending"))
    assert "registration_incomplete" in codes(module, committed(registered), fresh)


def test_contract_digest_change_fails() -> None:
    module = load()
    reviewed = committed(operation(), digest="f" * 64)
    assert "contract_drift" in codes(module, reviewed, derived(operation()))


def test_write_preserves_registrations(tmp_path: Path) -> None:
    module = load()
    existing = committed(
        operation(
            "getChapter",
            status="consumer_pending",
            consumers=[],
            pending={
                "owner": "C",
                "artifact": "artifact",
                "unblock_condition": "condition",
                "impact": "impact",
            },
        )
    )
    target = tmp_path / "map.json"
    target.write_text(module.json.dumps(existing))
    fresh = module.merge_registrations(
        derived(operation("getChapter", status="consumer_pending", consumers=[])),
        copy.deepcopy(existing),
    )
    assert fresh["operations"][0]["pending"]["owner"] == "C"


def test_spy_target_method_name_counts_as_behavior_test(tmp_path: Path) -> None:
    module = load()
    source = tmp_path / "web/workbench/src/assistant/assistant-session.test.ts"
    source.parent.mkdir(parents=True)
    source.write_text('vi.spyOn(client, "assistantTurn").mockResolvedValue(turn());')
    operation = {
        "id": "getAssistantTurn",
        "method": "get",
        "path": "/v1/workbench/assistant-turns/{turn_id}",
    }
    pattern = module.test_pattern(
        operation, module.client_methods(module.ROOT).get(("GET", operation["path"]))
    )
    assert pattern.search(source.read_text())


def status_operation() -> dict[str, str]:
    return {"id": "getWorkbenchStatusV2", "method": "get", "path": "/v2/workbench/status"}


def test_role_string_sharing_the_client_method_word_is_not_evidence(tmp_path: Path) -> None:
    """#788: `getByRole("status")` asserts an ARIA role, not the client's `status()` method."""
    module = load()
    source = tmp_path / "web/workbench/src/realtime/input-device-picker.test.tsx"
    source.parent.mkdir(parents=True)
    source.write_text(
        'expect(screen.getByRole("status")).toHaveTextContent("所选设备不在当前列表");\n'
        'const status = document.querySelector(".file-task-detail > p[role=status]");\n'
        'const label = "<p role=\\"status\\">";\n'
    )
    pattern = module.test_pattern(status_operation(), "status")
    assert pattern.search(source.read_text()) is None


def test_client_method_spy_target_is_evidence(tmp_path: Path) -> None:
    """The same word is evidence once a spy binds it to the object that owns the method."""
    module = load()
    source = tmp_path / "web/workbench/src/manuscripts/editor-session.test.ts"
    source.parent.mkdir(parents=True)
    source.write_text('const read = vi.spyOn(client, "status").mockResolvedValue(state());\n')
    pattern = module.test_pattern(status_operation(), "status")
    assert pattern.search(source.read_text())


def test_unbound_quoted_property_does_not_bind_the_method(tmp_path: Path) -> None:
    """A fake member typed by name (`WorkbenchClient["status"]`) is a string, not a spy."""
    module = load()
    source = tmp_path / "web/workbench/src/file-workflow/tts-task-host.test.tsx"
    source.parent.mkdir(parents=True)
    source.write_text(
        'status: vi.fn<WorkbenchClient["status"]>(),\nconst copy = "任务状态：尚未确认";\n'
    )
    pattern = module.test_pattern(status_operation(), "status")
    assert pattern.search(source.read_text()) is None


def book_operation() -> dict[str, str]:
    return {"id": "getBook", "method": "get", "path": "/v1/workbench/books/{book_id}"}


def python_source(tmp_path: Path, name: str, body: str) -> Path:
    source = tmp_path / "tests/unit/backend" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(body)
    return source


def test_absolute_paths_below_tests_are_discovered(tmp_path: Path) -> None:
    """#792: the scan passes absolute paths, so the scope must be read after `relative_to`."""
    module = load()
    python_test = module.ROOT / "tests/unit/backend/test_workbench_http_versions.py"
    assert python_test.is_absolute()
    assert module.is_test_candidate(module.ROOT, python_test)
    assert module.is_test_candidate(module.ROOT, module.ROOT / "tests/conftest.py")
    assert not module.is_test_candidate(module.ROOT, module.ROOT / "scripts/dev-env/check_x.py")
    assert module.is_test_candidate(module.ROOT, module.ROOT / "web/workbench/src/a.test.ts")
    # A path relative to the root is the same file, and a relative path outside it is not.
    relative = Path("tests/unit/backend/test_workbench_http_versions.py")
    assert not relative.is_absolute()
    assert module.is_test_candidate(module.ROOT, relative)
    assert not module.is_test_candidate(module.ROOT, Path("scripts/dev-env/check_x.py"))


def test_gate_own_unit_test_is_not_evidence_of_its_operations() -> None:
    """Its operationIds are fixture data: crediting them would be the gate citing itself."""
    module = load()
    assert module.is_test_candidate(module.ROOT, module.ROOT / module.GATE_SELF_TEST) is False


def test_python_request_to_the_route_is_evidence(tmp_path: Path) -> None:
    module = load()
    source = python_source(
        tmp_path,
        "test_books.py",
        "async def test_reads_a_book(client: Any) -> None:\n"
        "    book = await client.get(f\"/v1/workbench/books/{book['id']}\")\n",
    )
    assert module.python_evidence(book_operation(), source)


def test_python_request_to_a_sibling_route_is_not_evidence(tmp_path: Path) -> None:
    """`.../{book_id}/chapters` serves the chapter listing, not the book read."""
    module = load()
    source = python_source(
        tmp_path,
        "test_chapters.py",
        "async def test_lists_chapters(client: Any) -> None:\n"
        "    await client.get(f\"/v1/workbench/books/{book['id']}/chapters\")\n",
    )
    assert module.python_evidence(book_operation(), source) is False


def test_python_request_with_another_verb_is_not_evidence(tmp_path: Path) -> None:
    """`POST /v1/workbench/projects` creates; it does not cover the project listing."""
    module = load()
    source = python_source(
        tmp_path,
        "test_projects.py",
        "async def test_creates(client: Any) -> None:\n"
        '    await client.post("/v1/workbench/projects", json={"name": "synthetic"})\n',
    )
    operation = {"id": "listProjects", "method": "get", "path": "/v1/workbench/projects"}
    assert module.python_evidence(operation, source) is False


def test_python_contract_binding_call_is_evidence(tmp_path: Path) -> None:
    module = load()
    source = python_source(
        tmp_path,
        "test_contract.py",
        'assert validate_response("getBook", 200, {"body": payload}) is payload\n',
    )
    assert module.python_evidence(book_operation(), source)


def test_python_comment_and_docstring_mention_is_not_evidence(tmp_path: Path) -> None:
    """#792: naming the operation is not reaching it, however often the file says it."""
    module = load()
    source = python_source(
        tmp_path,
        "test_comment.py",
        '"""The `getBook` route is covered elsewhere."""\n'
        "def test_placeholder() -> None:\n"
        "    # getBook: read by id, asserted in the repository suite\n"
        '    assert "getBook" in __doc__\n',
    )
    assert module.python_evidence(book_operation(), source) is False


def test_python_fixture_key_is_not_an_operation_handle(tmp_path: Path) -> None:
    """`getVoiceRevision-invalid-request` is another operation's fixture, not this one."""
    module = load()
    source = python_source(
        tmp_path,
        "test_voice.py",
        'body = _example("getVoiceRevision-invalid-request")["body"]\n'
        'assert validate_response("retireVoiceRevision", 422, {"body": body})\n',
    )
    operation = {
        "id": "getVoiceRevision",
        "method": "get",
        "path": "/v1/workbench/voices/{voice_id}/revisions/{revision}",
    }
    assert module.python_evidence(operation, source) is False


def test_python_handle_literal_in_a_routed_table_is_evidence(tmp_path: Path) -> None:
    module = load()
    source = python_source(
        tmp_path,
        "test_errors.py",
        'CASES = [(404, "resource_not_found", "getBook")]\n',
    )
    assert module.python_evidence(book_operation(), source)


def test_python_client_method_word_in_a_domain_call_is_not_evidence(tmp_path: Path) -> None:
    """#792: `transcript.revise()`/`snapshot.status()` are domain methods, not the client."""
    module = load()
    source = python_source(
        tmp_path,
        "test_versions.py",
        "corrected = transcript.revise(expected_revision=1, text=BODY, now=NOW)\n"
        'assert snapshot.status()["abort_recovery"] is None\n',
    )
    revision = {
        "id": "createTranscriptRevision",
        "method": "post",
        "path": "/v1/workbench/transcripts/{transcript_id}/revisions",
    }
    assert module.python_evidence(revision, source) is False
    assert module.python_evidence(status_operation(), source) is False


def test_python_live_operation_id_assertion_is_evidence(tmp_path: Path) -> None:
    module = load()
    source = python_source(
        tmp_path,
        "test_schema.py",
        'assert schema["paths"]["/v2/workbench/status"]["get"]["operationId"] == '
        '"getWorkbenchStatusV2"\n',
    )
    assert module.python_evidence(status_operation(), source)


def test_python_comment_showing_a_binding_call_is_not_evidence(tmp_path: Path) -> None:
    """A comment that shows the call is still a comment: the tokens never form a call."""
    module = load()
    source = python_source(
        tmp_path,
        "test_comment_binding.py",
        "def test_placeholder() -> None:\n"
        '    # validate_response("getBook", 200, {"body": payload})\n'
        '    """例：validate_response("getBook", 200, {"body": payload})"""\n'
        "    assert True\n",
    )
    assert module.python_binding_operations(str(source)) == frozenset()
    assert module.python_evidence(book_operation(), source) is False
