"""Generated meeting HTTP boundaries with no database or LLM execution."""

import asyncio
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.llm import LlmError, LlmProfile
from speechnexus_backend.workbench.application.meetings import MeetingView, WorkbenchMeetings
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTaskView
from speechnexus_backend.workbench.application.version_ports import VersionStoreError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.meetings import MeetingSourceUnsupportedError
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTaskConflictError
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
    Unset,
)
from speechnexus_backend.workbench.interfaces.http_meetings import create_meeting_router
from speechnexus_backend.workbench.interfaces.meeting_views import meeting_payload, minutes_payload
from tests.unit.backend.test_workbench_meetings import NOW, meeting, minutes, sealed


class Service(WorkbenchMeetings):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failure: BaseException | None = None

    @property
    def llm_enabled(self) -> bool:
        return True

    @property
    def llm_profile(self) -> LlmProfile:
        return minutes(sealed()).profile

    def call(self, **kw: Any) -> None:
        self.calls.append(kw)
        if self.failure:
            raise self.failure

    async def create(self, **kw: Any) -> MeetingView:
        self.call(**kw)
        return MeetingView(meeting())

    async def get(self, **kw: Any) -> MeetingView:
        self.call(**kw)
        return MeetingView(sealed())

    async def seal(self, **kw: Any) -> MeetingView:
        self.call(**kw)
        return MeetingView(sealed())

    async def create_minutes(self, **kw: Any) -> MinutesTaskView:
        self.call(**kw)
        return minutes(sealed())

    async def get_minutes(self, **kw: Any) -> MinutesTaskView:
        self.call(**kw)
        return minutes(sealed())


def app(service: WorkbenchMeetings | None) -> FastAPI:
    value = create_app(
        None,
        meetings=service,
        default_workspace_id="workspace-demo",
        deployment_id="synthetic",
        cursor_key=b"synthetic-32-byte-cursor-key-00000",
        max_json_bytes=4096,
        body_timeout_seconds=0.02,
    )
    if service is None:
        value.include_router(
            create_meeting_router(
                None,
                default_workspace_id="workspace-demo",
                max_json_bytes=4096,
                body_timeout_seconds=0.02,
            )
        )
    return value


def client(value: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=value), base_url="http://synthetic")


CREATE = "/v1/workbench/projects/project-demo/meetings"
PATH = "/v1/workbench/meetings/meeting-demo"
OPS = [
    ("post", CREATE, {"display_name": "合成会议", "transcript_id": "transcript-demo"}),
    ("get", PATH, None),
    ("post", PATH + "/seal", {"expected_transcript_revision": "1"}),
    ("post", PATH + "/minutes", {"template_revision": "1"}),
    ("get", PATH + "/minutes/minutes-demo", None),
]


async def test_five_operations_use_generated_shapes_and_pin_server_workspace() -> None:
    service = Service()
    async with client(app(service)) as c:
        for index, (method, path, body) in enumerate(OPS):
            r = await c.request(
                method, path, json=body, headers={"Idempotency-Key": "original-key"}
            )
            assert r.status_code == [201, 200, 200, 202, 200][index], r.text
            assert r.headers["cache-control"] == "no-store" and r.headers["x-request-id"]
        assert service.calls[0] == {
            "project_id": "project-demo",
            "workspace_id": "workspace-demo",
            "transcript_id": "transcript-demo",
            "speech_session_id": None,
            "save_audio": False,
            "display_name": "合成会议",
            "key": "original-key",
        }
        assert (
            service.calls[2]["expected_revision"] == 1 and service.calls[2]["text"] is Unset.VALUE
        )
        assert service.calls[3]["template_revision"] == 1 and service.calls[3]["llm_model"] is None
        assert service.calls[3]["expected_source_revision"] is None
        assert service.calls[4]["minutes_id"] == "minutes-demo"
        schema = (await c.get("/openapi.json")).json()
        # Voice reads and public downloads answer stable unavailable errors when unconfigured.
        assert sum(len(p) for p in schema["paths"].values()) == 23
        status = (await c.get("/v2/workbench/status")).json()
        assert status["workbench_enabled"] and status["llm_enabled"]
        assert status["minutes_profile"] == {
            "template_revision": "1",
            "llm_model": "synthetic-model",
        }


async def test_minutes_optional_source_cas_reaches_application_as_integer() -> None:
    service = Service()
    async with client(app(service)) as c:
        r = await c.post(
            PATH + "/minutes", json={"template_revision": "1", "expected_source_revision": "2"}
        )
        assert r.status_code == 202, r.text
    assert service.calls[0]["expected_source_revision"] == 2


