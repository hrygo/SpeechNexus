"""Approved repeatable official bwrap rules on one ephemeral Python CI job VM.

Derived from the consumed PR155 experiment's verification/cleanup guards; this
policy has its own per-job identity and claim, never the old approval marker.
Run only in the GitHub-hosted job. Tests mock every policy-affecting command.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

REPOSITORY = "hrygo/SpeechNexus"
VERSION = "4.0.1really4.0.1-0ubuntu0.24.04.9"
PACKAGE_SHA = "90b02aa006eea7702cd4e851343e469e41365dda42145a3cb035de1d6c773b8c"
PROFILE_SHA = "11d39094f044f0cda0febb3ad517b830301da6b2ce929664af09ee9e4dd264f9"
MEMBER = "usr/share/apparmor/extra-profiles/bwrap-userns-restrict"
PARSER = "/usr/sbin/apparmor_parser"
NAMES = {"bwrap": "enforce", "unpriv_bwrap": "enforce"}


def command(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, timeout=60)


def related_profiles() -> dict[str, str]:
    result = json.loads(command(["sudo", "-n", "/usr/sbin/aa-status", "--json"]).stdout)
    profiles = result.get("profiles")
    if not isinstance(profiles, dict):
        raise RuntimeError("Unknown AppArmor status format")
    return {str(k): str(v) for k, v in profiles.items() if "bwrap" in k}


def process_snapshot() -> dict[str, str]:
    result = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            status = directory.joinpath("status").read_text()
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            if os.getuid() not in [int(value) for value in uid_line.split()[1:]]:
                continue
            fields = directory.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            result[directory.name] = fields[19]
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ESRCH):
                raise
    return result


def own_ancestors() -> set[str]:
    result = set()
    pid = os.getpid()
    while pid > 1:
        result.add(str(pid))
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        pid = int(fields[1])
    return result


def new_processes(before: dict[str, str], after: dict[str, str], exclude: set[str]) -> list[str]:
    return [pid for pid, start in after.items() if pid not in exclude and before.get(pid) != start]


def claim_once(directory: Path) -> None:
    with directory.joinpath("load-consumed").open("x") as stream:
        stream.write("One load attempt in this job VM; query an uncertain result, never retry.\n")


def extract_profile(package: Path) -> bytes:
    if (
        package.is_symlink()
        or not package.is_file()
        or package.stat().st_size > 32 * 1024 * 1024
        or hashlib.sha256(package.read_bytes()).hexdigest() != PACKAGE_SHA
    ):
        raise RuntimeError("Official package digest differs")
    unpacked = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(package)],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(unpacked)) as archive:
        members = [
            member for member in archive.getmembers() if member.name in {MEMBER, "./" + MEMBER}
        ]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 262144:
            raise RuntimeError("Expected exactly one bounded regular official profile")
        source = archive.extractfile(members[0])
        if source is None:
            raise RuntimeError("Official profile could not be read")
        payload = source.read(262145)
    if hashlib.sha256(payload).hexdigest() != PROFILE_SHA:
        raise RuntimeError("Official profile digest differs")
    return payload


def save(directory: Path, state: dict[str, Any]) -> None:
    temporary = directory / "receipt.json.tmp"
    with temporary.open("w") as stream:
        stream.write(json.dumps(state, sort_keys=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(directory / "receipt.json")
    print(json.dumps(state, sort_keys=True), flush=True)


def cleanup(directory: Path, state: dict[str, Any]) -> None:
    if not state.get("load_attempted"):
        state["cleanup"] = "no load attempted; no policy touched"
        save(directory, state)
        return
    if (
        not state.get("load_command_finished")
        or state.get("load_returncode") != 0
        or state.get("profiles_after_load") != NAMES
    ):
        state["profiles_after_uncertain_load"] = related_profiles()
        save(directory, state)
        raise RuntimeError("Load ownership uncertain: query only, retain policy for VM teardown")
    unexpected = new_processes(state["processes_before"], process_snapshot(), own_ancestors())
    state["remaining_new_uid_processes"] = unexpected
    save(directory, state)
    if unexpected:
        raise RuntimeError("Stop unknown: retain policy until ephemeral VM teardown")
    profile = directory / "bwrap-userns-restrict"
    if hashlib.sha256(profile.read_bytes()).hexdigest() != PROFILE_SHA:
        raise RuntimeError("Cleanup profile digest differs; retain policy for VM teardown")
    if not state.get("cleanup_attempted"):
        if related_profiles() != NAMES:
            raise RuntimeError("Loaded rule state changed; retain policy for VM teardown")
        state["cleanup_attempted"] = True
        save(directory, state)
        # Only the two newly added official names; no service restart or global policy reload.
        try:
            command(["sudo", "-n", PARSER, "-R", "-K", "-I", "/etc/apparmor.d", str(profile)])
        finally:
            state["profiles_after_cleanup"] = related_profiles()
            save(directory, state)
    else:
        state["profiles_after_cleanup"] = related_profiles()
        save(directory, state)
    if state["profiles_after_cleanup"] != state["profiles_before"]:
        raise RuntimeError("Cleanup uncertain: retain failure and allow VM teardown; never retry")
    state["cleanup"] = "new UID processes absent; only newly loaded rules removed"
    save(directory, state)


def runtime_identity(env: dict[str, str]) -> dict[str, str]:
    """A non-matrix job is uniquely identified by run/attempt/job key and VM name.

    No Actions permission or credential is added to look up numeric job IDs.
    The artifact links this tuple to normal Actions metadata after the run.
    """
    fixed = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_JOB": "python_checks",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
    }
    if any(env.get(key) != value for key, value in fixed.items()):
        raise RuntimeError("Policy is restricted to the repository's hosted Python test VM")
    event, ref = env.get("GITHUB_EVENT_NAME"), env.get("GITHUB_REF", "")
    if not (
        (event == "push" and ref == "refs/heads/main")
        or (event == "pull_request" and re.fullmatch(r"refs/pull/[1-9][0-9]*/merge", ref))
        or (event == "workflow_dispatch" and ref.startswith("refs/heads/"))
    ):
        raise RuntimeError("Unsupported policy event/ref; no rule loading")
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not re.fullmatch(r"[1-9][0-9]{0,19}", env.get(name, "")):
            raise RuntimeError("Missing run or attempt identity")
    if not re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", "")):
        raise RuntimeError("Missing exact checked-out source identity")
    if not 1 <= len(env.get("RUNNER_NAME", "")) <= 256:
        raise RuntimeError("Missing runner identity")
    return {
        name: env[name]
        for name in (
            *fixed,
            "GITHUB_EVENT_NAME",
            "GITHUB_REF",
            "GITHUB_RUN_ID",
            "GITHUB_RUN_ATTEMPT",
            "GITHUB_SHA",
            "RUNNER_NAME",
        )
    }


def check_existing_policy() -> dict[str, str]:
    before = related_profiles()
    if before:
        raise RuntimeError("Existing bwrap rules must never be replaced")
    candidates = list(Path("/etc/apparmor.d").glob("*bwrap*"))
    for folder in ("local", "disable", "force-complain"):
        candidates += list(Path("/etc/apparmor.d", folder).glob("*bwrap*"))
    if any(os.path.lexists(path) for path in candidates):
        raise RuntimeError("Existing rule files or local overrides require separate review")
    binary = Path("/usr/bin/bwrap")
    if binary.stat().st_mode & 0o6000:
        raise RuntimeError("Set-ID bwrap is outside the approved policy")
    try:
        capabilities = os.getxattr(binary, "security.capability")
    except OSError as error:
        if error.errno != errno.ENODATA:
            raise
        capabilities = b""
    if capabilities:
        raise RuntimeError("File capabilities are outside the approved policy")
    return before


def load(directory: Path, state: dict[str, Any]) -> None:
    if state.get("load_attempted") or directory.joinpath("load-consumed").exists():
        raise RuntimeError("This VM already consumed its load attempt; never reload")
    if command(["git", "rev-parse", "HEAD"]).stdout.strip() != state["runtime"]["GITHUB_SHA"]:
        raise RuntimeError("Checkout differs from the recorded source")
    before = check_existing_policy()
    command(["apt-get", "download", "apparmor-profiles=" + VERSION], cwd=directory)
    packages = list(directory.glob("apparmor-profiles_*_all.deb"))
    if len(packages) != 1:
        raise RuntimeError("Expected exactly one official package")
    profile = directory / "bwrap-userns-restrict"
    profile.write_bytes(extract_profile(packages[0]))
    command([PARSER, "-Q", "-K", "-I", "/etc/apparmor.d", str(profile)])
    state.update(
        package_sha256=PACKAGE_SHA,
        profile_sha256=PROFILE_SHA,
        parser_version=command([PARSER, "--version"]).stdout.strip()[:200],
        profiles_before=before,
        processes_before=process_snapshot(),
    )
    claim_once(directory)
    state["load_attempted"] = True
    save(directory, state)
    # One kernel load per VM. Timeout/failure never retries loading.
    try:
        command(["sudo", "-n", PARSER, "-a", "-K", "-I", "/etc/apparmor.d", str(profile)])
    except subprocess.CalledProcessError as error:
        state.update(load_command_finished=True, load_returncode=error.returncode)
        save(directory, state)
        raise
    state.update(load_command_finished=True, load_returncode=0)
    state["profiles_after_load"] = related_profiles()
    save(directory, state)
    if state["profiles_after_load"] != NAMES:
        raise RuntimeError("The two official rules must be the only enforced bwrap rules")


def main() -> None:
    identity = runtime_identity(dict(os.environ))
    directory = Path(os.environ["RUNNER_TEMP"]) / "speech-inspection-profile"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("Policy receipt directory cannot be a symlink")
    receipt = directory / "receipt.json"
    if receipt.is_symlink():
        raise RuntimeError("Policy receipt cannot be a symlink")
    if receipt.exists():
        if receipt.stat().st_size > 1024 * 1024:
            raise RuntimeError("Policy receipt exceeds its bound")
        state = json.loads(receipt.read_text())
        if not isinstance(state, dict) or state.get("runtime") != identity:
            raise RuntimeError("Receipt belongs to another job/source/VM")
    else:
        if directory.joinpath("load-consumed").exists():
            raise RuntimeError("Load claim without receipt is unknown; retain VM policy")
        state = {"runtime": identity, "load_attempted": False}
    try:
        if sys.argv[1:] == ["load"]:
            load(directory, state)
        elif sys.argv[1:] == ["cleanup"]:
            cleanup(directory, state)
        else:
            raise RuntimeError("Expected load or cleanup")
    except Exception as error:
        state["failure"] = f"{type(error).__name__}: {str(error)[:500]}"
        save(directory, state)
        raise


if __name__ == "__main__":
    main()
