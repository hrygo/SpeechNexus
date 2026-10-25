"""Exercise serving consumers, including an accepted add whose reply is pending."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.types import Message

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "deploy/engines/vllm-asr-lifecycle/src"


@pytest.fixture
def scope_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT))
    return importlib.import_module("speechnexus_vllm_asr.scope")


class Collector:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


class NativeEngine:
    """Use the native q=None cancellation pattern with a controlled send boundary."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.allow_add_reply = asyncio.Event()
        self.allow_output = asyncio.Event()
        self.submitted: list[str] = []
        self.aborted: list[str] = []
        self.collectors: list[Collector] = []
        self.abort_before_add_returned = False
        self.add_returned = False
        self.fail_abort = False
        self.fail_add = False
        self.log_requests = False
        self.output_processor = SimpleNamespace(
            abort_requests=lambda identities, internal: list(identities)
        )
        self.engine_core = SimpleNamespace(abort_requests_async=self._core_abort)

    async def _add_request(
        self,
        request: Any,
        prompt: Any,
        parent: Any,
        index: int,
        queue: Collector,
    ) -> None:
        self.submitted.append(request.request_id)
        self.entered.set()
        if len(self.submitted) == 2:
            self.two_entered.set()
        await self.allow_add_reply.wait()
        if self.fail_add:
            raise RuntimeError("synthetic lost send reply")
        self.add_returned = True

    async def add_request(self, request_id: str, prompt: Any, params: Any) -> Collector:
        queue = Collector(request_id + "-internal")
        self.collectors.append(queue)
        await self._add_request(
            SimpleNamespace(request_id=queue.request_id, external_req_id=request_id),
            None,
            None,
            0,
            queue,
        )
        return queue

    async def abort(self, request_ids: Any, *, internal: bool = False) -> None:
        assert internal
        ids = [request_ids] if isinstance(request_ids, str) else request_ids
        await self._core_abort(self.output_processor.abort_requests(ids, internal=internal))

    async def _core_abort(self, request_ids: list[str]) -> None:
        if self.fail_abort:
            raise RuntimeError("synthetic abort transport unavailable")
        if not self.add_returned and not self.fail_add:
            self.abort_before_add_returned = True
        self.aborted.extend(request_ids)

    async def generate(
        self, prompt: Any, sampling_params: Any, request_id: str
    ) -> AsyncIterator[str]:
        queue = None
        try:
            queue = await self.add_request(request_id, prompt, sampling_params)
            await self.allow_output.wait()
            yield "synthetic transcript"
        except asyncio.CancelledError, GeneratorExit:
            if queue is not None:
                await self.abort(queue.request_id, internal=True)
            raise
        finally:
            if queue is not None:
                queue.close()


async def _consumer(engine: Any, request_id: str) -> list[str]:
    return [item async for item in engine.generate({}, None, request_id)]


