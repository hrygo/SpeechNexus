"""The same-version identity a browser harness reports for its own build (#853).

The production web tier routes `GET /v1/nexus/version` to the speech base. The browser
harnesses here run a real workbench API composition root but no speech base, so
forwarding that path to the workbench app answers 404 and the bundle's runtime check
adds its own `role="alert"` banner to a page whose assertions expect exactly one alert.

Answering the path from the same body function the base answers with — over the same
generated contract digest, which the bundle baked in at build time — keeps the harness
the single-revision deployment it models: the check runs, sees an equal digest, and
stays quiet.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from speechnexus_backend import __version__
from speechnexus_backend.speech.interfaces.http.version_identity import version_identity_body


def version_identity_response() -> JSONResponse:
    """The identity response a single-revision deployment gives a bundle's check."""
    return JSONResponse(version_identity_body(__version__), headers={"Cache-Control": "no-store"})
