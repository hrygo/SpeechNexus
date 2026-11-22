"""Every bounded ``_Cursor`` must enforce its transaction deadline before querying.

Eleven copied implementations of one pattern live under ``backend/src``. Each one
must (a) refuse to touch the database once its deadline has passed and (b) install
a server-side ``statement_timeout`` bounded by the remaining time. Most of those
copies had no test that could tell the guard was there -- deleting one still left
the functional suite green, and deleting ``set_config`` makes a real transaction
wait instead of fail. A handful were already pinned indirectly
(``engine_registration``'s guard and ``set_config``, and ``voice_capability_evidence``'s
guard, through their own tests), which is exactly why the copies that were not are
worth pinning on every implementation rather than one at a time.

Two further ``_Cursor`` declarations are ``typing.Protocol`` stubs, not bounded
implementations; they are listed separately so the discovery guard still counts
all thirteen declarations in the backend.

The discovery guard matches the literal name ``_Cursor``. The same pattern also
appears under other names (``_DeadlineCursor`` is declared five times, plus
``_DeadlineConnection``); those have their own budget tests and are deliberately
out of this file's scope, so this guard must not be read as "every deadline
budget in the backend is covered".
"""

from __future__ import annotations

import importlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3] / "backend"
SOURCE_ROOT = BACKEND_ROOT / "src"
CURSOR_DECLARATION = re.compile(r"^\s*class _Cursor\b", re.MULTILINE)

QUERY = "SELECT payload FROM example WHERE identity = ?"
PARAMETERS = ("row-a",)
SET_TIMEOUT = "SELECT set_config('statement_timeout', ?, true)"


@dataclass(frozen=True, slots=True)
class Implementation:
    """One bounded cursor copy and where its refusal error is defined."""

    module: str
    error: str
    # ``workspaces_postgres._Cursor`` exposes ``rows()``; the other copies expose ``execute()``.
    invoke: str


IMPLEMENTATIONS = (
    Implementation("speechnexus_backend.engine_registration", "RegistrationError", "execute"),
    Implementation(
        "speechnexus_backend.speech.infrastructure.publications_postgres",
        "PublicationUnavailableError",
        "execute",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres",
        "VoiceEvidenceUnavailableError",
        "execute",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.profile_drafts_postgres",
        "ProfileUnavailableError",
        "execute",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.pronunciation_drafts_postgres",
        "PronunciationUnavailableError",
        "execute",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.binding_publication_evidence_postgres",
        "PublicationUnavailableError",
        "execute",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.voice_publications_postgres",
        "VoicePublicationUnavailableError",
        "execute",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.workspaces_postgres",
        "WorkspaceUnavailableError",
        "rows",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.binding_admin_reader_postgres",
        "BindingUnavailableError",
        "execute",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.binding_drafts_postgres",
        "BindingUnavailableError",
        "execute",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres",
        "BindingLifecycleUnavailableError",
        "execute",
    ),
)

# Declarations that are ``typing.Protocol`` stubs: no deadline state, no guard.
PROTOCOL_MODULES = (
    "speechnexus_backend.speech.infrastructure.public_job_admission",
    "speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres",
)


