"""Own native frontend cleanup, never assert engine termination or resource release.

This module runs inside the pinned engine image (Python 3.12), not the gateway.
The engine's original serving, preprocessing and generation methods remain in use.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


class ScopeCapacityError(RuntimeError):
    """The local bounded cleanup registry cannot accept more native work."""


@dataclass(frozen=True)
class _NativeIdentity:
    external_id: str
    parent_id: str


@dataclass(eq=False)
class _Scope:
    owner: AsrScopeController
    sealed: bool = False
    requests: dict[str, _NativeIdentity] = field(default_factory=dict)
    collectors: dict[int, Any] = field(default_factory=dict)
    adds: set[asyncio.Task[Any]] = field(default_factory=set)
    advancing: dict[asyncio.Task[Any], int] = field(default_factory=dict)
    generators: dict[int, Any] = field(default_factory=dict)
    cleanup: asyncio.Task[None] | None = None
    cleanup_failed: bool = False


CURRENT_SCOPE: ContextVar[_Scope | None] = ContextVar("speechnexus_asr_scope", default=None)


class _ScopedEngine:
    def __init__(self, controller: AsrScopeController) -> None:
        self._controller = controller

    def __getattr__(self, name: str) -> Any:
        return getattr(self._controller.native_engine, name)

    async def abort(self, request_id: Any, internal: bool = False) -> None:
        controller = self._controller
        scope = controller.current_scope()
        if scope is None:
            await controller.native_engine.abort(request_id, internal=internal)
            return
        await controller.abort_owned(scope, request_id, internal=internal)

    async def generate(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        controller = self._controller
        scope = controller.current_scope()
        if scope is not None and scope.sealed:
            raise asyncio.CancelledError
        native = controller.native_engine.generate(*args, **kwargs)
        if scope is None:
            try:
                async for output in native:
                    yield output
            finally:
                await native.aclose()
            return

        scope.generators[id(native)] = native
        try:
            while True:
                # Track only an actual advance. The task receiving a final yield
                # may already be serializing the HTTP response; never cancel it.
                task = asyncio.current_task()
                assert task is not None
                scope.advancing[task] = scope.advancing.get(task, 0) + 1
                try:
                    output = await anext(native)
                except StopAsyncIteration:
                    break
                finally:
                    remaining = scope.advancing[task] - 1
                    if remaining:
                        scope.advancing[task] = remaining
                    else:
                        del scope.advancing[task]
                yield output
        finally:
            try:
                await native.aclose()
            except BaseException:
                scope.cleanup_failed = True
                raise
            finally:
                scope.generators.pop(id(native), None)


class AsrScopeController:
    """Bound engine-local handles to one ASGI request, including detached native adds.

    Local cleanup completion is *not* a GPU fence, stop fact or budget release.
    A failed or hanging cleanup keeps its registry slot until process replacement.
    There is no replay, persistence, scheduler, or independently writable authority.
    """

    def __init__(
        self,
        native_engine: Any,
        *,
        max_scopes: int = 256,
        max_internal_requests: int = 4096,
    ) -> None:
        if (
            type(max_scopes) is not int
            or max_scopes < 1
            or type(max_internal_requests) is not int
            or max_internal_requests < 1
        ):
            raise ValueError("ASR cleanup limits must be positive integers")
        if getattr(native_engine, "_speechnexus_asr_scope_owner", None) is not None:
            raise ValueError("ASR cleanup is already installed on this native engine")
        self.native_engine = native_engine
        self.engine = _ScopedEngine(self)
        self._max_scopes = max_scopes
        self._max_internal_requests = max_internal_requests
        self._scopes: set[_Scope] = set()
        self._original_add = native_engine.add_request
        self._original_internal_add = native_engine._add_request
        self._clear_frontend = native_engine.output_processor.abort_requests
        self._abort_core = native_engine.engine_core.abort_requests_async
        native_engine.add_request = self._add_request
        native_engine._add_request = self._add_internal_request
        native_engine._speechnexus_asr_scope_owner = self

    @property
    def pending_scopes(self) -> int:
        return len(self._scopes)

    def current_scope(self) -> _Scope | None:
        scope = CURRENT_SCOPE.get()
        if scope is not None and scope.owner is not self:
            raise ValueError("ASR cleanup scope belongs to another engine")
        return scope

    def open(self) -> _Scope:
        if len(self._scopes) >= self._max_scopes:
            raise ScopeCapacityError("ASR request cleanup capacity is occupied")
        scope = _Scope(self)
        self._scopes.add(scope)
        return scope

    def seal(self, scope: _Scope) -> None:
        if scope.owner is not self:
            raise ValueError("ASR cleanup scope belongs to another engine")
        scope.sealed = True
        if scope.cleanup is None:
            scope.cleanup = asyncio.create_task(self._cleanup(scope))

    async def _add_request(self, *args: Any, **kwargs: Any) -> Any:
        scope = self.current_scope()
        if scope is None:
            return await self._original_add(*args, **kwargs)
        if scope.sealed:
            raise asyncio.CancelledError
        # Shield the entire native add, not just its IPC call: tokenization and
        # n>1 fanout can also await. The ASGI owner can cancel promptly while this
        # task retains ownership until all possible sends have resolved.
        task = asyncio.create_task(self._original_add(*args, **kwargs))
        scope.adds.add(task)
        # Keep even completed tasks until cleanup consumes their outcome. A
        # frontend cancellation can race a task completing with an exception.
        return await asyncio.shield(task)

    async def _add_internal_request(
        self,
        request: Any,
        prompt: Any,
        parent_req: Any,
        index: int,
        queue: Any,
    ) -> None:
        scope = self.current_scope()
        if scope is None:
            await self._original_internal_add(request, prompt, parent_req, index, queue)
            return
        # Keep the collector even for a rejected late child. Native n>1 children
        # share it; close it only after the enclosing add and all sends finish.
        scope.collectors[id(queue)] = queue
        if scope.sealed:
            raise asyncio.CancelledError
        if len(scope.requests) >= self._max_internal_requests:
            raise ScopeCapacityError("ASR native request fanout limit exceeded")
        internal_id = request.request_id
        external_id = request.external_req_id
        parent_id = queue.request_id
        if (
            not all(
                isinstance(value, str) and value for value in (internal_id, external_id, parent_id)
            )
            or internal_id in scope.requests
        ):
            raise ValueError("ASR native request identity is invalid or duplicated")
        # This precedes OutputProcessor registration and EngineCore IPC. Even an
        # ambiguous exception from the original add retains the ID for abort.
        scope.requests[internal_id] = _NativeIdentity(external_id, parent_id)
        await self._original_internal_add(request, prompt, parent_req, index, queue)

    async def _wait_adds(self, scope: _Scope) -> None:
        if scope.adds:
            # A serving abort may itself be cancelled. Its observation must not
            # cancel accepted native submissions or another cleanup observer.
            await asyncio.shield(asyncio.gather(*tuple(scope.adds), return_exceptions=True))

    async def abort_owned(self, scope: _Scope, request_id: Any, *, internal: bool) -> None:
        """Resolve native external/parent IDs exclusively in this request's ownership."""
        if scope.owner is not self:
            raise ValueError("ASR cleanup scope belongs to another engine")
        requested = {request_id} if isinstance(request_id, str) else set(request_id)
        # No later child may start while serving cancellation waits for its own
        # accepted adds. Other scopes' submissions are never included in this wait.
        scope.sealed = True
        await self._wait_adds(scope)
        selected = {
            identity
            for identity, association in scope.requests.items()
            if (
                identity in requested or association.parent_id in requested
                if internal
                else association.external_id in requested
            )
        }
        if selected and not await self._cancel_owned_ids(scope, selected):
            raise RuntimeError("ASR scoped abort cleanup is unconfirmed")

    async def _cancel_owned_ids(self, scope: _Scope, selected: set[str]) -> bool:
        families: dict[str, set[str]] = {}
        for identity, association in scope.requests.items():
            families.setdefault(association.parent_id, set()).add(identity)
        # Clearing a parent also clears its children. Include it only if all its
        # submitted children are selected; never expand the shared external map.
        frontend_ids = selected | {
            parent for parent, children in families.items() if children <= selected
        }
        complete = True
        try:
            self._clear_frontend(sorted(frontend_ids), internal=True)
        except BaseException:
            scope.cleanup_failed = True
            complete = False
        try:
            # Always send the recorded internal IDs, even if native abort already
            # removed the frontend map before losing its IPC acknowledgement.
            await self._abort_core(sorted(selected))
        except BaseException:
            scope.cleanup_failed = True
            complete = False
        return complete

    async def _cleanup(self, scope: _Scope) -> None:
        try:
            # A native merge iterator can still own child consumers after the HTTP
            # handler is gone. Stop advances, then resolve every shielded add.
            consumers = tuple(scope.advancing)
            for task in consumers:
                task.cancel()
            if consumers:
                await asyncio.gather(*consumers, return_exceptions=True)
            await self._wait_adds(scope)
            scope.adds.clear()
            # These can be suspended after a final yield (e.g. language detection).
            # Their owner tasks need not still be advancing or even alive.
            for native in tuple(scope.generators.values()):
                try:
                    await native.aclose()
                except BaseException:
                    scope.cleanup_failed = True
            if scope.requests:
                await self._cancel_owned_ids(scope, set(scope.requests))
            for collector in scope.collectors.values():
                try:
                    collector.close()
                except BaseException:
                    scope.cleanup_failed = True
        except BaseException:
            # Never emit vendor exceptions, IDs, prompts or model paths. There is
            # no automatic retry and no release claim on failed/partial cleanup.
            scope.cleanup_failed = True
            return
        if not scope.cleanup_failed:
            scope.generators.clear()
            scope.collectors.clear()
            scope.requests.clear()
            self._scopes.discard(scope)

    async def wait_closed(self, *, wait_timeout_seconds: float) -> bool:
        """Bounded local cleanup observation; timeout does not cancel owned native work."""
        cleanups = [scope.cleanup for scope in self._scopes if scope.cleanup is not None]
        try:
            async with asyncio.timeout(wait_timeout_seconds):
                if cleanups:
                    await asyncio.shield(asyncio.gather(*cleanups))
        except TimeoutError:
            return False
        return not self._scopes
