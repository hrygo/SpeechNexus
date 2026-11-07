"""Voice drafts use the existing public Job submission transaction."""

import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from speechnexus_backend.speech.application.public_jobs import (
    PublicJobs,
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

    One helper serves both the GET projection and the metadata write reply so a
    PATCH can never expose a candidate that GET would report as unavailable.
    """
    draft_revision = view.get("revision")
    if isinstance(draft_revision, dict) and draft_revision.get("status") == "candidate":
        try:
            job = jobs.get(draft_revision["generation_job_id"])
            previews = jobs.artifacts(job)
        except PublicJobNotFoundError:
            raise PublicJobUnavailableError from None
        draft_revision["preview_artifact_ids"] = [fact.artifact_id for fact in previews]


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
        view = self._store.update_metadata(identifier(draft_id), command, idempotency_key)
        # The PATCH reply is a draft projection too; apply the same candidate
        # invariant before exposing it.
        require_candidate_facts(view, self._jobs)
        draft = view.get("draft")
        if not isinstance(draft, dict):
            raise PublicJobUnavailableError
        return draft
