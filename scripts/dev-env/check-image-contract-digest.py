#!/usr/bin/env python3
"""Compare one built Web image's baked contract digest with a contract revision.

Each Web bundle compiles in the contract digest it was generated from, and the API
reports the same value at `GET /v1/nexus/version`; equal values mean the frontend and
the API were generated from one contract revision. `same-version-check.md` says how to
read that pair at runtime. This script is the build-time counterpart: it reads the
digest out of an *image* rather than a host `dist` directory, so an image that was
built on another host can be checked after `docker load` without a checkout, a
toolchain or a running stack.

Usage (from the repository root):

    uv run python scripts/dev-env/check-image-contract-digest.py <image>
    uv run python scripts/dev-env/check-image-contract-digest.py <image> --expect sha256:…
    uv run python scripts/dev-env/check-image-contract-digest.py <image> \
        --bundle-root /usr/share/nginx/html

Without `--expect` the expected value is recomputed from this checkout's `contracts/`
by the same generator `GET /v1/nexus/version` and the bundle are built from, so a
mismatch means the image and this tree disagree. `--expect` is for a host without a
checkout and takes one of two sources only: a digest recomputed offline on the commit
being checked, or the digest the building host recorded in its release record. It must
not take the value the inspected deployment reports at `GET /v1/nexus/version` — that is
the deployment vouching for itself (an altered bundle with an untouched label passes
against its own value), and a check fed that way proves nothing about the commit.

Two bundle contents are not the digest: a single repeated hex character is a committed
test fixture (`contracts/**` carries `sha256:aaaa…`-shaped placeholders that the
production program can render), and any other candidate is reported as ambiguous
instead of being guessed at. Only `*.js` under the bundle root is scanned; a digest
carried by another file type is not seen and reads as an image that bakes in none.

Exit codes: 0 when the bundle's digest equals the expected one; 1 when it does not,
when the bundle carries no digest (an image built before the same-version guard
existed), or when more than one candidate remains; 2 when the check could not run
(Docker missing, image absent, no published bundle in the image).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = Path(__file__).with_name("generate-contract-bindings.py")

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REPEATED_FIXTURE = re.compile(r"sha256:(.)\1{63}")
GUARD_MARKER = b"data-nexus-contract-check"
DEFAULT_BUNDLE_ROOT = "/usr/share/nginx/html"


def abort(message: str) -> NoReturn:
    """Exit 2: the check could not run, so it says nothing about the image."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def load_generator() -> Any:
    """Import the hyphenated generator module by path, like the CI guards do."""
    spec = importlib.util.spec_from_file_location("generate_contract_bindings", GENERATOR)
    if spec is None or spec.loader is None:
        abort(f"Unable to load {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(arguments: list[str], what: str) -> str:
    """Run one Docker command and return its stdout, naming the failure."""
    try:
        completed = subprocess.run(arguments, capture_output=True, text=True)
    except OSError as error:
        abort(f"{what} could not run: {error}")
    if completed.returncode != 0:
        abort(f"{what} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout


def image_labels(image: str) -> dict[str, str]:
    """Read the source labels a build records, without requiring any of them."""
    labels = run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image],
        f"docker image inspect {image}",
    )
    return json.loads(labels.strip() or "null") or {}


def copy_bundle(image: str, bundle_root: str, destination: Path) -> int:
    """Copy the image's published bundle out, so the scan needs nothing from the image."""
    container = run(["docker", "create", image], f"docker create {image}").strip()
    try:
        try:
            subprocess.run(
                ["docker", "cp", f"{container}:{bundle_root}/.", str(destination)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            abort(
                f"{image} publishes no bundle at {bundle_root}; pass --bundle-root with "
                "the directory the image serves"
            )
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, text=True)
    return len(list(destination.rglob("*.js")))


def scan(bundle: Path) -> tuple[dict[str, list[str]], list[str], bool]:
    """Collect the digest candidates, the files that mention them and the guard marker."""
    candidates: dict[str, list[str]] = {}
    fixtures: set[str] = set()
    guard = False
    scripts = sorted(bundle.rglob("*.js"))
    for path in scripts:
        content = path.read_bytes()
        guard = guard or GUARD_MARKER in content
        text = content.decode("utf-8", "replace")
        for digest in DIGEST.findall(text):
            if REPEATED_FIXTURE.fullmatch(digest):
                fixtures.add(digest)
                continue
            mentioned = candidates.setdefault(digest, [])
            if path.name not in mentioned:
                mentioned.append(path.name)
    return candidates, sorted(fixtures), guard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "image",
        help="Built Web image reference, for example speechnexus/web-workbench:<sha>.",
    )
    parser.add_argument(
        "--expect",
        metavar="SHA256",
        help="Expected digest, from exactly two sources: recomputed offline on the "
        "commit being checked, or recorded by the building host in its release record. "
        "Never the value the inspected deployment reports for itself. Defaults to this "
        "checkout's contracts recomputed by the generator.",
    )
    parser.add_argument(
        "--bundle-root",
        default=DEFAULT_BUNDLE_ROOT,
        metavar="PATH",
        help=f"Directory the image publishes the built page from (default: {DEFAULT_BUNDLE_ROOT}).",
    )
    arguments = parser.parse_args()

    expected = (
        arguments.expect.strip()
        if arguments.expect is not None
        else load_generator().contract_digest(ROOT / "contracts")
    )
    labels = image_labels(arguments.image)
    with tempfile.TemporaryDirectory(prefix="next-digest-") as temporary:
        bundle = Path(temporary)
        script_count = copy_bundle(arguments.image, arguments.bundle_root, bundle)
        candidates, fixtures, guard = scan(bundle)

    print(f"image            {arguments.image}")
    print(
        "source commit    "
        f"{labels.get('org.speechnexus.source.commit', '<no label>')} "
        f"(dirty={labels.get('org.speechnexus.source.dirty', '<no label>')})"
    )
    print(f"bundle           {arguments.bundle_root} ({script_count} bundled scripts)")
    print(f"same-version guard {'present' if guard else 'ABSENT'}")
    if fixtures:
        print(f"ignored fixtures {', '.join(fixtures)} (committed placeholders, not this digest)")

    if not candidates:
        print(f"expected         {expected}")
        print(
            "FAIL: the bundle bakes in no contract digest. An image this old predates "
            "the same-version guard (#853), so it cannot be shown to be the same "
            "contract revision as the API; rebuild it from the commit under check.",
            file=sys.stderr,
        )
        return 1
    if len(candidates) > 1:
        print(f"expected         {expected}")
        for digest, files in sorted(candidates.items()):
            print(f"candidate        {digest} in {', '.join(files)}")
        print(
            "FAIL: more than one digest remains, so the baked value cannot be told apart "
            "from the others; inspect the candidates by hand.",
            file=sys.stderr,
        )
        return 1

    baked, files = next(iter(candidates.items()))
    print(f"baked digest     {baked} ({', '.join(files)})")
    print(f"expected         {expected}")
    if baked != expected:
        print(
            "FAIL: the image and the expected contract revision disagree. Either the "
            "image was built from a different commit, or the expectation is not this "
            "image's contract revision.",
            file=sys.stderr,
        )
        return 1
    print("OK: the image's bundle carries this contract revision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
