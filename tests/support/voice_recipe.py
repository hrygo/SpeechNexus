"""The voice-creation write path plan A installs, without the #1080 refusal.

``PublicJobs.submit``/``submit_revision`` refuse every Job kind no queue can claim
(#1080), which is exactly the ``voice_design``/``voice_clone``/``voice_preview``
kinds the draft, dispatch, publication and evidence suites are built on. Those
suites cover two things that stay real: the draft read model, and the writer plan A
will wire to the service. Importing this fixture makes it autouse for the importing
module only, so a suite opts in with one line and the refusal itself is pinned by
its own cases at every layer:

* ``tests/unit/backend/test_voice_creation_jobs.py`` -- service boundary, and that
  the store's write ports are never called;
* ``tests/unit/backend/test_undispatchable_voice_jobs_http.py`` -- both HTTP
  surfaces, plus the still-accepted ``tts`` control;
* ``tests/integration/test_undispatchable_voice_jobs_pg.py`` -- real PostgreSQL,
  row counts before and after, and a claim on the supported lane.

A module that installs this fixture must not also claim the refusal works; that
claim lives in the modules above.
"""

from __future__ import annotations

import pytest

REFUSAL_SCOPE = "speechnexus_backend.speech.application.public_jobs.UNDISPATCHABLE_JOB_KINDS"


@pytest.fixture(autouse=True)
def plan_a_write_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty the refusal's scope so the draft and dispatch suites still write."""
    monkeypatch.setattr(REFUSAL_SCOPE, ())
