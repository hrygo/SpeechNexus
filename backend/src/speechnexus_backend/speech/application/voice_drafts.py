"""Voice drafts use the existing public Job submission transaction."""

import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from speechnexus_backend.speech.application.public_jobs import (
    PublicJobs,
    VoiceDraftStaleRevisionError,
    VoiceInputInvalidError,
)
from speechnexus_backend.speech.domain.public_jobs import (
    PublicJob,
    PublicJobNotFoundError,
    PublicJobUnavailableError,
    identifier,
)

# The persisted input is the UTF-8 JSON document the revision echoes. The HTTP
# ingress bound is the same size so a valid request cannot be serialized past
# the database CHECK by ASCII escaping; both sides use this helper and flag.
MAX_STORED_VOICE_INPUT_BYTES = 65536
# The shared LanguageTag shape the stored revision echo must satisfy. The request
# contract is looser (any string <=35), so the write path canonicalises to this
# public shape instead of rejecting a value the request declared valid.
_VOICE_LANGUAGE = re.compile(r"^(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)$")


def canonical_voice_language(value: str) -> str:
    """Lowercase only the primary subtag; fall back to BCP 47 ``und``.

    ``EN-US`` becomes ``en-US`` (still the same language), while a value such as
    ``english`` or ``x!`` cannot be represented by the response LanguageTag and
    becomes ``und`` (undetermined). The original request stays in the Job intent.
    """
    primary, separator, rest = value.partition("-")
    candidate = primary.lower() + separator + rest
    return candidate if _VOICE_LANGUAGE.fullmatch(candidate) else "und"


def voice_input_shape(payload_input: Mapping[str, Any], kind: str) -> dict[str, Any]:
    shape = {**payload_input, "kind": kind}
    language = shape.get("language")
    if isinstance(language, str):
        shape["language"] = canonical_voice_language(language)
    return shape


def voice_input_document(payload_input: Mapping[str, Any], kind: str) -> str:
    """One canonical stored/checked shape for a kind-tagged voice input.

    ``ensure_ascii=False`` keeps UTF-8 text at its wire size instead of
    inflating one CJK or non-BMP character into six or twelve ASCII bytes.
    """
    return json.dumps(voice_input_shape(payload_input, kind), ensure_ascii=False)


def voice_input_fits(document: str) -> bool:
    return 2 <= len(document.encode("utf-8")) <= MAX_STORED_VOICE_INPUT_BYTES


class VoiceDraftConflictError(RuntimeError):
    """Metadata CAS, publication guard or idempotency conflict."""


class VoiceDraftNotFoundError(LookupError):
    pass


