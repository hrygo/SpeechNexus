"""Composed startup refuses a profile writer whose publication authority is absent.

The property under test is the one the card asked for: a writer-enabled API that
cannot read its reviewed evidence stops at startup naming the variable, instead of
starting and answering 503 ``profile_result_unknown`` from inside the writer.
"""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from speechnexus_backend.app import create_app
from speechnexus_backend.profile_runtime import ProfileRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.profile_publication_evidence import (
    EVIDENCE_FILE_VARIABLE,
    EVIDENCE_FORMAT,
    DeploymentFilePublicationVerifier,
)
from tests.unit.backend.test_health import _settings

WRITER = SecretStr("postgresql://sn_profile_writer:synthetic@127.0.0.1/unused")


def _settings_for(**changes: object) -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "asset_allowed_hosts": ("testserver",),
            **changes,
        }
    )


def _evidence(tmp_path: Path, *, entries: list[dict[str, str]] | None = None) -> Path:
    path = tmp_path / "profile-publication-evidence.json"
    path.write_text(
        json.dumps({"format": EVIDENCE_FORMAT, "entries": entries or []}) + "\n",
        encoding="utf-8",
    )
    return path


def test_writer_without_evidence_is_refused_at_construction() -> None:
    settings = _settings_for(profile_writer_database_url=WRITER)
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        ProfileRuntime(settings)
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        create_app(settings=settings)


def test_evidence_path_that_cannot_be_read_is_named_at_construction(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    settings = _settings_for(
        profile_writer_database_url=WRITER,
        profile_publication_evidence_file=str(missing),
    )
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        ProfileRuntime(settings)
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        create_app(settings=settings)


def test_invalid_evidence_document_is_named_at_construction(tmp_path: Path) -> None:
    path = tmp_path / "profile-publication-evidence.json"
    path.write_text('{"format": "wrong/1", "entries": []}', encoding="utf-8")
    settings = _settings_for(
        profile_writer_database_url=WRITER,
        profile_publication_evidence_file=str(path),
    )
    with pytest.raises(ValueError, match=EVIDENCE_FILE_VARIABLE):
        create_app(settings=settings)


def test_evidence_without_a_writer_is_refused_naming_both_keys(tmp_path: Path) -> None:
    settings = _settings_for(profile_publication_evidence_file=str(_evidence(tmp_path)))
    with pytest.raises(ValueError, match=rf"{EVIDENCE_FILE_VARIABLE}.*PROFILE_WRITER"):
        ProfileRuntime(settings)
    with pytest.raises(ValueError, match=rf"{EVIDENCE_FILE_VARIABLE}.*PROFILE_WRITER"):
        create_app(settings=settings)


def test_reviewed_evidence_is_the_installed_authority(tmp_path: Path) -> None:
    path = _evidence(tmp_path)
    settings = _settings_for(
        profile_writer_database_url=WRITER,
        profile_publication_evidence_file=str(path),
    )
    runtime = ProfileRuntime(settings)
    # Construction reads and validates the document; it opens no pool and owns
    # no provider until lifespan startup.
    assert isinstance(runtime._verifier, DeploymentFilePublicationVerifier)
    assert runtime.provider is None and runtime._pool is None
    app = create_app(settings=settings)
    assert app.state.profile_runtime is not None
    assert isinstance(app.state.profile_runtime._verifier, DeploymentFilePublicationVerifier)


def test_non_api_role_keeps_its_explicit_identity_refusal(tmp_path: Path) -> None:
    settings = _settings_for(
        profile_writer_database_url=WRITER,
        profile_publication_evidence_file=str(_evidence(tmp_path)),
        process_role="scheduler",
    )
    with pytest.raises(ValueError, match="explicit API profile writer identity"):
        ProfileRuntime(settings)
