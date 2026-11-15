"""Deployment-owned startup gate for the independent workbench API container.

`workbench-api` in `deploy/compose.yaml` runs this module as its entrypoint. It
verifies the deployment inputs the service cannot silently do without — the
runtime DSN, the separate cleanup DSN the deletion ledger replay needs, the
deployment identity, the cursor key and the schema — then runs the *production*
lifespan preflight (`check_database`, `check_cleanup_identity`) and only then
`exec`s the ASGI factory. Every line is either `OK: <what is now true>` or
`FAIL: <the named prerequisite>`; a failure exits 1 and never starts the server.

Why a gate instead of relying on the ASGI lifespan alone: the lifespan refusal is
correct but arrives as a traceback from a server that already bound its socket in
some deployments, and Compose reports a container that prints nothing as fine.
The gate is the same judgments in an ordering an operator can read, and it is
what makes "the workbench shares the API's wide identity" a startup failure
instead of a convention (`check_database` refuses any grant outside the workbench
schema, so a shared `sn_api` cannot pass).

The privilege and migration surface is not restated here: the settings class
validates the environment (including the `SPEECHNEXUS_WORKBENCH_*` namespace), and
the two checks come from `speechnexus_backend.workbench.runtime`, so this file
cannot drift from what the server actually enforces. `main()` takes its `env`,
`load`, `check` and `exec` collaborators as parameters, which is how
`tests/unit/backend/test_workbench_startup_gate.py` proves the wiring, the exit
code and the "no `exec` after a failure" ordering without PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TextIO

from speechnexus_backend.workbench.migrate import role_of
from speechnexus_backend.workbench.runtime import (
    check_cleanup_identity,
    check_database,
)
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

DSN_ENV = "SPEECHNEXUS_WORKBENCH_DATABASE_URL"
CLEANUP_DSN_ENV = "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL"
SCHEMA_ENV = "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA"
WORKSPACE_ENV = "SPEECHNEXUS_WORKBENCH_WORKSPACE_ID"
DEPLOYMENT_ENV = "SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID"
CURSOR_KEY_ENV = "SPEECHNEXUS_WORKBENCH_CURSOR_KEY"
DEFAULT_SCHEMA = "workbench"
# The documented factory entry; keep it identical to deploy/README.md's command.
API_COMMAND = (
    "python",
    "-m",
    "uvicorn",
    "speechnexus_backend.workbench.runtime:create_app_from_env",
    "--factory",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
)


def findings(env: Mapping[str, str]) -> list[str]:
    """Name every blank input before the settings class turns it into a traceback."""
    problems: list[str] = []
    for key, why in (
        (DSN_ENV, "the serving identity has no database role"),
        (
            CLEANUP_DSN_ENV,
            "the deletion routes and the ledger replay stay unavailable without a "
            "separate cleanup identity, so an accepted deletion could not settle",
        ),
        (WORKSPACE_ENV, "the default workspace of this deployment is unknown"),
        (DEPLOYMENT_ENV, "the deployment identity that scopes cursors is unknown"),
        (CURSOR_KEY_ENV, "list cursors cannot be signed without a key"),
    ):
        if not env.get(key, "").strip():
            problems.append(f"{key} is blank: {why}")
    schema = env.get(SCHEMA_ENV, DEFAULT_SCHEMA).strip() or DEFAULT_SCHEMA
    if schema == "speech":
        problems.append(
            f"{SCHEMA_ENV} is `speech`: the workbench owns its own namespace and "
            "never shares the speech product schema"
        )
    serving, erasing = (
        role_of(env.get(DSN_ENV, "").strip()),
        role_of(env.get(CLEANUP_DSN_ENV, "").strip()),
    )
    if serving is not None and serving == erasing:
        problems.append(
            f"{DSN_ENV} and {CLEANUP_DSN_ENV} name the same role {serving}: the "
            "serving identity must never hold the erasing authority the cleanup "
            "role carries"
        )
    return problems


def load(env: Mapping[str, str]) -> WorkbenchSettings:
    """Build the production settings from this container's environment."""
    # WorkbenchSettings reads the process environment; the mapping is only the
    # reporting surface, so an inconsistent pair fails in one place.
    return WorkbenchSettings()


async def _checks(settings: WorkbenchSettings) -> None:
    await check_database(settings)
    await check_cleanup_identity(settings)


def main(
    env: Mapping[str, str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    load_settings: Callable[[Mapping[str, str]], Any] = load,
    check: Callable[[Any], Awaitable[None]] = _checks,
    exec_command: Callable[[str, Sequence[str]], Any] = os.execvp,
) -> int:
    """Gate the API start; return an exit code only when the server must not run."""
    environment = os.environ if env is None else env
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err
    problems = findings(environment)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=stderr)
        return 1
    try:
        settings = load_settings(environment)
    except Exception as error:
        # The settings class already names the offending key; keep the type so the
        # message cannot echo a DSN or a secret value.
        print(
            f"FAIL: the workbench configuration was refused at load "
            f"({type(error).__name__}); fix the named SPEECHNEXUS_WORKBENCH_ keys",
            file=stderr,
        )
        return 1
    try:
        asyncio.run(check(settings))
    except Exception as error:
        # RuntimeStartupError carries the relation, privilege and repair it refused,
        # never the DSN. Any other failure is reported by type only: a driver message
        # can contain a connection string, an object name or SQL.
        if error.__class__.__name__ == "RuntimeStartupError":
            print(
                f"FAIL: the workbench runtime database was refused at startup: {error}",
                file=stderr,
            )
        else:
            print(
                "FAIL: the workbench runtime database check failed "
                f"({type(error).__name__}); check the two workbench DSNs, the roles "
                "they name and that PostgreSQL is reachable",
                file=stderr,
            )
        return 1
    print(
        "OK: runtime and cleanup identities hold exactly their declared surface",
        file=stdout,
    )
    print("OK: workbench schema is at the revision this build expects", file=stdout)
    print("OK: starting the workbench ASGI factory", file=stdout)
    stdout.flush()
    stderr.flush()
    # `exec` replaces the process, so the OK lines are flushed first (the image
    # sets PYTHONUNBUFFERED, but the gate must not depend on that). `execvp` takes
    # the program and its argv separately; the argv repeats the program name.
    exec_command(API_COMMAND[0], list(API_COMMAND))
    return 0  # pragma: no cover - execvp never returns on success


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    raise SystemExit(main())