async def test_scope_cleans_accepted_add_after_frontend_cancel(scope_module: Any) -> None:
    engine = NativeEngine()
    controller = scope_module.AsrScopeController(engine, max_scopes=2)
    scope = controller.open()
    token = scope_module.CURRENT_SCOPE.set(scope)
    try:
        task = asyncio.create_task(_consumer(controller.engine, "original"))
    finally:
        scope_module.CURRENT_SCOPE.reset(token)
    await asyncio.wait_for(engine.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    controller.seal(scope)
    # The accepted add has not returned. Abort must not overtake it, and cleanup
    # observation timeout must not cancel the native add or forget the scope.
    assert not await controller.wait_closed(wait_timeout_seconds=0.01)
    assert controller.pending_scopes == 1
    assert engine.submitted == ["original-internal"]
    assert engine.aborted == []
    engine.allow_add_reply.set()
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert engine.aborted == ["original-internal"]
    assert engine.collectors[0].closed
    assert not engine.abort_before_add_returned


async def test_scope_cleans_add_that_fails_after_possible_send(scope_module: Any) -> None:
    engine = NativeEngine()
    engine.fail_add = True
    controller = scope_module.AsrScopeController(engine)
    scope = controller.open()
    token = scope_module.CURRENT_SCOPE.set(scope)
    task = asyncio.create_task(_consumer(controller.engine, "original"))
    scope_module.CURRENT_SCOPE.reset(token)
    await asyncio.wait_for(engine.entered.wait(), 1)
    engine.allow_add_reply.set()
    with pytest.raises(RuntimeError, match="lost send reply"):
        await task
    controller.seal(scope)
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert engine.aborted == ["original-internal"]
    assert engine.collectors[0].closed


async def test_cleanup_sends_recorded_ids_after_frontend_map_was_removed(scope_module: Any) -> None:
    engine = NativeEngine()
    engine.allow_add_reply.set()
    engine.allow_output.set()
    controller = scope_module.AsrScopeController(engine)
    scope = controller.open()
    token = scope_module.CURRENT_SCOPE.set(scope)
    await _consumer(controller.engine, "original")
    scope_module.CURRENT_SCOPE.reset(token)
    # Native abort can remove its frontend mapping before an IPC error. Looking
    # up that mapping again must not drop this accepted request's abort.
    engine.output_processor.abort_requests = lambda identities, internal: []
    controller.seal(scope)
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert engine.aborted == ["original-internal"]


async def test_scope_covers_language_and_multiple_chunks(scope_module: Any) -> None:
    engine = NativeEngine()
    engine.allow_add_reply.set()
    engine.allow_output.set()
    controller = scope_module.AsrScopeController(engine)
    scope = controller.open()
    token = scope_module.CURRENT_SCOPE.set(scope)
    try:
        for request_id in ["outer-lang_detect", "outer-0", "outer-1"]:
            assert await _consumer(controller.engine, request_id) == ["synthetic transcript"]
    finally:
        scope_module.CURRENT_SCOPE.reset(token)
        controller.seal(scope)
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert set(engine.aborted) == {
        "outer-lang_detect-internal",
        "outer-0-internal",
        "outer-1-internal",
    }
    assert all(queue.closed for queue in engine.collectors)


async def test_sealed_scope_rejects_late_child_without_send(scope_module: Any) -> None:
    engine = NativeEngine()
    controller = scope_module.AsrScopeController(engine)
    scope = controller.open()
    controller.seal(scope)
    token = scope_module.CURRENT_SCOPE.set(scope)
    try:
        with pytest.raises(asyncio.CancelledError):
            await _consumer(controller.engine, "late-child")
    finally:
        scope_module.CURRENT_SCOPE.reset(token)
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert engine.submitted == []


async def test_two_scopes_do_not_abort_each_other(scope_module: Any) -> None:
    engine = NativeEngine()
    engine.allow_add_reply.set()
    controller = scope_module.AsrScopeController(engine)
    scopes = [controller.open(), controller.open()]
    tasks = []
    for index, scope in enumerate(scopes):
        token = scope_module.CURRENT_SCOPE.set(scope)
        tasks.append(asyncio.create_task(_consumer(controller.engine, f"request-{index}")))
        scope_module.CURRENT_SCOPE.reset(token)
    await asyncio.wait_for(engine.entered.wait(), 1)
    # Ensure both independently scheduled consumers have registered.
    await asyncio.wait_for(engine.two_entered.wait(), 1)
    controller.seal(scopes[0])
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    assert not await controller.wait_closed(wait_timeout_seconds=0.01)
    assert "request-0-internal" in engine.aborted
    assert "request-1-internal" not in engine.aborted
    engine.allow_output.set()
    assert await tasks[1] == ["synthetic transcript"]
    controller.seal(scopes[1])
    assert await controller.wait_closed(wait_timeout_seconds=1)


async def test_unscoped_other_serving_is_unchanged(scope_module: Any) -> None:
    engine = NativeEngine()
    engine.allow_add_reply.set()
    engine.allow_output.set()
    controller = scope_module.AsrScopeController(engine)
    assert await _consumer(engine, "other-native-endpoint") == ["synthetic transcript"]
    assert engine.aborted == []
    assert controller.pending_scopes == 0


@pytest.mark.parametrize("cancel_path", ["request-finish", "serving-abort"])
async def test_failed_cleanup_stays_bounded_and_unconfirmed(
    scope_module: Any, cancel_path: str
) -> None:
    engine = NativeEngine()
    engine.fail_abort = True
    engine.allow_add_reply.set()
    engine.allow_output.set()
    controller = scope_module.AsrScopeController(engine, max_scopes=1)
    scope = controller.open()
    token = scope_module.CURRENT_SCOPE.set(scope)
    await _consumer(controller.engine, "original")
    if cancel_path == "serving-abort":
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await controller.engine.abort("original")
    scope_module.CURRENT_SCOPE.reset(token)
    controller.seal(scope)
    assert not await controller.wait_closed(wait_timeout_seconds=1)
    assert controller.pending_scopes == 1
    with pytest.raises(scope_module.ScopeCapacityError):
        controller.open()


async def test_request_fanout_is_bounded_before_extra_send(scope_module: Any) -> None:
    engine = NativeEngine()
    engine.allow_add_reply.set()
    engine.allow_output.set()
    controller = scope_module.AsrScopeController(engine, max_internal_requests=1)
    scope = controller.open()
    token = scope_module.CURRENT_SCOPE.set(scope)
    try:
        await _consumer(controller.engine, "first")
        with pytest.raises(scope_module.ScopeCapacityError):
            await _consumer(controller.engine, "second")
    finally:
        scope_module.CURRENT_SCOPE.reset(token)
        controller.seal(scope)
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert engine.submitted == ["first-internal"]


async def test_asgi_disconnect_after_yield_seals_native_scope(
    scope_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from starlette.responses import StreamingResponse

    monkeypatch.syspath_prepend(str(PLUGIN_ROOT))
    middleware = importlib.import_module("speechnexus_vllm_asr.middleware")
    engine = NativeEngine()
    engine.allow_add_reply.set()
    engine.allow_output.set()
    controller = scope_module.AsrScopeController(engine)
    app = FastAPI()
    app.state.speechnexus_asr_scopes = controller
    app.add_middleware(middleware.AsrScopeMiddleware, root_app=app)

    @app.post("/v1/audio/transcriptions")
    async def transcribe() -> StreamingResponse:
        return StreamingResponse(controller.engine.generate({}, None, "original"))

    async def receive() -> Message:
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/audio/transcriptions",
                "raw_path": b"/v1/audio/transcriptions",
                "query_string": b"",
                "headers": [],
                "server": ("127.0.0.1", 8000),
                "client": ("127.0.0.1", 1234),
            },
            receive,
            send,
        )
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert "original-internal" in engine.aborted
    assert engine.collectors[0].closed


