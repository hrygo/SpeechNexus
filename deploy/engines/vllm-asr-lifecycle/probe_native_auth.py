"""Verify native auth inside the pinned image; no model, GPU or network required."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any


async def check_case(
    middleware: Any, path: str, token: str | None, expected_status: int, body_reads: int
) -> None:
    reads: list[int] = []
    reached: list[int] = []
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        reads.append(1)
        return {"type": "http.request", "body": b"synthetic bounded audio", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        reached.append(1)
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    headers = [] if token is None else [(b"authorization", ("Bearer " + token).encode())]
    scope = {"type": "http", "method": "POST", "path": path, "root_path": "", "headers": headers}
    await middleware(inner, tokens=["synthetic-generation-2"])(scope, receive, send)
    assert messages[0]["status"] == expected_status
    assert len(reads) == len(reached) == body_reads


def main() -> None:
    import vllm
    from vllm.entrypoints.serve.utils.server_utils import AuthenticationMiddleware

    root = Path(__file__).resolve().parent
    native = json.loads((root / "native-auth-pin.json").read_text())
    runtime = json.loads((root / native["runtime_pin"]).read_text())
    package = Path(vllm.__file__).resolve().parent
    digest = hashlib.sha256((package / native["middleware_source"]).read_bytes()).hexdigest()
    assert digest == native["middleware_sha256"]
    api_server = "entrypoints/openai/api_server.py"
    assert (
        hashlib.sha256((package / api_server).read_bytes()).hexdigest()
        == runtime["source_sha256"][api_server]
    )

    async def verify() -> None:
        await check_case(AuthenticationMiddleware, "/v1/audio/transcriptions", None, 401, 0)
        await check_case(
            AuthenticationMiddleware, "/v1/audio/transcriptions", "synthetic-generation-1", 401, 0
        )
        await check_case(
            AuthenticationMiddleware, "/v1/audio/transcriptions", "synthetic-generation-2", 200, 1
        )
        # This limitation is part of the evidence: /health does not prove auth.
        await check_case(AuthenticationMiddleware, "/health", None, 200, 1)

    asyncio.run(verify())
    print(
        json.dumps(
            {
                "cases_passed": 4,
                "unauthorized_body_reads": 0,
                "health_is_unauthenticated": True,
                "middleware_sha256": digest,
                "model_loaded": False,
                "stop_evidence": False,
                "qualification": False,
            }
        )
    )


if __name__ == "__main__":
    main()
