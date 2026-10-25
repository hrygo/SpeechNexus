"""Opaque keyset positions bind deployment/list/parent without claiming identity."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from speechnexus_backend.workbench.application.listings import ListingPosition
from speechnexus_backend.workbench.interfaces.cursors import CursorCodec, InvalidCursorError

KEY = b"synthetic-cursor-key-32-bytes-only"
POSITION = ListingPosition(
    "projects", "workspace-one", datetime(2026, 10, 24, 0, 0, 0, 123456, tzinfo=UTC), "project-one"
)


def test_cursor_exact_microseconds_is_stable_and_survives_same_config_restart() -> None:
    one = CursorCodec("deployment-one", KEY)
    token = one.encode(POSITION)
    assert len(token) <= 512 and "=" not in token and "/" not in token and "+" not in token
    assert (
        CursorCodec("deployment-one", KEY).decode(token, kind="projects", parent_id="workspace-one")
        == POSITION
    )
    assert one.encode(POSITION) == token and "synthetic-cursor-key" not in repr(one)
    long = replace(POSITION, parent_id="w" * 128, item_id="i" * 128)
    assert len(one.encode(long)) <= 512


@pytest.mark.parametrize("field,value", [("kind", "transcripts"), ("parent_id", "workspace-two")])
def test_cursor_cannot_cross_scope(field: str, value: str) -> None:
    c = CursorCodec("deployment-one", KEY)
    scope: dict[str, Any] = {"kind": "projects", "parent_id": "workspace-one", field: value}
    with pytest.raises(InvalidCursorError, match=r"^invalid pagination cursor$"):
        c.decode(c.encode(POSITION), **scope)


@pytest.mark.parametrize("deployment,key", [("deployment-two", KEY), ("deployment-one", b"x" * 32)])
def test_cursor_cannot_cross_deployment_or_key(deployment: str, key: bytes) -> None:
    token = CursorCodec("deployment-one", KEY).encode(POSITION)
    with pytest.raises(InvalidCursorError):
        CursorCodec(deployment, key).decode(token, kind="projects", parent_id="workspace-one")


@pytest.mark.parametrize(
    "token", ["", "a", "abc", "a" * 513, "private-synthetic", "@@@@", "😀", "AA=="]
)
def test_cursor_rejects_malformed_without_input_leak(token: str) -> None:
    with pytest.raises(InvalidCursorError, match=r"^invalid pagination cursor$"):
        CursorCodec("deployment-one", KEY).decode(token, kind="projects", parent_id="workspace-one")


def test_tampering_and_alternate_padding_are_rejected() -> None:
    c = CursorCodec("deployment-one", KEY)
    token = c.encode(POSITION)
    for bad in [
        token + "=",
        token[:-1],
        ("A" if token[0] != "A" else "B") + token[1:],
        token[:10] + "Z" + token[11:],
    ]:
        with pytest.raises(InvalidCursorError):
            c.decode(bad, kind="projects", parent_id="workspace-one")


def test_dst_fold_and_pre_epoch_positions_preserve_utc_instants() -> None:
    c = CursorCodec("deployment-one", KEY)
    for date in [
        datetime(1960, 1, 1, tzinfo=UTC),
        datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1),
    ]:
        p = replace(POSITION, created_at=date)
        actual = c.decode(c.encode(p), kind=p.kind, parent_id=p.parent_id)
        assert actual.created_at == date.astimezone(UTC)


@pytest.mark.parametrize(
    "deployment,key", [("", KEY), ("d", b"x" * 31), ("d", "private-synthetic"), ("d", None)]
)
def test_invalid_config_has_no_secret_in_error(deployment: str, key: Any) -> None:
    with pytest.raises(ValueError, match=r"^invalid cursor configuration$"):
        CursorCodec(deployment, key)