class RecordingCursor:
    """Minimal cursor double: records calls, never reaches PostgreSQL."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 1
        self.description: object | None = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> RecordingCursor:
        self.calls.append((query, parameters))
        return self

    def fetchall(self) -> list[dict[str, object]]:
        return []


def cursor_id(entry: Implementation) -> str:
    return entry.module.rsplit(".", 1)[-1]


def module(entry: Implementation) -> Any:
    return importlib.import_module(entry.module)


def error_type(entry: Implementation) -> Any:
    return getattr(module(entry), entry.error)


def build(entry: Implementation, deadline: float) -> tuple[Any, RecordingCursor]:
    cursor_class = module(entry)._Cursor
    recorder = RecordingCursor()
    return cursor_class(cast(Any, recorder), deadline), recorder


def invoke(entry: Implementation, cursor: Any) -> object:
    return getattr(cursor, entry.invoke)(QUERY, PARAMETERS)


def discover_cursor_modules() -> set[str]:
    """Dotted module paths of every ``class _Cursor`` declaration in backend/src."""

    discovered: set[str] = set()
    for path in (SOURCE_ROOT / "speechnexus_backend").rglob("*.py"):
        if not CURSOR_DECLARATION.search(path.read_text(encoding="utf-8")):
            continue
        parts = list(path.relative_to(SOURCE_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        discovered.add(".".join(parts))
    return discovered


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_expired_deadline_refuses_before_any_query(entry: Implementation) -> None:
    expected = error_type(entry)
    now = 100.0
    cursor, recorder = build(entry, deadline=now - 1)
    raised: BaseException | None = None

    with mock.patch("time.monotonic", return_value=now):
        try:
            invoke(entry, cursor)
        except BaseException as error:
            raised = error

    assert recorder.calls == [], (
        f"{entry.module} sent work to the database after its deadline: {recorder.calls!r}"
    )
    assert type(raised) is expected, (
        f"{entry.module} accepted an expired deadline instead of raising {entry.error}"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_live_deadline_sets_statement_timeout_before_the_query(entry: Implementation) -> None:
    deadline, now = 100.0, 97.5
    cursor, recorder = build(entry, deadline=deadline)
    assert deadline - now == 2.5

    with mock.patch("time.monotonic", return_value=now):
        invoke(entry, cursor)

    milliseconds = str(max(1, int(2.5 * 1000)))
    assert milliseconds == "2500"
    assert recorder.calls == [(SET_TIMEOUT, (milliseconds,)), (QUERY, PARAMETERS)], (
        f"{entry.module} ran its query without the server-side statement timeout first: "
        f"{recorder.calls!r}"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_zero_remaining_deadline_is_already_expired(entry: Implementation) -> None:
    expected = error_type(entry)
    deadline = now = 100.0
    cursor, recorder = build(entry, deadline=deadline)
    assert deadline - now == 0.0

    with (
        mock.patch("time.monotonic", return_value=now),
        pytest.raises(expected),
    ):
        invoke(entry, cursor)

    assert recorder.calls == [], (
        f"{entry.module} let an exactly-expired deadline reach the database: {recorder.calls!r}"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_sub_millisecond_remaining_deadline_never_sends_zero_milliseconds(
    entry: Implementation,
) -> None:
    deadline = 100.0
    now = deadline - 1e-7
    remaining = deadline - now
    assert 0 < remaining < 0.001
    expected_milliseconds = str(max(1, int(remaining * 1000)))
    assert expected_milliseconds == "1"

    cursor, recorder = build(entry, deadline=deadline)
    with mock.patch("time.monotonic", return_value=now):
        invoke(entry, cursor)

    assert recorder.calls == [
        (SET_TIMEOUT, (expected_milliseconds,)),
        (QUERY, PARAMETERS),
    ], f"{entry.module} did not floor a sub-millisecond budget at 1ms: {recorder.calls!r}"


def test_table_covers_every_cursor_declaration_in_the_backend() -> None:
    discovered = discover_cursor_modules()
    tabled = {entry.module for entry in IMPLEMENTATIONS} | set(PROTOCOL_MODULES)

    assert len(IMPLEMENTATIONS) == 11
    assert len(PROTOCOL_MODULES) == 2
    assert discovered == tabled, (
        "A backend _Cursor declaration is not classified here.\n"
        f"untested: {sorted(discovered - tabled)}\n"
        f"stale: {sorted(tabled - discovered)}"
    )


@pytest.mark.parametrize("module_name", PROTOCOL_MODULES, ids=lambda name: name.rsplit(".", 1)[-1])
def test_protocol_declarations_are_not_bounded_implementations(module_name: str) -> None:
    cursor_class = importlib.import_module(module_name)._Cursor
    protocol_marker = "_is_protocol"
    assert getattr(cursor_class, protocol_marker, False), (
        f"{module_name}._Cursor became a concrete implementation; "
        "add it to IMPLEMENTATIONS and test its deadline guard"
    )


def test_savepoint_control_is_not_preceded_by_the_timeout_probe() -> None:
    """事务已被唯一冲突中止时,探针自己也会失败,回滚就永远走不到.

    保存点回滚是中止事务里唯一允许的语句;先发 `set_config('statement_timeout', …)`
    会得到 `current transaction is aborted`,于是 `insert_if_absent` 抛出的不是
    「已存在」而是可用性错误。
    """

    raw = mock.Mock()
    raw.execute.return_value = raw
    cursor = _profile_cursor(raw)
    for statement in (
        "SAVEPOINT portable_sql_insert_if_absent",
        "ROLLBACK TO SAVEPOINT portable_sql_insert_if_absent",
        "RELEASE SAVEPOINT portable_sql_insert_if_absent",
    ):
        raw.reset_mock()
        cursor.execute(statement)
        assert raw.execute.call_args_list == [mock.call(statement, ())]


def _profile_cursor(raw: Any) -> Any:
    module = importlib.import_module(
        "speechnexus_backend.speech.infrastructure.profile_drafts_postgres"
    )
    return module._Cursor(raw, time.monotonic() + 5)


PROBE = "SELECT set_config('statement_timeout', ?, true)"
PER_STATEMENT = re.compile(r"def (?:execute|rows|changed)\(self, [a-z_]+: str")
SAVEPOINT_SKIP = "is_savepoint_control("


def test_every_per_statement_probe_lets_savepoint_control_through() -> None:
    """先探测再执行的适配器必须放行保存点控制语句.

    事务被唯一冲突中止之后,唯一还能执行的是 `ROLLBACK TO SAVEPOINT`;探测语句抢在它前面
    会让回滚永远走不到,调用方看到的是可用性错误而不是「别人先写」。这条守护的是**一致性**:
    新增一个逐语句装 `statement_timeout` 的适配器时,必须一并放行保存点控制。
    """

    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        # 协议声明(`def execute(...) -> _Rows: ...`)不是实现,不装探针也不执行语句。
        implemented = any(
            PER_STATEMENT.search(line) and not line.rstrip().endswith(": ...")
            for line in text.splitlines()
        )
        if PROBE not in text or not implemented:
            continue
        if SAVEPOINT_SKIP not in text:
            offenders.append(str(path.relative_to(SOURCE_ROOT)))
    assert offenders == [], offenders
