#!/usr/bin/env python3
"""Compare the two-host deployment with the reference Compose assembly.

The reference assembly (`deploy/compose.yaml`) is the definition of the complete
system: one service per deployable unit, each with an explicit variable set. The
two-host deployment is a port of those units onto a k8s application plane and a
Compose compute plane. This script makes the difference machine-checkable, so a
unit or a variable cannot silently disappear from the port.

Three states per reference service:

* ``ported``     - the two-host deployment runs this unit; every variable it
                   declares must appear somewhere under ``deploy/cluster``.
* ``planned``    - the unit is not ported yet and names the plan phase that will
                   port it. Reported, not a failure.
* ``equivalent`` - the unit exists under another name or is the same object; the
                   variable check still applies.

Anything else is a failure, as is a ``ported`` unit missing a variable. The
variable scan is lexical (any occurrence in the cluster tree), which is coarse on
purpose: it can prove absence cheaply, and the plan's phases provide the real
behavioral proof.

Run ``--selftest`` to prove the checks can go red: it feeds a corpus with one
variable removed and a reference with an unmapped service, and fails if the
checker accepts either.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "deploy/compose.yaml"
CLUSTER = ROOT / "deploy/cluster"

# reference service -> (state, where it runs in the two-host deployment, note)
COVERAGE: dict[str, tuple[str, str, str]] = {
    "postgres": ("ported", "k8s app-plane/postgres.yaml", ""),
    "minio": ("ported", "k8s app-plane/minio.yaml", ""),
    "minio-init": ("ported", "k8s app-plane/minio-init.yaml", ""),
    "migrate": ("ported", "k8s app-plane/migrate-job.yaml", ""),
    "api": ("ported", "k8s app-plane/api.yaml", ""),
    "asset-inspector": ("ported", "k8s app-plane/asset-inspector.yaml", ""),
    "gateway": ("ported", "MegaCube .runtime/cluster/compute/compose.yaml", ""),
    "job-dispatch-worker": (
        "ported",
        "MegaCube compute compose, beside the gateway",
        "the reference runs it with network_mode: service:gateway, so the port keeps them together",
    ),
    "engine-register": ("ported", "k8s app-plane/register-engine-job.yaml", ""),
    "binding-disable-worker": (
        "ported",
        "MegaCube compute compose, beside the gateway",
        "its runtime refuses a plain-HTTP gateway origin that is not loopback",
    ),
    "artifact-retention": ("ported", "k8s app-plane/workers.yaml", ""),
    "admission-watch": ("ported", "k8s app-plane/workers.yaml", ""),
    "engine-readiness": ("ported", "k8s app-plane/workers.yaml", ""),
    "engine-readiness-renew": ("ported", "k8s app-plane/workers.yaml", ""),
    # #1293: the pass runs under the dispatcher's identity, so it is a scheduled
    # unit here while the reference runs it with ``docker compose run --once``.
    "job-cancellation-reconciler": (
        "ported",
        "k8s app-plane/workers.yaml (CronJob job-cancellation)",
        "scheduled pass replaces the reference's explicit run --rm invocation",
    ),
    "workbench-migrate": ("ported", "k8s app-plane/workbench.yaml", ""),
    "workbench-api": ("ported", "k8s app-plane/workbench.yaml", ""),
    "workbench": ("ported", "k8s app-plane/web.yaml", ""),
    "admin": ("ported", "k8s app-plane/web.yaml", ""),
}

# Variables the port legitimately does not carry. Every entry needs a reason.
ENV_EXCEPTIONS: dict[str, str] = {
    "SPEECHNEXUS_WORKBENCH_MIGRATION_DATABASE_URL": "workbench migrate arrives with phase C",
    # Voice, pronunciation and profile publication are separate product slices; the
    # deployment ports their variables together with the units that consume them.
    "SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL": "voice pronunciation slice not assembled",
    "SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL": "voice pronunciation slice not assembled",
    "SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL": "voice pronunciation slice not assembled",
    "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL": "built-in voice publication slice not assembled",
    "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE": "profile publication slice not assembled",
    # The reference runs the audio sandbox in-process (bubblewrap + prlimit); the
    # two-host deployment uses the separate inspector container instead, which is
    # the same boundary and the only one Docker Desktop can provide.
    "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP": "inspector replaces in-process bubblewrap",
    "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND": "inspector replaces in-process launcher",
    "SPEECHNEXUS_AUDIO_INSPECTION_CPU_SECONDS": "inspector owns its rlimits",
    "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON": "inspector replaces launcher",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS": "sandboxed inspector owns its limits",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_INPUT_BYTES": "sandboxed inspector owns its limits",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_RECEIPT_BYTES": "sandboxed inspector owns its limits",
    "SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES": "inspector owns its rlimits",
    "SPEECHNEXUS_AUDIO_INSPECTION_OUTPUT_BYTES": "inspector owns its limits",
    "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT": "inspector owns its rlimits",
    "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT": "sandboxed inspector owns its runtime root",
    "SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS": "sandboxed inspector owns its rlimits",
    # Defaults-writer and profile-writer identities stay unconfigured on purpose;
    # see docs/operations/compose-minimal.md §"不创建的账号".
    "SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL": "defaults-writer not created by design",
    "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL": "profile-writer identity is not created by design",
    # File TTS and voice-clone/voice-design lanes are not assembled in the two-host
    # deployment yet; their gateway config files and object credentials arrive with
    # those lanes (phase C of the plan).
    "SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE": "file TTS lane not assembled",
    "SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_ACCESS_KEY": "file TTS lane not assembled",
    "SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_SECRET_KEY": "file TTS lane not assembled",
    "SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_CONFIG_FILE": "voice clone lane not assembled",
    "SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_OBJECT_ACCESS_KEY": "voice clone lane not assembled",
    "SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_OBJECT_SECRET_KEY": "voice clone lane not assembled",
    "SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE": "voice design lane not assembled",
    "SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_OBJECT_ACCESS_KEY": "voice design lane not assembled",
    "SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_OBJECT_SECRET_KEY": "voice design lane not assembled",
}

# Units that exist only in the two-host deployment, with the reason they exist.
EXTRA_UNITS: dict[str, str] = {
    "bridge": "the two hosts have no managed private network; the tunnel lives in a k8s Pod",
    "node-controller": "runs beside the docker daemon it manages, as in the reference topology",
    "engine": "started by the controller from an approved template, not by Compose",
}


def reference_services(path: Path) -> dict[str, list[str]]:
    document = yaml.safe_load(path.read_text())
    services = document.get("services") or {}
    result: dict[str, list[str]] = {}
    for name, body in services.items():
        env = (body or {}).get("environment") or {}
        result[name] = (
            sorted(env)
            if isinstance(env, dict)
            else sorted(entry.split("=", 1)[0] for entry in env)
        )
    return result


def cluster_corpus(root: Path) -> str:
    chunks = []
    for path in sorted(root.rglob("*")):
        if path.name == Path(__file__).name:
            continue
        suffixes = {".py", ".yaml", ".yml", ".sh", ".env", ".md", ".json"}
        if path.is_file() and path.suffix in suffixes:
            chunks.append(path.read_text(errors="ignore"))
    return "\n".join(chunks)


# Cluster-assigned addresses (Service CIDR, Docker's bridge range) are environment
# facts: they change when the cluster is recreated, so they are rendered at deploy
# time (render-web-upstreams.py) and must never be committed.
ENVIRONMENT_ADDRESS = re.compile(r"\b(?:10\.96|172\.17|10\.244)\.\d{1,3}\.\d{1,3}\b")

# A VRAM ratio is correct for exactly one card size. The approved template is the
# same file on a 48 GB production card and on a development box, so the number
# belongs in deployment configuration, not here.
TEMPLATE_GPU_RATIO = re.compile(r"--gpu-memory-utilization")


def check(
    services: dict[str, list[str]], corpus: str, coverage: dict[str, tuple[str, str, str]]
) -> list[str]:
    problems: list[str] = []
    for address in sorted(set(ENVIRONMENT_ADDRESS.findall(corpus))):
        problems.append(
            f"hardcoded cluster address {address}: assigned by the cluster, render it instead"
        )
    if TEMPLATE_GPU_RATIO.search(corpus):
        problems.append(
            "approved template carries --gpu-memory-utilization: the VRAM budget is "
            "deployment configuration (ENGINE_GPU_MEMORY_MIB), not a template constant"
        )
    for name in services:
        if name not in coverage:
            problems.append(f"{name}: no coverage entry in the two-host deployment")
    for name, (state, _where, note) in coverage.items():
        if state not in {"ported", "equivalent", "planned"}:
            problems.append(f"{name}: unknown state {state!r}")
        if state == "planned" and not note:
            problems.append(f"{name}: planned without a phase note")
        if state == "planned":
            continue
        for key in services.get(name, []):
            if key in ENV_EXCEPTIONS:
                continue
            if key not in corpus:
                problems.append(f"{name}: variable {key} is not carried by the port")
    return problems


def render(services: dict[str, list[str]], coverage: dict[str, tuple[str, str, str]]) -> str:
    rows = ["| unit | state | location |", "| --- | --- | --- |"]
    for name in sorted(services):
        state, where, _ = coverage.get(name, ("MISSING", "-", ""))
        rows.append(f"| `{name}` | {state} | {where} |")
    return "\n".join(rows)


def selftest() -> int:
    services = reference_services(REFERENCE)
    corpus = cluster_corpus(CLUSTER)
    coverage = dict(COVERAGE)

    baseline = check(services, corpus, coverage)
    if baseline:
        print("selftest: the real tree already has problems, fix those first", file=sys.stderr)
        for problem in baseline:
            print(f"  {problem}", file=sys.stderr)
        return 1

    # 1. A dropped variable must be reported.
    dropped = next(key for key in services["api"] if key not in ENV_EXCEPTIONS)
    stripped = corpus.replace(dropped, "")
    red = check(services, stripped, coverage)
    if not any(dropped in problem for problem in red):
        print(f"selftest FAILED: dropping {dropped} was not detected", file=sys.stderr)
        return 1

    # 2. An unmapped reference service must be reported.
    red = check({**services, "realtime-gateway": []}, corpus, coverage)
    if not any("realtime-gateway" in problem for problem in red):
        print("selftest FAILED: an unmapped service was accepted", file=sys.stderr)
        return 1

    # 3. A committed cluster address must be reported, not merely tolerated: the
    # value is assigned by the cluster and belongs in a rendered ConfigMap.
    injected = "10." + "96.7.7"
    red = check(services, corpus + f"\nWEB_SPEECH_UPSTREAM=http://{injected}:8000\n", coverage)
    if not any(injected in problem for problem in red):
        print("selftest FAILED: a hardcoded cluster address was not detected", file=sys.stderr)
        return 1

    # 4. A planned unit without a phase note must be reported.
    red = check(services, corpus, {**coverage, "admin": ("planned", "phase C", "")})
    if not any("admin: planned without a phase note" in problem for problem in red):
        print("selftest FAILED: a plan entry without a phase note was accepted", file=sys.stderr)
        return 1

    print(
        "selftest OK: dropped variable, unmapped service, hardcoded address "
        "and note-less plan all go red"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="prove the checks can go red")
    args = parser.parse_args()
    if args.selftest:
        return selftest()

    services = reference_services(REFERENCE)
    corpus = cluster_corpus(CLUSTER)
    print(render(services, COVERAGE))
    print()
    print("reference-only units (two-host additions):")
    for name, reason in EXTRA_UNITS.items():
        print(f"  {name}: {reason}")
    print()

    problems = check(services, corpus, COVERAGE)
    planned = sorted(n for n, (s, _, _) in COVERAGE.items() if s == "planned")
    if planned:
        print(f"planned, not yet a failure: {', '.join(planned)}")
    if problems:
        print(f"\nRESULT FAIL ({len(problems)} problems)")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("\nRESULT OK: every ported unit carries its reference variables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