class VoiceDraftStore(Protocol):
    def get(self, draft_id: str, revision: str | None = None) -> dict[str, Any] | None: ...

    def draft_exists(self, draft_id: str) -> bool: ...

    def revision_target(self, draft_id: str, revision: str) -> dict[str, Any] | None: ...

    def revision(self, draft_id: str, revision: str) -> dict[str, Any] | None: ...

    def update_metadata(
        self,
        draft_id: str,
        command: Mapping[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]: ...


def draft_id_for_job(job_id: str) -> str:
    return "draft-" + identifier(job_id)


def require_candidate_facts(view: dict[str, Any], jobs: PublicJobs) -> None:
    """Reject a candidate that has no committed receipt or ready preview.

    The GET projection uses this helper; the metadata transaction applies the
    same ``resolve_voice_previews`` rule before it writes, so a PATCH cannot
    expose a candidate that GET would report as unavailable.
    """
    draft_revision = view.get("revision")
    if isinstance(draft_revision, dict) and draft_revision.get("status") == "candidate":
        try:
            job = jobs.get(draft_revision["generation_job_id"])
            previews = jobs.artifacts(job)
        except PublicJobNotFoundError:
            raise PublicJobUnavailableError from None
        draft_revision["preview_artifact_ids"] = [fact.artifact_id for fact in previews]


def _revision_input(payload: Mapping[str, Any], kind: str) -> None:
    request_input = payload.get("input")
    if not isinstance(request_input, dict):
        raise VoiceInputInvalidError
    if kind == "design":
        if not isinstance(request_input.get("description"), str) or not isinstance(
            request_input.get("preview_text"), str
        ):
            raise VoiceInputInvalidError
    else:
        if (
            not isinstance(request_input.get("reference_audio_asset_id"), str)
            or request_input.get("source_use_confirmed") is not True
        ):
            raise VoiceInputInvalidError
    language = request_input.get("language")
    if language is not None and (
        not isinstance(language, str) or not language.strip() or len(language) > 35
    ):
        raise VoiceInputInvalidError
    if not voice_input_fits(voice_input_document(request_input, kind)):
        raise VoiceInputInvalidError


class VoiceDrafts:
    def __init__(self, jobs: PublicJobs, store: VoiceDraftStore) -> None:
        self._jobs, self._store = jobs, store

    def create(
        self,
        kind: Literal["voice_design", "voice_clone"],
        payload: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], PublicJob]:
        job = self._jobs.submit(
            {**payload, "kind": kind}, request_id=request_id, idempotency_key=idempotency_key
        )
        view = self.get(draft_id_for_job(job.job_id))
        return view["draft"], job

    def generate(
        self,
        draft_id: str,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], PublicJob]:
        draft_id = identifier(draft_id)
        expected = payload["expected_draft_revision"]
        target = self._store.revision_target(draft_id, expected)
        if target is None:
            if not self._store.draft_exists(draft_id):
                raise VoiceDraftNotFoundError
            raise VoiceDraftStaleRevisionError
        if target["status"] in ("published", "preview_only"):
            raise VoiceDraftConflictError
        kind = target["kind"]
        if kind not in ("design", "clone"):
            raise PublicJobUnavailableError
        _revision_input(payload, kind)
        job, revision = self._jobs.submit_revision(
            {
                **payload,
                "kind": "voice_design" if kind == "design" else "voice_clone",
                "workspace_id": target["workspace_id"],
                "draft_target": {"id": draft_id, "expected_revision": expected},
            },
            draft_id=draft_id,
            expected_revision=expected,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        revision_view = self._store.revision(draft_id, revision)
        if revision_view is None:
            raise PublicJobUnavailableError
        # The draft is the current head; a replayed revision may be older than
        # it, and the revision object remains the original immutable one.
        return {"draft": self.get(draft_id)["draft"], "revision": revision_view}, job

    def validate(
        self,
        draft_id: str,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> PublicJob:
        draft_id = identifier(draft_id)
        expected = payload["expected_draft_revision"]
        target = self._store.revision_target(draft_id, expected)
        if target is None:
            if not self._store.draft_exists(draft_id):
                raise VoiceDraftNotFoundError
            raise VoiceDraftStaleRevisionError
        if target["status"] in ("published", "preview_only"):
            raise VoiceDraftConflictError
        body: dict[str, Any] = {
            "kind": "voice_validation",
            "workspace_id": target["workspace_id"],
            "input": {
                "voice_draft_id": draft_id,
                "draft_revision": expected,
                "validation_set_revision": payload["validation_set_revision"],
            },
            "model_binding": target["binding"],
        }
        if "model" in payload:
            body["model"] = payload["model"]
        return self._jobs.submit(body, request_id=request_id, idempotency_key=idempotency_key)

    def preview(
        self,
        draft_id: str,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> PublicJob:
        """Submit one preview Job fixed to the target revision's binding.

        A same-key replay found by the lock-free early lookup returns the
        original Job even after the head advanced. If that lookup loses a race
        with a concurrent commit, ``PublicJobs.submit`` retries the key after
        the head/candidate checks fail; by then a store may have taken and
        released its locks, so the retry is best-effort rather than lock-free,
        but a replay still wins over reporting a conflict.
        """
        draft_id = identifier(draft_id)
        expected = payload["expected_draft_revision"]
        text = payload.get("text")
        target = self._store.revision_target(draft_id, expected)
        if target is None:
            if not self._store.draft_exists(draft_id):
                raise VoiceDraftNotFoundError
            raise VoiceInputInvalidError
        if target["kind"] not in ("design", "clone"):
            raise VoiceInputInvalidError
        if not isinstance(text, str) or not 1 <= len(text) <= 2000:
            raise VoiceInputInvalidError
        body: dict[str, Any] = {
            "kind": "voice_preview",
            "workspace_id": target["workspace_id"],
            "input": {
                "voice_draft_id": draft_id,
                "draft_revision": expected,
                "text": text,
            },
            "model_binding": target["binding"],
        }
        if "model" in payload:
            body["model"] = payload["model"]
        return self._jobs.submit(body, request_id=request_id, idempotency_key=idempotency_key)

    def get(self, draft_id: str, revision: str | None = None) -> dict[str, Any]:
        view = self._store.get(identifier(draft_id), revision)
        if view is None:
            raise VoiceDraftNotFoundError
        # A candidate claims both a committed receipt and ready preview
        # Artifacts. Reuse the public Job read path instead of trusting the
        # stored result JSON alone, so a missing receipt, wrong ownership or a
        # non-ready artifact cannot surface as a schema-valid candidate.
        require_candidate_facts(view, self._jobs)
        return view

    def update_metadata(
        self, draft_id: str, command: Mapping[str, Any], idempotency_key: str | None
    ) -> dict[str, Any]:
        # HTTP validation is generated; guard the narrow application write port too.
        if set(command) != {"expected_metadata_revision", "display_name"}:
            raise ValueError("Only draft metadata may be updated")
        draft = self._store.update_metadata(identifier(draft_id), command, idempotency_key)
        # The store validates candidate facts while its transaction is open and
        # returns the draft projection; no post-commit rejection can leave a
        # metadata write behind a 503.
        if not isinstance(draft, dict):
            raise PublicJobUnavailableError
        return draft
