"""Prefetch the locked Playwright APT plan, then run its unchanged installer.

Only the ephemeral Ubuntu CI runner uses this helper. APT authenticates package
indexes; strong digest verification is required before writing its full cache,
because the official installer may reuse completed archives by size alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

PLAYWRIGHT = ["pnpm", "exec", "playwright"]
INSTALL = [*PLAYWRIGHT, "install", "--with-deps", "--only-shell", "chromium"]
PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?")
ARCHIVE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.:%~-]*\.deb")
HASHES = {"SHA256": ("sha256", 64), "SHA512": ("sha512", 128)}


class Archive(NamedTuple):
    uri: str
    name: str
    size: int
    algorithm: str
    digest: str


def missing_packages(result: subprocess.CompletedProcess[str]) -> list[str]:
    lines = result.stdout.strip().splitlines()
    if result.returncode == 0 and lines == ["All system dependencies are installed."]:
        return []
    header = re.fullmatch(
        r"Missing system dependencies \(([1-9][0-9]*)\):", lines[0] if lines else ""
    )
    if result.returncode != 1 or header is None:
        raise ValueError("Unrecognized Playwright dependency plan; refusing to guess packages")
    packages = [line.strip() for line in lines[1:]]
    if (
        len(packages) != int(header[1])
        or len(packages) != len(set(packages))
        or not all(PACKAGE.fullmatch(package) for package in packages)
    ):
        raise ValueError("Invalid Playwright dependency package inventory")
    return packages


def archive_plan(output: str) -> list[Archive]:
    archives: list[Archive] = []
    names: set[str] = set()
    for line in output.splitlines():
        if not line.startswith("'"):
            continue  # APT also prints its ordinary human-readable install summary.
        fields = shlex.split(line)
        if len(fields) != 4:
            raise ValueError("Invalid APT archive record")
        uri, name, size, checksum = fields
        parsed = urlsplit(uri)
        algorithm, separator, digest = checksum.partition(":")
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
            or any(character.isspace() for character in uri)
            or ARCHIVE.fullmatch(name) is None
            or name in names
            or not size.isdecimal()
            or int(size) <= 0
            or not separator
            or algorithm not in HASHES
        ):
            raise ValueError("Unsupported APT archive identity")
        hash_name, length = HASHES[algorithm]
        if re.fullmatch(r"[a-fA-F0-9]{" + str(length) + "}", digest) is None:
            raise ValueError("Invalid APT archive checksum")
        names.add(name)
        archives.append(Archive(uri, name, int(size), hash_name, digest.lower()))
    return archives


def verified(path: Path, archive: Archive) -> bool:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != archive.size:
        return False
    digest = hashlib.new(archive.algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == archive.digest


def capture(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def checked_capture(args: list[str]) -> str:
    result = capture(args)
    if result.returncode:
        # Do not reflect archive URLs, proxy values or arbitrary subprocess text.
        raise RuntimeError(f"{args[0]} dependency planning failed (exit {result.returncode})")
    return result.stdout


def prepare(cache: Path, temporary_root: Path) -> None:
    # Keep custom APT proxy/rate/transport policy on its original path rather
    # than accidentally bypassing it with a second downloader.
    # Request key names only, never proxy credentials or other configuration values.
    policy = capture(["apt-config", "dump", "--format", "%f%n"])
    known_keys = (
        "Proxy",
        "Proxy-Auto-Detect",
        "ProxyAutoDetect",
        "Dl-Limit",
        "Timeout",
        "Pipeline-Depth",
        "No-Cache",
        "Max-Age",
        "No-Store",
        "User-Agent",
        "AllowRedirect",
        "CaInfo",
        "Verify-Peer",
        "Verify-Host",
        "SslCert",
        "SslKey",
    )
    categories = []
    for protocol in ("http", "https"):
        matches = [
            f"{protocol}:{key}"
            for key in known_keys
            if f"Acquire::{protocol}::{key}" in policy.stdout.splitlines()
        ]
        if re.search(rf"Acquire::{protocol}::", policy.stdout) and not matches:
            matches.append(f"{protocol}:other-or-nested-key")
        categories.extend(matches)
    if re.search(r"Acquire::.*ForceIPv", policy.stdout):
        categories.append("address-family")
    # Fixed categories only: no stderr text, arbitrary key suffixes, values,
    # proxy credentials, host names or local configuration paths enter logs.
    print(
        "APT preparation policy: "
        + json.dumps(
            {
                "exit": policy.returncode,
                "stderr": "permission-denied"
                if "permission denied" in policy.stderr.lower()
                else "other"
                if policy.stderr
                else "none",
                "categories": categories,
            }
        ),
        flush=True,
    )
    if (
        policy.returncode
        or policy.stderr
        or re.search(r"Acquire::(?:http|https)::|Acquire::.*ForceIPv", policy.stdout, re.I)
    ):
        print(
            "APT transport policy is custom or unreadable; keeping the original installer.",
            flush=True,
        )
        subprocess.run(INSTALL, check=True)
        return
    subprocess.run(["sudo", "apt-get", "update"], check=True)
    packages = missing_packages(capture([*PLAYWRIGHT, "install-deps", "--dry-run", "chromium"]))
    if packages:
        output = checked_capture(
            [
                "sudo",
                "apt-get",
                "-o",
                "Acquire::ForceHash=sha256",
                "--print-uris",
                "--yes",
                "--no-install-recommends",
                "install",
                *packages,
            ]
        )
        archives = archive_plan(output)
        with tempfile.TemporaryDirectory(prefix="speechnexus-browser-", dir=temporary_root) as root:
            temporary = Path(root)
            plan = temporary / "archives.txt"
            plan.write_text(
                "".join(f"{a.uri}\n  out={a.name}\n" for a in archives), encoding="utf-8"
            )
            if archives:
                subprocess.run(
                    [
                        "aria2c",
                        "--no-conf",
                        "--max-concurrent-downloads=4",
                        "--max-connection-per-server=1",
                        "--split=1",
                        "--max-tries=2",
                        "--connect-timeout=20",
                        "--timeout=30",
                        "--auto-file-renaming=false",
                        "--allow-overwrite=false",
                        "--console-log-level=warn",
                        "--download-result=hide",
                        "--dir",
                        str(temporary),
                        "--input-file",
                        str(plan),
                    ],
                    check=True,
                )
            for archive in archives:
                source = temporary / archive.name
                if not verified(source, archive):
                    raise ValueError("APT archive size/checksum mismatch")
                destination = cache / archive.name
                if destination.exists():
                    if not verified(destination, archive):
                        raise ValueError("Existing APT cache archive does not match the plan")
                    continue
                subprocess.run(
                    ["sudo", "install", "--mode=0644", "--", str(source), str(destination)],
                    check=True,
                )
            print(
                f"Prefetched and verified {len(archives)} APT archives for the official installer."
            )
    subprocess.run(INSTALL, check=True)


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        raise RuntimeError("This dependency preparer is restricted to the existing Linux CI job")
    prepare(Path("/var/cache/apt/archives"), Path(os.environ["RUNNER_TEMP"]))


if __name__ == "__main__":
    main()