async def test_asgi_capacity_rejects_before_native_handler(
    scope_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from fastapi import FastAPI

    monkeypatch.syspath_prepend(str(PLUGIN_ROOT))
    middleware = importlib.import_module("speechnexus_vllm_asr.middleware")
    controller = scope_module.AsrScopeController(NativeEngine(), max_scopes=1)
    occupied = controller.open()
    invoked = []
    app = FastAPI()
    app.state.speechnexus_asr_scopes = controller

    def vendor_error(message: str, *, status_code: int) -> Any:
        invoked.append(status_code)
        return SimpleNamespace(
            model_dump_json=lambda: json.dumps({"error": {"message": message, "code": status_code}})
        )

    app.state.openai_serving_transcription = SimpleNamespace(create_error_response=vendor_error)
    app.add_middleware(middleware.AsrScopeMiddleware, root_app=app)

    @app.post("/v1/audio/transcriptions")
    async def transcribe() -> dict[str, str]:
        raise AssertionError("native handler must not execute at occupied cleanup capacity")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post("/v1/audio/transcriptions", content=b"synthetic")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == 503
        assert invoked == [503]
        assert (await client.get("/health")).json() == {"status": "ok"}
    controller.seal(occupied)
    assert await controller.wait_closed(wait_timeout_seconds=1)


@pytest.mark.parametrize("root_path", ["", "/private"])
async def test_asgi_normal_response_preserves_native_text(
    scope_module: Any, monkeypatch: pytest.MonkeyPatch, root_path: str
) -> None:
    import httpx
    from fastapi import FastAPI

    monkeypatch.syspath_prepend(str(PLUGIN_ROOT))
    middleware = importlib.import_module("speechnexus_vllm_asr.middleware")
    engine = NativeEngine()
    engine.allow_add_reply.set()
    engine.allow_output.set()
    controller = scope_module.AsrScopeController(engine)
    app = FastAPI()
    app.root_path = root_path
    app.state.speechnexus_asr_scopes = controller
    app.add_middleware(middleware.AsrScopeMiddleware, root_app=app)

    @app.post("/v1/audio/transcriptions")
    async def transcribe() -> dict[str, str]:
        chunks = await _consumer(controller.engine, "original")
        return {"text": "".join(chunks)}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.post(root_path + "/v1/audio/transcriptions")).json() == {
            "text": "synthetic transcript"
        }
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert engine.aborted == ["original-internal"]


