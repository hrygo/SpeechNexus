#!/usr/bin/env python3
"""Start one disposable Unix-socket-only PG cluster, test it, and stop it.

Uses existing PostgreSQL binaries. Never discovers or contacts a running service,
installs software, modifies shared databases, or creates a persistent service.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]


def run(
    command: list[str], *, env: dict[str, str] | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=ROOT, env=env, check=True, capture_output=True, text=True, timeout=timeout
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument(
        "--workbench-runtime",
        action="store_true",
        help="also qualify independent workbench bootstrap/lifespan on private PG",
    )
    parser.add_argument(
        "--admin-workspaces",
        action="store_true",
        help="also require production Admin + actual A Workspace HTTP/SQL reads",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="also require production-bundle Chromium qualification",
    )
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/team-c/c00-pg-probe.json")
    args = parser.parse_args()
    pg_bin: Path = args.pg_bin.resolve()
    output: Path = args.output.resolve()
    for binary in ("initdb", "pg_ctl", "createdb", "postgres"):
        if not (pg_bin / binary).is_file():
            parser.error(f"missing PostgreSQL binary: {binary}")
    runtime = ROOT / ".runtime/team-c"
    runtime.mkdir(parents=True, exist_ok=True)
    cluster = Path(tempfile.mkdtemp(prefix="pg-", dir=runtime))
    data, sockets = cluster / "data", cluster / "socket"
    sockets.mkdir(mode=0o700)
    if len(str(sockets).encode()) > 80:
        parser.error("workspace path is too long for a private PostgreSQL Unix socket")
    pg_ctl = str(pg_bin / "pg_ctl")
    started = False
    stopped = False
    result: dict[str, object] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "kind": "disposable-workbench-postgres",
        "cluster": str(cluster.relative_to(ROOT)),
        "tcp": False,
        "postgres": run([str(pg_bin / "postgres"), "--version"]).stdout.strip(),
        "tests_passed": False,
        "stopped": False,
    }
    code = 1
    try:
        run(
            [
                str(pg_bin / "initdb"),
                "-D",
                str(data),
                "-U",
                "workbench_test_owner",
                "--auth-local=trust",
                "--auth-host=reject",
                "--no-locale",
                "--encoding=UTF8",
            ]
        )
        with (data / "postgresql.conf").open("a") as config:
            socket_literal = str(sockets).replace("'", "''")
            config.write(
                f"\nlisten_addresses = ''\nunix_socket_directories = '{socket_literal}'\n"
                "unix_socket_permissions = 0700\nport = 55439\nshared_buffers = '32MB'\n"
                "max_connections = 20\nspeechnexus.disposable_workbench_probe = 'on'\n"
            )
        # A failed start may still leave a process: always inspect and stop below.
        started = True
        run(
            [
                pg_ctl,
                "-D",
                str(data),
                "-l",
                str(cluster / "postgres.log"),
                "-w",
                "-t",
                "20",
                "start",
            ]
        )
        # libpq also reads PGHOSTADDR/PGSERVICE/PGOPTIONS. Do not let inherited
        # connection defaults redirect this socket-only disposable probe.
        env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        env.update(PGHOST=str(sockets), PGPORT="55439", PGUSER="workbench_test_owner")
        run([str(pg_bin / "createdb"), "speechnexus_workbench_test"], env=env)
        env["SPEECHNEXUS_WORKBENCH_TEST_DSN"] = make_conninfo(
            host=str(sockets),
            port=55439,
            dbname="speechnexus_workbench_test",
            user="workbench_test_owner",
        )
        command = [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_workbench_versions_postgres.py",
            "tests/integration/test_workbench_http_postgres.py",
            "tests/integration/test_workbench_http_management.py",
            "tests/integration/test_workbench_http_meetings.py",
            "tests/integration/test_workbench_sealing_postgres.py",
            "tests/integration/test_workbench_listings_postgres.py",
            "tests/integration/test_workbench_transcript_import_postgres.py",
            "tests/integration/test_workbench_transcript_exports_postgres.py",
            "tests/integration/test_workbench_minutes_postgres.py",
            "tests/integration/test_workbench_minutes_tasks_postgres.py",
            "tests/integration/test_workbench_meetings_postgres.py",
            "tests/integration/test_workbench_deletions_postgres.py",
            "tests/integration/test_workbench_deletion_cleanup.py",
            "tests/integration/test_workbench_content_writes.py",
            "tests/integration/test_workbench_owned_deletions.py",
            "tests/integration/test_workbench_runtime_export_dispatch.py",
            "tests/integration/test_workbench_books_postgres.py",
            "tests/integration/test_workbench_render_units_postgres.py",
            "tests/integration/test_workbench_render_units_http.py",
            "tests/integration/test_workbench_assistant_postgres.py",
            "tests/integration/test_workbench_assistant_http.py",
            "tests/integration/test_workbench_exports_postgres.py",
            "tests/integration/test_workbench_exports_http.py",
            "tests/integration/test_workbench_export_worker_postgres.py",
            "tests/integration/test_workbench_recovery_postgres.py",
            "tests/integration/test_workbench_saved_assets_cleanup.py",
            "tests/integration/test_workbench_saved_assets_postgres.py",
            "tests/unit/backend/test_workbench_postgres_cleanup.py",
            "tests/unit/backend/test_workbench_public_transcript_source.py",
            "tests/unit/backend/test_workbench_llm.py",
            "tests/unit/backend/test_workbench_minutes_tasks.py",
            "-q",
        ]
        if args.browser:
            command.append("tests/integration/test_workbench_meeting_browser.py")
            command.append("tests/integration/test_workbench_deletion_browser.py")
            # The bundle against the deployment's own composition root, not a hand-built app.
            command.append("tests/integration/test_workbench_real_runtime_browser.py")
            # The authoring/proofreading page against the same real composition root.
            command.append("tests/integration/test_workbench_real_runtime_manuscripts_browser.py")
            # The assistant surface must stay closed unless the deployment itself says so.
            command.append("tests/integration/test_workbench_real_runtime_assistant_browser.py")
            # The AdminUI diagnostics page against the same real runtime.
            command.append("tests/integration/test_admin_diagnostics_browser.py")
            # The deployment teleprompter state consumed by the production bundle.
            command.append(
                "tests/integration/test_workbench_real_runtime_prompter_state_browser.py"
            )
            # The audiobook flow: a chapter created in the production bundle, verified over HTTP.
            command.append(
                "tests/integration/test_workbench_real_runtime_chapter_create_browser.py"
            )
            # The realtime tab must not open the microphone before a session is connected.
            command.append(
                "tests/integration/test_workbench_real_runtime_realtime_consent_browser.py"
            )
            # Guards the probe inventory: no C-side probe may be wrapped by nothing.
            command.append("tests/integration/test_probe_inventory.py")
            # Two self-contained synthetic realtime producers (ASR sessions, TTS rendering).
            command.append("tests/integration/test_workbench_realtime_synthetic_probes.py")
            # Five self-contained probes that existed but ran in no gate: capture graph, media
            # worklet, voice creation, the Admin task centre and the file workflow demo.
            command.append("tests/integration/test_workbench_orphan_probes.py")
            env["SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT"] = str(
                args.output.resolve().parent / (args.output.stem + "-browser")
            )
            result["browser"] = "production-bundle-real-c-http-private-pg-fake-llm-and-real-runtime"
        if args.admin_workspaces:
            command.append("tests/integration/test_admin_workspaces_browser.py")
            env["SPEECHNEXUS_ADMIN_WORKSPACES_OUTPUT"] = str(
                args.output.resolve().parent / (args.output.stem + "-admin-workspaces")
            )
            result["admin_workspaces"] = "production-admin-real-a-http-private-pg-explicit-service"
        if args.workbench_runtime:
            command.append("tests/integration/test_workbench_runtime.py")
            # The runtime owns export production; the gate must cover that loop too.
            command.append("tests/integration/test_workbench_runtime_export_dispatch.py")
            # Consumers read capability states from the status document, so the gate pins them.
            command.append("tests/integration/test_workbench_runtime_capabilities.py")
            # The saved-copy read path over real HTTP (write from the base, read back by id).
            command.append("tests/integration/test_workbench_runtime_saved_assets_http.py")
            result["workbench_runtime"] = "real-c-runtime-private-pg"
        if args.coverage:
            env["COVERAGE_FILE"] = str(cluster / ".coverage")
            command.extend(
                [
                    "--cov=speechnexus_backend.workbench.infrastructure",
                    "--cov=speechnexus_backend.workbench.application.minutes_tasks",
                    "--cov=speechnexus_backend.workbench.domain.minutes_tasks",
                    "--cov-report=term-missing",
                    "--cov-fail-under=90",
                    f"--cov-report=json:{cluster / 'coverage.json'}",
                ]
            )
            result["coverage_log"] = str((cluster / "coverage.json").relative_to(ROOT))
        tests = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        (cluster / "pytest.log").write_text(tests.stdout + tests.stderr)
        print(tests.stdout + tests.stderr, end="")
        result["tests_passed"] = tests.returncode == 0
        result["pytest_log"] = str((cluster / "pytest.log").relative_to(ROOT))
        code = tests.returncode
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        # All child inputs are this probe's own synthetic identities, never a shared DSN.
        print(f"probe subprocess failed: {type(error).__name__}", file=sys.stderr)
        result["failure"] = type(error).__name__
    finally:
        if started:
            status = subprocess.run(
                [pg_ctl, "-D", str(data), "status"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if status.returncode == 0:
                stop = subprocess.run(
                    [pg_ctl, "-D", str(data), "-m", "fast", "-w", "-t", "20", "stop"],
                    capture_output=True,
                    text=True,
                    timeout=25,
                    check=False,
                )
                stopped = stop.returncode == 0
            else:
                stopped = not (data / "postmaster.pid").exists()
            status = subprocess.run(
                [pg_ctl, "-D", str(data), "status"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            stopped = stopped and status.returncode != 0 and not (data / "postmaster.pid").exists()
        result["stopped"] = stopped
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"evidence: {output.relative_to(ROOT) if output.is_relative_to(ROOT) else output}")
        if started and not stopped:
            print(
                f"probe shutdown unconfirmed; inspect retained cluster: {cluster}", file=sys.stderr
            )
            code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
