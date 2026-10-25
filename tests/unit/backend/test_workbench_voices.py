"""Published voice catalogue boundaries with no database, network or engine."""

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.application.voices import (
    VoiceCatalogueUnavailableError,
    WorkbenchVoices,
)
from speechnexus_backend.workbench.domain.voices import (
    VoiceCataloguePage,
    VoiceRevision,
    VoiceSummary,
    validate_catalogue_scope,
    validate_voice_reference,
)

NOW = datetime(2026, 10, 25, 9, 30, tzinfo=UTC)
WORKSPACE = "workspace-synthetic"
VOICE = "voice-synthetic-one"
BINDING = "binding-synthetic-one"


def summary(**changes: object) -> VoiceSummary:
    values: dict[str, object] = {
        "voice_id": VOICE,
        "revision": "3",
        "display_name": "合成旁白",
        "source_type": "designed",
        "status": "published",
        "languages": ("zh-CN",),
        "audio_formats": ("wav",),
    }
    values.update(changes)
    return VoiceSummary(**cast(Any, values))


def revision_value(**changes: object) -> VoiceRevision:
    values: dict[str, object] = {
        "voice_id": VOICE,
        "revision": "3",
        "display_name": "合成旁白",
        "status": "published",
        "languages": ("zh-CN", "en"),
        "audio_formats": ("wav", "flac"),
        "model_binding_id": BINDING,
        "model_binding_revision": "2",
        "published_at": NOW,
    }
    values.update(changes)
    return VoiceRevision(**cast(Any, values))


class Catalogue:
    """Port double that records what the workbench actually asked the base for."""

    def __init__(self, *, page: VoiceCataloguePage | None = None) -> None:
        self.page = page or VoiceCataloguePage(items=(summary(),), has_more=False, next_cursor=None)
        self.calls: list[tuple[object, ...]] = []
        self.failure: Exception | None = None

    async def list_summaries(
        self, *, workspace_id: str, language: str | None, limit: int, after: str | None
    ) -> VoiceCataloguePage:
        self.calls.append(("list", workspace_id, language, limit, after))
        if self.failure is not None:
            raise self.failure
        return self.page

    async def get_revision(self, *, voice_id: str, revision: str) -> VoiceRevision:
        self.calls.append(("get", voice_id, revision))
        if self.failure is not None:
            raise self.failure
        return revision_value()