@pytest.mark.parametrize("cancel_path", ["request-finish", "repeated-serving-abort"])
async def test_native_fanout_cannot_add_late_sibling(scope_module: Any, cancel_path: str) -> None:
    class FanoutEngine(NativeEngine):
        async def add_request(self, request_id: str, prompt: Any, params: Any) -> Collector:
            queue = Collector("parent-internal")
            self.collectors.append(queue)
            for child in ["first-internal", "late-second-internal"]:
                await self._add_request(
                    SimpleNamespace(request_id=child, external_req_id=request_id),
                    None,
                    None,
                    0,
                    queue,
                )
            return queue

    engine = FanoutEngine()
    frontend_cleared: list[str] = []

    def clear_frontend(identities: list[str], *, internal: bool) -> list[str]:
        assert internal
        frontend_cleared.extend(identities)
        return []

    engine.output_processor.abort_requests = clear_frontend
    controller = scope_module.AsrScopeController(engine)
    scope = controller.open()
    token = scope_module.CURRENT_SCOPE.set(scope)
    task = asyncio.create_task(_consumer(controller.engine, "fanout"))
    scope_module.CURRENT_SCOPE.reset(token)
    await asyncio.wait_for(engine.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    aborts = []
    if cancel_path == "repeated-serving-abort":
        token = scope_module.CURRENT_SCOPE.set(scope)
        aborts = [
            asyncio.create_task(controller.engine.abort("fanout")),
            asyncio.create_task(controller.engine.abort("fanout")),
        ]
        scope_module.CURRENT_SCOPE.reset(token)
        # Let each cancellation enter its wait before acknowledging the native add.
        await asyncio.sleep(0)
    else:
        controller.seal(scope)
    engine.allow_add_reply.set()
    if aborts:
        await asyncio.gather(*aborts)
        controller.seal(scope)
    assert await controller.wait_closed(wait_timeout_seconds=1)
    assert engine.submitted == ["first-internal"]
    assert set(engine.aborted) == {"first-internal"}
    assert engine.collectors[0].closed
    assert "parent-internal" in frontend_cleared


async def test_failed_core_abort_still_closes_collector_after_pending_add(
    scope_module: Any,
) -> None:
    engine = NativeEngine()
    engine.fail_abort = True
    controller = scope_module.AsrScopeController(engine)
    scope = controller.open()
    token = scope_module.CURRENT_SCOPE.set(scope)
    task = asyncio.create_task(_consumer(controller.engine, "original"))
    scope_module.CURRENT_SCOPE.reset(token)
    await asyncio.wait_for(engine.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    controller.seal(scope)
    engine.allow_add_reply.set()
    assert not await controller.wait_closed(wait_timeout_seconds=1)
    assert engine.collectors[0].closed
    assert controller.pending_scopes == 1


async def test_controller_rejects_foreign_scope_and_duplicate_install(scope_module: Any) -> None:
    engine = NativeEngine()
    first = scope_module.AsrScopeController(engine)
    with pytest.raises(ValueError, match="already installed"):
        scope_module.AsrScopeController(engine)
    second = scope_module.AsrScopeController(NativeEngine())
    scope = first.open()
    with pytest.raises(ValueError, match="another engine"):
        second.seal(scope)
    token = scope_module.CURRENT_SCOPE.set(scope)
    try:
        with pytest.raises(ValueError, match="another engine"):
            second.current_scope()
    finally:
        scope_module.CURRENT_SCOPE.reset(token)
    first.seal(scope)
    assert await first.wait_closed(wait_timeout_seconds=1)


@pytest.mark.parametrize("difference", ["none", "version", "commit", "source", "module_file"])
def test_runtime_pin_is_checked_before_hooks(
    scope_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    difference: str,
) -> None:
    plugin = importlib.import_module("speechnexus_vllm_asr.plugin")
    source = tmp_path / "synthetic.py"
    source.write_text("synthetic native source")
    pin = {
        "version": "0.28.0",
        "commit": "synthetic-pinned",
        "source_sha256": {"synthetic.py": hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    native_type = object()
    modules = {
        "vllm": SimpleNamespace(
            __file__=None if difference == "module_file" else str(tmp_path / "__init__.py")
        ),
        "vllm._version": SimpleNamespace(
            __version__="changed" if difference == "version" else "0.28.0",
            __commit_id__="changed" if difference == "commit" else "synthetic-pinned",
        ),
        "vllm.v1.engine.async_llm": SimpleNamespace(AsyncLLM=native_type),
    }
    if difference == "source":
        source.write_text("changed native source")
    monkeypatch.setattr(
        plugin,
        "files",
        lambda package: SimpleNamespace(
            joinpath=lambda path: SimpleNamespace(read_text=lambda: json.dumps(pin))
        ),
    )
    monkeypatch.setattr(plugin.importlib, "import_module", lambda name: modules[name])
    if difference == "none":
        assert plugin.verify_runtime() is native_type
    else:
        with pytest.raises(RuntimeError, match="ASR cleanup"):
            plugin.verify_runtime()


async def test_plugin_installs_only_on_native_private_asr(
    scope_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = importlib.import_module("speechnexus_vllm_asr.plugin")
    monkeypatch.setattr(plugin, "verify_runtime", lambda: NativeEngine)
    engine = NativeEngine()
    engine.log_requests = False
    state = SimpleNamespace(openai_serving_transcription=SimpleNamespace(engine_client=engine))
    await plugin.AsrScopePlugin().init_state(engine, state, SimpleNamespace())
    assert state.openai_serving_transcription.engine_client is state.speechnexus_asr_scopes.engine
    invalid = NativeEngine()
    invalid.log_requests = True
    with pytest.raises(RuntimeError, match="private logging"):
        await plugin.AsrScopePlugin().init_state(
            invalid,
            SimpleNamespace(openai_serving_transcription=SimpleNamespace(engine_client=invalid)),
            SimpleNamespace(),
        )


class SharedExternalEngine(NativeEngine):
    """Native external lookup expands every internal request with that external ID."""

    def __init__(self) -> None:
        super().__init__()
        self.external_ids: dict[str, set[str]] = {}
        self.internal_external: dict[str, str] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.accepted: asyncio.Queue[str] = asyncio.Queue()
        self.output_processor.abort_requests = self._expand_and_remove
        self.sequence = 0

    def _expand_and_remove(self, identities: list[str], *, internal: bool) -> list[str]:
        expanded: set[str] = set()
        for identity in identities:
            if internal:
                if identity in self.internal_external:
                    expanded.add(identity)
            else:
                expanded.update(self.external_ids.get(identity, set()))
        for identity in expanded:
            external = self.internal_external.pop(identity)
            self.external_ids[external].remove(identity)
            if not self.external_ids[external]:
                del self.external_ids[external]
        return sorted(expanded)

    async def add_request(self, request_id: str, prompt: Any, params: Any) -> Collector:
        self.sequence += 1
        queue = Collector(f"{request_id}-random-{self.sequence}")
        self.collectors.append(queue)
        await self._add_request(
            SimpleNamespace(request_id=queue.request_id, external_req_id=request_id),
            None,
            None,
            0,
            queue,
        )
        return queue

    async def _add_request(
        self, request: Any, prompt: Any, parent: Any, index: int, queue: Collector
    ) -> None:
        identity = request.request_id
        self.external_ids.setdefault(request.external_req_id, set()).add(identity)
        self.internal_external[identity] = request.external_req_id
        self.submitted.append(identity)
        self.gates[identity] = asyncio.Event()
        self.accepted.put_nowait(identity)
        await self.gates[identity].wait()
        self.add_returned = True

    async def abort(self, request_ids: Any, internal: bool = False) -> None:
        ids = [request_ids] if isinstance(request_ids, str) else list(request_ids)
        await self._core_abort(self._expand_and_remove(ids, internal=internal))


async def _native_serving_cancel_path(
    engine: Any, request_id: str, entered: asyncio.Event, *, abort_as_list: bool = False
) -> Any:
    try:
        return await _consumer(engine, request_id)
    except asyncio.CancelledError:
        # SpeechToTextBaseServing aborts external IDs in its cancellation path.
        entered.set()
        await asyncio.gather(
            engine.abort([request_id] if abort_as_list else request_id), return_exceptions=True
        )
        raise


@pytest.mark.parametrize("abort_as_list", [False, True])
async def test_native_serving_duplicate_external_id_cancels_only_its_scope(
    scope_module: Any, abort_as_list: bool
) -> None:
    engine = SharedExternalEngine()
    controller = scope_module.AsrScopeController(engine)
    scopes = [controller.open(), controller.open()]
    serving_abort = asyncio.Event()
    tasks = []
    owned_ids = []
    for scope in scopes:
        token = scope_module.CURRENT_SCOPE.set(scope)
        tasks.append(
            asyncio.create_task(
                _native_serving_cancel_path(
                    controller.engine,
                    "same-X-Request-Id",
                    serving_abort,
                    abort_as_list=abort_as_list,
                )
            )
        )
        scope_module.CURRENT_SCOPE.reset(token)
        owned_ids.append(await asyncio.wait_for(engine.accepted.get(), 1))
    try:
        assert owned_ids[0] != owned_ids[1]
        tasks[0].cancel()
        await asyncio.wait_for(serving_abort.wait(), 1)
        # The first add is still unresolved. Neither original nor other-scope
        # requests may be cancelled before this scope's add has finished.
        assert engine.aborted == []
        engine.gates[owned_ids[0]].set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tasks[0], 1)
        assert set(engine.aborted) == {owned_ids[0]}
        assert not tasks[1].done()
        assert not engine.gates[owned_ids[1]].is_set()
        assert engine.external_ids == {"same-X-Request-Id": {owned_ids[1]}}
        token = scope_module.CURRENT_SCOPE.set(scopes[0])
        try:
            # Repeated external cancellation and a guessed foreign internal ID
            # remain confined to this request, even after its frontend map is gone.
            await controller.engine.abort("same-X-Request-Id")
            await controller.engine.abort([owned_ids[0]], internal=True)
            await controller.engine.abort(owned_ids[1], internal=True)
        finally:
            scope_module.CURRENT_SCOPE.reset(token)
        assert set(engine.aborted) == {owned_ids[0]}
        assert engine.external_ids == {"same-X-Request-Id": {owned_ids[1]}}
        # The other request independently completes using the unchanged ID.
        engine.gates[owned_ids[1]].set()
        engine.allow_output.set()
        assert await asyncio.wait_for(tasks[1], 1) == ["synthetic transcript"]
    finally:
        for gate in engine.gates.values():
            gate.set()
        engine.allow_output.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        for scope in scopes:
            controller.seal(scope)
        await asyncio.gather(*tasks, return_exceptions=True)
        assert await controller.wait_closed(wait_timeout_seconds=1)


async def test_facade_abort_outside_asr_scope_preserves_native_behavior(scope_module: Any) -> None:
    engine = SharedExternalEngine()
    controller = scope_module.AsrScopeController(engine)
    tasks = [
        asyncio.create_task(_consumer(controller.engine, "same-unscoped-ID")) for _ in range(2)
    ]
    ids = [await asyncio.wait_for(engine.accepted.get(), 1) for _ in range(2)]
    await controller.engine.abort("same-unscoped-ID")
    assert set(engine.aborted) == set(ids)
    for gate in engine.gates.values():
        gate.set()
    engine.allow_output.set()
    await asyncio.gather(*tasks)
    assert controller.pending_scopes == 0
