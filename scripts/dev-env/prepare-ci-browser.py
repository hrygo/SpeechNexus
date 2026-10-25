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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

PLAYWRIGHT = ["pnpm", "exec", "playwright"]
INSTALL = [*PLAYWRIGHT, "install", "--with-deps", "--only-shell", "chromium"]
PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?")
ARCHIVE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.:%~-]*\.deb")
HASHES = {"SHA256": ("sha256", 64), "SHA512": ("sha512", 128)}
APT_HELPER = "/usr/lib/apt/apt-helper"


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
        remote = parsed.scheme in {"http", "https", "mirror+http", "mirror+https"} and bool(
            parsed.hostname
        )
        mirror_file = (
            parsed.scheme == "mirror+file"
            and parsed.path.startswith("/etc/apt/")
            and not {".", ".."}.intersection(parsed.path.split("/"))
        )
        if (
            not (remote or mirror_file)
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


def download(archive: Archive, temporary: Path) -> None:
    label = next(name for name, (algorithm, _) in HASHES.items() if algorithm == archive.algorithm)
    # Use APT's own acquire system with its existing configuration, never an
    # override of timeout, pipelining, cache headers or authentication policy.
    subprocess.run(
        [
            APT_HELPER,
            "download-file",
            archive.uri,
            str(temporary / archive.name),
            f"{label}:{archive.digest}",
            f"Checksum-FileSize:{archive.size}",
        ],
        check=True,
    )


def prefetch(archives: list[Archive], temporary: Path) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(download, archive, temporary) for archive in archives]
        for future in futures:
            future.result()


def prepare(cache: Path, temporary_root: Path) -> None:
    # Native APT inherits observed controls without changing their values.
    # Unknown/custom controls retain the old path.
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
        "User-Agent-Non-Interactive",
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
        known = {f"Acquire::{protocol}::{key}" for key in known_keys}
        if any(
            re.search(rf"Acquire::{protocol}::", line, re.I) and line not in known
            for line in policy.stdout.splitlines()
        ):
            matches.append(f"{protocol}:other-or-nested-key")
        # Expose only a documented control name when a qualified key contains
        # it; arbitrary host/credential components remain entirely private.
        for line in policy.stdout.splitlines():
            split = re.split(rf"Acquire::{protocol}::", line, flags=re.I)
            if len(split) != 2 or line in known:
                continue
            parts = [part.lower() for part in split[1].split("::") if part]
            controls = [key for key in known_keys if key.lower() in parts]
            matches.extend(f"{protocol}:qualified-{key}" for key in controls)
            if not controls:
                matches.append(
                    f"{protocol}:unclassified-{'nested' if len(parts) > 1 else 'direct'}"
                )
        matches = sorted(set(matches))
        categories.extend(matches)
    if re.search(r"Acquire::.*ForceIPv", policy.stdout, re.I):
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
    supported = {
        f"Acquire::{protocol}::{key}"
        for protocol in ("http", "https")
        for key in ("Timeout", "Pipeline-Depth", "No-Cache")
    }
    # APT 2.8.1 methods/http.cc reads this exact direct field via ConfigFindB
    # and selects its non-interactive marker from the HTTP method's systemd
    # unit context. apt-helper uses that same acquire method and existing
    # configuration; never synthesize or override the User-Agent header here.
    supported.add("Acquire::http::User-Agent-Non-Interactive")
    transport_keys = [
        line
        for line in policy.stdout.splitlines()
        if re.search(r"Acquire::(?:http|https)::|Acquire::.*ForceIPv", line, re.I)
    ]
    reasons = []
    if policy.returncode:
        reasons.append("config-command-failed")
    if policy.stderr:
        reasons.append("config-stderr")
    if os.environ.get("APT_CONFIG"):
        reasons.append("alternate-config")
    unsupported = [key for key in transport_keys if key not in supported]
    if unsupported:
        reasons.append("unsupported-transport-key")
    for pattern, reason in (
        (r"Acquire::Queue-Mode", "queue-control"),
        (r"Acquire::ForceHash", "hash-control"),
        (r"Binary::apt-get::Acquire::", "apt-get-binary-control"),
        (r"Binary::apt-helper::Acquire::", "apt-helper-binary-control"),
    ):
        if re.search(pattern, policy.stdout, re.I):
            reasons.append(reason)
    # This classifies namespace only, never the arbitrary unsupported suffix.
    scopes = set()
    for key in unsupported:
        for prefix, scope in (
            ("Acquire::", "global"),
            ("Binary::apt-get::", "apt-get-binary"),
            ("Binary::apt-helper::", "apt-helper-binary"),
            ("Binary::apt::", "apt-binary"),
        ):
            if key.lower().startswith(prefix.lower()):
                scopes.add(scope)
                break
        else:
            scopes.add("other-namespace")
    safe_direct: dict[str, list[str]] = {"http": [], "https": []}
    empty_direct = 0
    hidden_unsupported = 0
    for key in unsupported:
        direct = re.fullmatch(r"Acquire::(http|https)::([A-Za-z][A-Za-z0-9_-]{0,63})", key, re.I)
        if direct:
            safe_direct[direct[1].lower()].append(direct[2])
        elif re.fullmatch(r"Acquire::(?:http|https)::", key, re.I):
            empty_direct += 1
        else:
            hidden_unsupported += 1
    print(
        "APT prefetch guard: "
        + json.dumps(
            {
                "reasons": reasons,
                "unsupported_scopes": sorted(scopes),
                "safe_direct_fields": {
                    protocol: sorted(set(names)) for protocol, names in safe_direct.items()
                },
                "empty_direct_key_count": empty_direct,
                "hidden_unsupported_key_count": hidden_unsupported,
            }
        ),
        flush=True,
    )
    if reasons:
        print(
            "APT transport policy is custom or unreadable; keeping the original installer.",
            flush=True,
        )
        subprocess.run(INSTALL, check=True)
        return
    print("Preparing archives with at most four native APT acquire workers.", flush=True)
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
            prefetch(archives, temporary)
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
                f"Prefetched and verified {len(archives)} APT archives for the official installer.",
                flush=True,
            )
    subprocess.run(INSTALL, check=True)


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        raise RuntimeError("This dependency preparer is restricted to the existing Linux CI job")
    prepare(Path("/var/cache/apt/archives"), Path(os.environ["RUNNER_TEMP"]))


if __name__ == "__main__":
    main()
