"""Run only newly committed file acceptances; crash recovery never reruns work."""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from speechnexus_gateway.application.execution_completion import ExecutionCompletionService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.execution_results import (
    ExecutionAudioStagingResult,
    StagingResult,
)
from speechnexus_gateway.domain.file_execution import (
    FileCommandExecution,
    FileExecutionLedger,
    FileExecutionLimits,
)
from speechnexus_gateway.domain.observation import ExecutionObservation, RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import (
    AppendInterval,
    MeterKind,
    OutputIntervalRecord,
)
from speechnexus_gateway.domain.ports import AbortOutcome, DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.recovery import (
    MarkExecutionUnknown,
    TerminalStopEvidenceUnavailableError,
)
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    EngineObservedState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmitRequest,
)
from speechnexus_gateway.domain.terminal import EngineTerminalResponse


@dataclass(slots=True, repr=False)
class _Preparation:
    request: SubmitRequest
    engine_request_id: str
    prepared_at: datetime
    cancelled: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    task: asyncio.Task[None] | None = None


_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,31}")


class FileExecutionWorker[StoreT]:
    """A process-local executor, backed exclusively by the original submission ledger.

    Dispatch prepares no I/O. The submit service alone calls start after confirming
    a new acceptance commit and releasing ingress. A restarted worker quarantines
    old active records instead of treating them as a retry queue.
    """

    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        ledger: FileExecutionLedger,
        execution: FileCommandExecution[StoreT],
        results: StoreT,
        limits: FileExecutionLimits,
        engine_request_prefix: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if _PREFIX.fullmatch(engine_request_prefix) is None:
            raise ValueError("file work requires a bounded engine request prefix")
        self._ingress = ingress
        self._ledger = ledger
        self._execution = execution
        self._results = results
        self._command_bytes = limits.max_command_bytes
        self._max_pending = limits.max_pending
        self._shutdown_seconds = limits.shutdown_seconds
        self._engine_request_prefix = engine_request_prefix
        self._completion = ExecutionCompletionService(ledger=ledger)
        self._now = now
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._close_requested = threading.Event()
        self._preparations: dict[str, _Preparation] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._accepting = False
        self._opened = False

    @property
    def max_command_bytes(self) -> int:
        return self._command_bytes

    @property
    def running(self) -> bool:
        with self._lock:
            return self._accepting and self._thread is not None and self._thread.is_alive()

    def open(self) -> None:
        # A close cannot overtake startup and release its pool/ownership while
        # startup is still reading or about to launch a delayed loop thread.
        with self._lifecycle_lock:
            self._open()

    def _open(self) -> None:
        with self._lock:
            if self._opened or self._close_requested.is_set():
                raise RuntimeError("a closed file worker lifecycle cannot be reused")
            self._opened = True
        self._ingress.check_ownership()
        while candidates := self._ledger.list_unfinished(
            gateway_id=self._ingress.gateway_id, host_node_id=self._ingress.node_id, limit=500
        ):
            for intent in candidates:
                self._ingress.check_ownership()
                self._quarantine(intent.identity.submit_intent_id, cancelled=False)
        self._thread = threading.Thread(target=self._serve, name="file-execution", daemon=False)
        self._thread.start()
        if not self._ready.wait(self._shutdown_seconds):
            self._close_requested.set()
            raise RuntimeError("file worker did not start")
        with self._lock:
            if self._close_requested.is_set() or not self._thread.is_alive():
                raise RuntimeError("file worker did not start")
            self._accepting = True

    def _serve(self) -> None:
        async def lifetime() -> None:
            with self._lock:
                self._loop = asyncio.get_running_loop()
                self._stop = asyncio.Event()
                if self._close_requested.is_set():
                    return
                self._ready.set()
            await self._stop.wait()

        with asyncio.Runner() as runner:
            runner.run(lifetime())

    def dispatch(self, request: SubmitRequest) -> DispatchOutcome:
        """Short local acceptance preparation; object/media/model calls are forbidden."""
        at = self._now()
        try:
            frozen = self._execution.validate_request(request)
            if (
                frozen.request_document != request.request_document
                or at >= request.identity.deadline_at
            ):
                raise ValueError
        except Exception:
            return DispatchOutcome(DispatchResult.NOT_SENT, at)
        with self._lock:
            if (
                not self._accepting
                or self._thread is None
                or not self._thread.is_alive()
                or len(self._preparations) >= self._max_pending
                or request.identity.submit_intent_id in self._preparations
            ):
                return DispatchOutcome(DispatchResult.NOT_SENT, at)
            ticket = _Preparation(frozen, self._engine_request_prefix + "-" + uuid.uuid4().hex, at)
            self._preparations[request.identity.submit_intent_id] = ticket
        return DispatchOutcome(DispatchResult.ACKNOWLEDGED, at, ticket.engine_request_id)

    def discard(self, submit_intent_id: str) -> None:
        with self._lock:
            ticket = self._preparations.get(submit_intent_id)
            if ticket is not None and not ticket.started:
                del self._preparations[submit_intent_id]

    def start(self, intent: SubmissionIntent) -> None:
        key = intent.identity.submit_intent_id
        with self._lock:
            ticket = self._preparations.get(key)
            if (
                ticket is None
                or ticket.request.identity != intent.identity
                or ticket.request.request_digest != intent.request_digest
                or ticket.engine_request_id != intent.engine_request_id
                or ticket.prepared_at != intent.dispatched_at
                or intent.execution_state is not ExecutionState.EXECUTING
            ):
                raise SubmissionOutcomeUnknownError("no matching original file preparation")
            if ticket.started:
                return
            if not self._accepting or self._loop is None:
                raise SubmissionOutcomeUnknownError("the accepted file worker is unavailable")
            ticket.started = True
            try:
                self._loop.call_soon_threadsafe(self._launch, ticket, intent)
            except RuntimeError:
                del self._preparations[key]
                self._accepting = False
                raise SubmissionOutcomeUnknownError("the accepted file worker stopped") from None

    def _launch(self, ticket: _Preparation, intent: SubmissionIntent) -> None:
        task = asyncio.create_task(self._run(ticket, intent))
        ticket.task = task
        self._tasks.add(task)
        task.add_done_callback(lambda completed: self._finished(ticket, completed))

    def _finished(self, ticket: _Preparation, task: asyncio.Task[None]) -> None:
        # A task cancelled before its first step never enters _run at all.
        # Completion also follows the join of any already-started DB writer.
        key = ticket.request.identity.submit_intent_id
        with self._lock:
            if self._preparations.get(key) is ticket:
                del self._preparations[key]
        self._tasks.discard(task)

    def _check_start(self, intent: SubmissionIntent) -> None:
        # Only the new-commit callback reaches this point. This short read/check
        # revalidates the current authority and releases ingress before any I/O.
        with self._ingress.dispatch(intent.identity):
            self._ledger.check_file_start(intent)

    async def _database[T](self, call: Callable[[], T]) -> T:
        task = asyncio.create_task(asyncio.to_thread(call))
        interrupted = False
        # Cancellation cannot leave a DB writer using closed authority. Always
        # consume its result, including a failure completed during cancellation.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                break
        try:
            result = task.result()
        except Exception:
            if interrupted:
                raise asyncio.CancelledError from None
            raise
        if interrupted:
            raise asyncio.CancelledError
        return result

    async def _run(self, ticket: _Preparation, intent: SubmissionIntent) -> None:
        key = intent.identity.submit_intent_id
        try:
            if ticket.cancelled.is_set():
                raise asyncio.CancelledError
            await self._database(lambda: self._check_start(intent))
            if ticket.cancelled.is_set():
                raise asyncio.CancelledError
            outcome = await self._execution.stage_result(
                ticket.request,
                store=self._results,
                cancelled=ticket.cancelled.is_set,
            )
            await self._database(lambda: self._register(intent, outcome.result, ticket.cancelled))
            await self._complete(intent, outcome.terminal)
        except Exception, asyncio.CancelledError:
            try:
                await self._database(
                    lambda: self._quarantine(key, cancelled=ticket.cancelled.is_set())
                )
            except Exception, asyncio.CancelledError:
                # Unknown cleanup blocks further acceptance; no provider detail
                # or private command/content is logged or reported as success.
                with self._lock:
                    self._accepting = False

    async def _complete(
        self, accepted: SubmissionIntent, terminal: EngineTerminalResponse | None
    ) -> None:
        """Record the success transition only with the adapter's terminal fact."""
        try:
            await self._database(lambda: self._completion.complete(accepted, terminal))
        except TerminalStopEvidenceUnavailableError:
            # The private result exists, but nothing trusted proved this execution
            # stopped. Keep the capacity and record that specific pending reason.
            await self._database(
                lambda: self._quarantine(
                    accepted.identity.submit_intent_id,
                    cancelled=False,
                    reason_code=ExecutionErrorCode.EXECUTION_CLEANUP_PENDING,
                )
            )

    def _register(
        self, accepted: SubmissionIntent, result: StagingResult, cancelled: threading.Event
    ) -> None:
        key = accepted.identity.submit_intent_id
        for _ in range(3):
            current = self._ledger.get(key)
            if (
                current is None
                or current.identity != accepted.identity
                or current.request_digest != accepted.request_digest
                or current.engine_request_id != accepted.engine_request_id
            ):
                raise SubmissionOutcomeUnknownError("the original file result is unavailable")
            if current.staging_result is not None:
                if current.staging_result != result:
                    raise SubmissionOutcomeUnknownError("the execution has another fixed result")
                return
            if cancelled.is_set():
                raise ExecutionCleanupPendingError("cancelled file result cannot be registered")
            if isinstance(result, ExecutionAudioStagingResult):
                current = self._append_audio_interval(current, result)
            observed = replace(
                ExecutionObservation.from_intent(current),
                observed_at=max(current.updated_at, self._now()),
                staging_reference=result.reference,
            )
            try:
                self._ledger.record_execution_observation(
                    RecordExecutionObservation(observed, current.version, result)
                )
                return
            except StaleSubmissionVersionError:
                continue  # Re-read only; never rerun inference or the object write.
            except SubmissionOutcomeUnknownError:
                recovered = self._ledger.get(key)
                if (
                    recovered is not None
                    and recovered.identity == accepted.identity
                    and recovered.request_digest == accepted.request_digest
                    and recovered.engine_request_id == accepted.engine_request_id
                    and recovered.staging_result == result
                ):
                    return
                raise
        raise SubmissionOutcomeUnknownError("the original result registration needs reconciliation")

    def _append_audio_interval(
        self, current: SubmissionIntent, result: ExecutionAudioStagingResult
    ) -> SubmissionIntent:
        """Record the complete generated range before registering its private receipt."""
        if (
            current.output_stream_id is None
            or current.engine_request_id is None
            or current.identity.sample_rate_hz != result.audio.sample_rate_hz
        ):
            raise SubmissionOutcomeUnknownError("the audio result has no matching output stream")
        return self._ledger.append_interval(
            OutputIntervalRecord(
                AppendInterval(
                    interval_id="generated-" + current.engine_request_id,
                    submit_intent_id=current.identity.submit_intent_id,
                    output_stream_id=current.output_stream_id,
                    sample_rate_hz=result.audio.sample_rate_hz,
                    meter_kind=MeterKind.GENERATED,
                    sample_start=0,
                    sample_end=result.audio.source_pcm_samples,
                ),
                current.identity,
            )
        ).intent

    def _quarantine(
        self, key: str, *, cancelled: bool, reason_code: ExecutionErrorCode | None = None
    ) -> None:
        current = self._ledger.get(key)
        # A stop that committed while the local task was cancelled is an accepted
        # terminal fact: quarantining it would be a reversal and would wedge acceptance.
        if (
            current is not None
            and not current.execution_state.is_terminal
            and current.execution_state.holds_capacity
            and current.execution_state is not ExecutionState.UNKNOWN_QUARANTINED
        ):
            self._ledger.mark_execution_unknown(
                MarkExecutionUnknown.for_intent(
                    current,
                    reason_code=(
                        reason_code
                        if reason_code is not None
                        else (
                            ExecutionErrorCode.CANCELLATION_UNCONFIRMED
                            if cancelled
                            else ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN
                        )
                    ),
                    at=max(current.updated_at, self._now()),
                )
            )

    def observe(self, intent: SubmissionIntent) -> EngineObservation | None:
        with self._lock:
            ticket = self._preparations.get(intent.identity.submit_intent_id)
            if (
                ticket is None
                or not ticket.started
                or ticket.cancelled.is_set()
                or intent.execution_state is not ExecutionState.EXECUTING
                or ticket.request.identity != intent.identity
                or ticket.request.request_digest != intent.request_digest
                or ticket.engine_request_id != intent.engine_request_id
            ):
                return None
            return EngineObservation(
                ticket.engine_request_id,
                EngineObservedState.EXECUTING,
                dispatched_at=ticket.prepared_at,
            )

    def abort(self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int) -> AbortOutcome:
        with self._lock:
            ticket = self._preparations.get(intent.identity.submit_intent_id)
            if ticket is not None and ticket.engine_request_id == intent.engine_request_id:
                ticket.cancelled.set()
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._cancel, ticket)
        return AbortOutcome(self._now(), engine_request_id=intent.engine_request_id)

    @staticmethod
    def _cancel(ticket: _Preparation) -> None:
        if ticket.task is not None:
            ticket.task.cancel()

    async def _shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._stop is not None:
            self._stop.set()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close()

    def _close(self) -> None:
        with self._lock:
            self._close_requested.set()
            self._accepting = False
            for ticket in self._preparations.values():
                ticket.cancelled.set()
            loop, thread, ready = self._loop, self._thread, self._ready.is_set()
        # Native startup can fail before a thread exists. That object cannot be
        # joined, and no background writer needs to retain the unused authority.
        if thread is None or thread.ident is None:
            return
        if thread.is_alive() and loop is not None and ready:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(
                    timeout=self._shutdown_seconds
                )
            except Exception:
                raise RuntimeError("file worker cleanup is not confirmed") from None
        # A delayed startup may not have published its loop yet. It still owns
        # resources until joined; the close flag prevents that late loop serving.
        thread.join(self._shutdown_seconds)
        if thread.is_alive():
            raise RuntimeError("file worker is still using its owned resources")
        with self._lock:
            self._preparations.clear()