@pytest.mark.parametrize(
    "changes",
    [
        {"voice_id": "Bad"},
        {"voice_id": ""},
        {"revision": "0"},
        {"revision": "03"},
        {"revision": "1" * 20},
        {"revision": 3},
        {"revision": ""},
        {"display_name": ""},
        {"display_name": "x" * 129},
        {"display_name": "  "},
        {"source_type": "recorded"},
        {"source_type": "DESIGNED"},
        {"status": "draft"},
        {"languages": ()},
        {"languages": ("zh-CN", "zh-CN")},
        {"languages": ["zh-CN"]},
        {"languages": ("zh_CN",)},
        {"languages": ("x" * 36,)},
        {"audio_formats": ()},
        {"audio_formats": ("wav", "wav")},
        {"audio_formats": ("wav!",)},
    ],
)
def test_a_summary_rejects_anything_the_base_cannot_have_published(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        summary(**changes)


def test_a_summary_carries_the_bases_values_verbatim() -> None:
    value = summary(status="retired", languages=("zh-CN", "en"))
    assert (value.voice_id, value.revision, value.status) == (VOICE, "3", "retired")
    assert value.languages == ("zh-CN", "en")


@pytest.mark.parametrize(
    "changes",
    [
        {"model_binding_id": "Bad"},
        {"model_binding_revision": "0"},
        {"published_at": datetime(2026, 10, 25, 9, 30)},
        {"status": "retired"},
        {"status": "published", "retired_at": NOW},
        {"status": "disabled"},
        {"status": "disabled", "disabled_reason": "stopped"},
        {"status": "published", "disabled_reason": "stopped"},
        {"status": "retired", "retired_at": NOW, "disabled_reason": "stopped"},
        {"disabled_reason": ""},
        {"status": "disabled", "retired_at": NOW, "disabled_reason": "停" * 2001},
    ],
)
def test_a_revision_rejects_an_inconsistent_lifecycle(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        revision_value(**changes)


def test_a_revision_accepts_retirement_and_disablement_with_their_evidence() -> None:
    retired = revision_value(status="retired", retired_at=NOW)
    disabled = revision_value(
        status="disabled", retired_at=NOW, disabled_reason="引擎不再提供该绑定"
    )
    assert retired.retired_at == NOW and retired.disabled_reason is None
    assert disabled.disabled_reason == "引擎不再提供该绑定" and disabled.retired_at == NOW


@pytest.mark.parametrize(
    "changes",
    [
        {"items": [summary()]},
        {"items": (object(),)},
        {"has_more": 1},
        {"has_more": True, "next_cursor": None},
        {"next_cursor": "x" * 513},
        {"next_cursor": ""},
    ],
)
def test_a_page_rejects_incoherent_paging(changes: dict[str, object]) -> None:
    values: dict[str, object] = {"items": (), "has_more": False, "next_cursor": None}
    values.update(changes)
    with pytest.raises(ValueError):
        VoiceCataloguePage(**cast(Any, values))


@pytest.mark.parametrize(
    "scope",
    [
        {"workspace_id": "Bad", "language": None, "limit": 50, "after": None},
        {"workspace_id": WORKSPACE, "language": "zh_CN", "limit": 50, "after": None},
        {"workspace_id": WORKSPACE, "language": "x" * 36, "limit": 50, "after": None},
        {"workspace_id": WORKSPACE, "language": None, "limit": 0, "after": None},
        {"workspace_id": WORKSPACE, "language": None, "limit": 101, "after": None},
        {"workspace_id": WORKSPACE, "language": None, "limit": True, "after": None},
        {"workspace_id": WORKSPACE, "language": None, "limit": 50, "after": "x" * 513},
        {"workspace_id": WORKSPACE, "language": None, "limit": 50, "after": ""},
    ],
)
def test_a_catalogue_query_outside_the_frozen_scope_is_refused(scope: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_catalogue_scope(**cast(Any, scope))


@pytest.mark.parametrize(
    "reference",
    [
        {"voice_id": "Bad", "revision": 1},
        {"voice_id": VOICE, "revision": "0"},
        {"voice_id": VOICE, "revision": "1" * 20},
        {"voice_id": VOICE, "revision": 1},
    ],
)
def test_a_pinned_voice_reference_is_validated(reference: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_voice_reference(**cast(Any, reference))


async def test_listing_forwards_the_bases_cursor_and_language_once() -> None:
    catalogue = Catalogue()
    service = WorkbenchVoices(catalogue)
    page = await service.list_voices(
        workspace_id=WORKSPACE, language="zh-CN", limit=10, after="base-cursor"
    )
    assert catalogue.calls == [("list", WORKSPACE, "zh-CN", 10, "base-cursor")]
    assert page.items[0].voice_id == VOICE and page.has_more is False


async def test_a_refused_query_never_reaches_the_base() -> None:
    catalogue = Catalogue()
    service = WorkbenchVoices(catalogue)
    with pytest.raises(ValueError):
        await service.list_voices(workspace_id=WORKSPACE, limit=0)
    with pytest.raises(ValueError):
        await service.get_revision(voice_id="Bad", revision="1")
    assert catalogue.calls == []


async def test_a_pinned_revision_is_read_from_the_base() -> None:
    catalogue = Catalogue()
    service = WorkbenchVoices(catalogue)
    value = await service.get_revision(voice_id=VOICE, revision="7")
    assert catalogue.calls == [("get", VOICE, "7")]
    assert isinstance(value, VoiceRevision)


async def test_a_missing_or_unreachable_catalogue_is_reported_as_such() -> None:
    catalogue = Catalogue()
    service = WorkbenchVoices(catalogue)
    catalogue.failure = ResourceNotFoundError()
    with pytest.raises(ResourceNotFoundError):
        await service.get_revision(voice_id=VOICE, revision="1")
    catalogue.failure = VoiceCatalogueUnavailableError()
    with pytest.raises(VoiceCatalogueUnavailableError):
        await service.list_voices(workspace_id=WORKSPACE)