@pytest.mark.parametrize("operation", OPS)
async def test_disabled_routes_fail_explicitly_without_phantom_success(operation: Any) -> None:
    method, path, body = operation
    async with client(app(None)) as c:
        r = await c.request(method, path, json=body)
        assert r.status_code == 503 and r.json()["error"]["code"] == "meetings_unavailable"


@pytest.mark.parametrize(
    "error,status,code,index",
    [
        (ResourceNotFoundError(), 404, "resource_not_found", 1),
        (IdempotencyConflictError(), 409, "idempotency_conflict", 0),
        (RevisionConflictError(1, 2), 409, "revision_conflict", 2),
        (InvalidVersionChangeError("private"), 422, "invalid_change", 2),
        (MeetingSourceUnsupportedError(), 422, "meeting_source_unsupported", 0),
        (MinutesTaskConflictError(), 409, "minutes_conflict", 3),
        (LlmError("disabled"), 503, "llm_disabled", 3),
        (LlmError("profile_mismatch"), 422, "llm_profile_mismatch", 3),
        (LlmError("source_not_sealed"), 409, "llm_source_not_sealed", 3),
        (LlmError("source_changed"), 409, "llm_source_changed", 3),
        (LlmError("invalid_input"), 422, "llm_invalid_input", 3),
        (VersionStoreError(sqlstate=None, commit_unknown=True), 503, "outcome_unknown", 3),
        (VersionStoreError(sqlstate=None, commit_unknown=False), 503, "store_unavailable", 4),
        (RuntimeError("private"), 500, "internal_error", 1),
    ],
)
async def test_stable_errors_do_not_expose_private_exception_text(
    error: BaseException,
    status: int,
    code: str,
    index: int,
) -> None:
    service = Service()
    service.failure = error
    method, path, body = OPS[index]
    async with client(app(service)) as c:
        r = await c.request(method, path, json=body)
        assert r.status_code == status and r.json()["error"]["code"] == code, r.text
        assert "private" not in r.text and r.headers["x-should-retry"] == "false"


@pytest.mark.parametrize(
    "content,headers,status",
    [
        ('{"display_name":"x","display_name":"y"}', {"Content-Type": "application/json"}, 400),
        ('{"display_name":NaN}', {"Content-Type": "application/json"}, 400),
        ('{"display_name":"\\ud800"}', {"Content-Type": "application/json"}, 400),
        ("{}", {"Content-Type": "text/plain"}, 415),
        ("x" * 4097, {"Content-Type": "application/json"}, 413),
        (
            '{"display_name":"x","transcript_id":"t","save_audio":"false"}',
            {"Content-Type": "application/json"},
            400,
        ),
        (
            "{}",
            [
                ("Content-Type", "application/json"),
                ("Idempotency-Key", "one"),
                ("Idempotency-Key", "two"),
            ],
            400,
        ),
    ],
)
async def test_ingress_refuses_unsafe_input_before_service(
    content: str, headers: Any, status: int
) -> None:
    service = Service()
    async with client(app(service)) as c:
        r = await c.post(CREATE, content=content, headers=headers)
        assert r.status_code == status, r.text
        assert not service.calls
        r = await c.get(PATH + "?workspace_id=other")
        assert r.status_code == 400 and not service.calls


async def test_body_deadline_and_cancellation_are_not_successes() -> None:
    service = Service()

    async def slow() -> Any:
        yield b"{"
        await asyncio.sleep(0.05)
        yield b"}"

    async with client(app(service)) as c:
        r = await c.post(CREATE, content=slow(), headers={"Content-Type": "application/json"})
        assert r.status_code == 408 and not service.calls
        service.failure = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await c.get(PATH)


@pytest.mark.parametrize(
    "state", ["queued", "running", "succeeded", "failed", "cancelled", "expired"]
)
def test_minutes_projection_has_only_public_content_and_stable_failure(state: str) -> None:
    value = minutes(sealed())
    changes: dict[str, Any] = {"status": state}
    if state == "running":
        changes.update(attempt=1, attempt_token="synthetic-token")
    if state == "succeeded":
        changes["text"] = " 合成纪要 e\u0301😀\n "
    if state == "failed":
        changes["error"] = "unavailable"
    payload = minutes_payload("meeting-demo", replace(value, task=replace(value.task, **changes)))
    assert payload["status"] == state and payload["source_revision"] == "2"
    assert payload["minutes_text"] == (changes.get("text") if state == "succeeded" else None)
    assert bool(payload["error"]) == (state == "failed")
    assert all(
        k not in payload
        for k in ("binding_id", "usage", "profile_sha256", "attempt_token", "system_prompt")
    )
    linked = sealed().attach_minutes(replace(value.task, **changes), now=NOW)
    result = meeting_payload(
        MeetingView(linked, replace(value, task=replace(value.task, **changes)))
    )
    assert result["status"] == ("minutes_ready" if state == "succeeded" else "minutes_pending")
