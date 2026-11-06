"""Voice drafts use the existing public Job submission transaction."""

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.public_jobs import PublicJob, identifier


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
        return view

    def update_metadata(
        self, draft_id: str, command: Mapping[str, Any], idempotency_key: str | None
    ) -> dict[str, Any]:
        # HTTP validation is generated; guard the narrow application write port too.
        if set(command) != {"expected_metadata_revision", "display_name"}:
            raise ValueError("Only draft metadata may be updated")
        return self._store.update_metadata(identifier(draft_id), command, idempotency_key)
