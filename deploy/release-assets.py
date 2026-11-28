#!/usr/bin/env python3
"""Derive the self-contained release attachments from the development compose file.

Why this exists
---------------
`deploy/compose.yaml` is a build-from-source file: every SpeechNexus service carries
`build:` and no `image:`, and `minio` names a tag (`speechnexus-minio:RELEASE.…`) that
only exists on the host that built it. Publishing that file verbatim as the release
attachment produced an attachment that resolved to project-local image names and could
not start without a source checkout:

    $ docker compose -p relaccpure --env-file compose.env \
        -f speechnexus-v0.1.0-compose.yaml config --images
    postgres:16.15-alpine
    relaccpure-api                       <- not ghcr.io/hrygo/speechnexus-python
    relaccpure-gateway
    speechnexus-minio:RELEASE.2025-09-07T16-13-09Z
    relaccpure-minio-init
    $ docker compose … up -d postgres minio
    resolve : lstat /private/tmp/deploy: no such file or directory

What this program writes
------------------------
The compose file, rewritten while keeping every comment, profile, `depends_on`,
healthcheck, `x-` env anchor and `${VAR}` placeholder byte for byte:

* every SpeechNexus service loses `build:` and gains
  `image: <registry>/<repository>@sha256:<digest>`, taking the digest from the
  published `speechnexus-<tag>-images.txt` — the same file the release page carries;
* `minio` is published too (`deploy/images/minio.Dockerfile`), because the upstream
  image is not anonymously pullable any more: the `minio/minio` Docker Hub repository
  does not exist and `quay.io/minio/minio` answers 401;
* `postgres` keeps its upstream tag. It is a third-party image that pulls anonymously,
  it is not something this repository builds, and resolving a Docker Hub digest at
  publication time would add a rate-limited registry read to the release path. The
  manifest records it separately, with `digest_pinned: false`, so the difference is
  visible instead of implied;
* the three scripts the compose file bind-mounts from the checkout
  (`init-object-storage.py`, `job-startup-gate.py`, `workbench-startup-gate.py`) are
  copied next to the compose as `speechnexus-<tag>-<name>` attachments and the mounts
  are repointed at them, so the attachment set is closed under what the file needs and
  a `docker compose up` from a directory holding only the attachments works. They are
  attachments rather than inline Compose `configs` because Compose refuses a `content:`
  config on a `read_only: true` service ("`file` is the sole supported option"), and
  weakening those services to carry the script inline is not a trade this file makes;
* `x-python-build` is dropped: it only ever described the build context (`..`), and
  leaving a dead anchor that names a source directory is the residue this asset must
  not carry. The `x-engine-registration-env` anchor stays, because services still
  reference it.

The release manifest written next to it is what lets a reader decide what the artifact
is without opening the documentation: tag, build commit, every image digest, the
migration head read out of the published image, and the attachment list with checksums.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: build repository -> the compose services that run it. `node-controller` is not in
#: `deploy/compose.yaml`; it ships as an image for the operator, not as a service here.
SERVICE_IMAGES = {
    "minio": "speechnexus-minio",
    "minio-init": "speechnexus-python",
    "api": "speechnexus-python",
    "gateway": "speechnexus-python",
    "binding-disable-worker": "speechnexus-python",
    "job-dispatch-worker": "speechnexus-python",
    "artifact-retention": "speechnexus-python",
    "admission-watch": "speechnexus-python",
    "lease-reconciler": "speechnexus-python",
    "migrate": "speechnexus-python",
    "engine-register": "speechnexus-python",
    "engine-readiness-renew": "speechnexus-python",
    "engine-readiness": "speechnexus-python",
    "workbench-migrate": "speechnexus-python",
    "workbench-api": "speechnexus-python",
    "asset-inspector": "speechnexus-asset-inspector",
    "admin": "speechnexus-web-admin",
    "workbench": "speechnexus-web-workbench",
}

#: Published images that no service in `deploy/compose.yaml` runs. They still belong in
#: `images.txt` and in the manifest; they are just not part of this compose file.
COMPOSE_EXTERNAL_IMAGES = {"speechnexus-node-controller"}

#: Services that must keep a public image until this repository builds them itself.
PUBLIC_IMAGES = {"postgres": "postgres:16.15-alpine"}

#: Scripts the compose file bind-mounts from its own directory, in the order they are
#: copied out. The mount target stays as the source compose declares it.
MOUNTED_SCRIPTS = (
    "init-object-storage.py",
    "job-startup-gate.py",
    "workbench-startup-gate.py",
)

#: `deploy/compose.yaml` and the attachment are run from the same directory; the
#: attachments are flat because `docker compose -f <file>` resolves `./x` against the
#: file's directory, and a reader who downloads `speechnexus-<tag>-*` gets them all.
ATTACHMENT_GLOB = "speechnexus-{tag}-*"

HEADER = """\
# Self-contained release compose asset — generated, do not edit.
#
# Derived from `deploy/compose.yaml` at commit {commit} (tag {tag}) by
# `deploy/release-assets.py` in `.github/workflows/release-images.yml`.
#
# Every SpeechNexus service runs a published digest image:
#   `docker compose --env-file compose.env -f {name} config --images` prints only
#   `ghcr.io/hrygo/<repository>@sha256:...` lines plus `{public}`.
#
# There is no `build:` key and no build context anywhere in this file, so it needs
# no source checkout. `{public}` keeps its upstream tag: it is a third-party image
# that pulls anonymously and is not built by this repository (the release manifest
# records it as `digest_pinned: false`).
#
# This file mounts three scripts that it does not contain. They are attachments of
# the same release ({scripts}); download every `{glob}` asset
# into one directory and run compose there, or those services will fail before they
# start.
#
"""


class GenerationError(Exception):
    """A property the asset must have cannot be produced from the inputs."""


@dataclass(frozen=True)
class Image:
    """One line of `speechnexus-<tag>-images.txt`."""

    repository: str
    reference: str
    digest: str
    pinned: str

    @classmethod
    def parse(cls, line: str) -> Image:
        match = re.fullmatch(
            r"(?P<prefix>[a-z0-9.\-]+(?::[0-9]+)?/[a-z0-9._/\-]+)"
            r":(?P<tag>[^:@/\s]+)@(?P<digest>sha256:[0-9a-f]{64})",
            line,
        )
        if match is None:
            raise GenerationError(
                f"images.txt line is not `registry/repo:tag@sha256:<64 hex>`: {line!r}"
            )
        return cls(
            repository=match["prefix"].rsplit("/", 1)[-1],
            reference=line,
            digest=match["digest"],
            pinned=f"{match['prefix']}@{match['digest']}",
        )


@dataclass
class Service:
    """One service block of `deploy/compose.yaml`, as line indices."""

    name: str
    start: int  # the `  <name>:` header
    end: int  # exclusive; the next non-blank line at indent <= 2

    @property
    def body(self) -> int:
        return self.start + 1


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def service_spans(lines: list[str]) -> tuple[int, list[Service]]:
    """Locate the `services:` section and one span per service, in source order."""
    try:
        head = lines.index("services:\n")
    except ValueError as error:  # pragma: no cover - a malformed input file
        raise GenerationError("deploy/compose.yaml has no top-level `services:`") from error
    header = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*$")
    spans: list[Service] = []
    index = head + 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and indent_of(line) == 0:
            break  # `volumes:`, `networks:` … the section is over
        match = header.match(line)
        if match is None:
            index += 1
            continue
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            if candidate.strip() and indent_of(candidate) <= 2:
                break
            end += 1
        spans.append(Service(name=match[1], start=index, end=end))
        index = end
    if not spans:
        raise GenerationError("deploy/compose.yaml declares no services")
    return head, spans


def drop_build_anchor(lines: list[str]) -> list[str]:
    """Remove `x-python-build: &python-build` and its children."""
    pattern = re.compile(r"^x-python-build:\s*&python-build\s*$")
    for index, line in enumerate(lines):
        if pattern.match(line):
            end = index + 1
            while end < len(lines):
                candidate = lines[end]
                if candidate.strip() and indent_of(candidate) == 0:
                    break
                end += 1
            kept = lines[:index] + lines[end:]
            # Every remaining reference must be a `build:` key that the service
            # rewrite removes; anything else would keep the anchor load-bearing.
            stray = [
                line
                for line in kept
                if "*python-build" in line and not re.match(r"^    build: \*python-build\s*$", line)
            ]
            if stray:
                raise GenerationError(
                    f"*python-build is referenced outside a `build:` key: {stray[0]!r}"
                )
            return kept
    return lines


def key_range(
    lines: list[str], start: int, end: int, key: str, depth: int = 4
) -> tuple[int, int] | None:
    """Return `(first, stop)` for one `key:` at `depth` spaces and its continuation."""
    pattern = re.compile(rf"^{' ' * depth}{re.escape(key)}:")
    for index in range(start, end):
        if pattern.match(lines[index]):
            stop = index + 1
            while stop < end:
                candidate = lines[stop]
                if candidate.strip() and indent_of(candidate) <= depth:
                    break
                stop += 1
            return index, stop
    return None


def attachment_name(tag: str, script: str) -> str:
    """The file name a mounted script is published under."""
    return f"speechnexus-{tag}-{script}"


def render_service(
    service: Service,
    lines: list[str],
    images: dict[str, Image],
    tag: str,
) -> tuple[list[str], list[str]]:
    """Pin one service's image, drop its build and repoint its script mounts.

    Returns the rewritten block and the scripts whose mount it repointed.
    """
    body = list(lines[service.body : service.end])
    build = key_range(body, 0, len(body), "build")
    image = key_range(body, 0, len(body), "image")
    mounted: list[str] = []

    if service.name in SERVICE_IMAGES:
        repository = SERVICE_IMAGES[service.name]
        pinned = images.get(repository)
        if pinned is None:
            raise GenerationError(
                f"service {service.name!r} runs {repository}, which images.txt does not name"
            )
        image_line = f"    image: {pinned.pinned}\n"
        if build is None and image is None:
            raise GenerationError(f"service {service.name!r} has neither `build:` nor `image:`")
        if image is not None:
            body[image[0]] = image_line
        else:
            # Put the pinned image where the build key stood, so the rendered file
            # keeps the source ordering instead of a stray blank line.
            body[build[0]] = image_line
            for offset in range(build[0] + 1, build[1]):
                body[offset] = ""
            build = None
    elif service.name in PUBLIC_IMAGES:
        expected = PUBLIC_IMAGES[service.name]
        if image is None:
            raise GenerationError(
                f"service {service.name!r} must keep the public image {expected!r}, "
                "but it has no `image:`"
            )
        actual = body[image[0]].split(":", 1)[1].strip()
        if actual != expected:
            raise GenerationError(
                f"service {service.name!r} names {actual!r}; the release asset expects "
                f"{expected!r}. Change PUBLIC_IMAGES and the release manifest together"
            )
    else:
        raise GenerationError(
            f"service {service.name!r} is neither pinned to a published digest nor a "
            f"declared public image; add it to SERVICE_IMAGES (and to the release image "
            f"list) so the attachment cannot keep a `build:` or a project-local name"
        )

    if build is not None:
        for offset in range(build[0], build[1]):
            body[offset] = ""

    for script in MOUNTED_SCRIPTS:
        mount = re.compile(rf"^      - \./(?P<file>{re.escape(script)}):(?P<target>/\S+):ro\s*$")
        for index, line in enumerate(body):
            match = mount.match(line)
            if match is None:
                continue
            body[index] = f"      - ./{attachment_name(tag, script)}:{match['target']}:ro\n"
            mounted.append(script)

    return [lines[service.start], *body], mounted


def repoint_checkout_paths(lines: list[str]) -> list[str]:
    """Rewrite commands inside preserved comments to the attachment layout.

    Comments survive the rewrite byte for byte, but they were written for a
    checkout: `--env-file deploy/compose.env` names a file that is not beside a
    downloaded attachment, so a reader who copies that line gets `no such file or
    directory`. Only comment lines are touched; nothing compose reads changes.
    """
    return [
        line.replace("deploy/compose.env", "compose.env") if line.lstrip().startswith("#") else line
        for line in lines
    ]


def verify(text: str, expected: dict[str, Image]) -> tuple[list[str], int]:
    """Assert the properties the attachment is published for; return (services, pinned)."""
    stripped = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    for forbidden in ("build:", "context:", "dockerfile:"):
        offenders = [line for line in stripped if re.match(r"^ {2,6}" + forbidden, line)]
        if offenders:
            raise GenerationError(f"generated compose still carries {forbidden}: {offenders[0]!r}")

    _, spans = service_spans([line + "\n" for line in text.splitlines()])
    services = [span.name for span in spans]
    pinned = 0
    for span in spans:
        block = [line + "\n" for line in text.splitlines()[span.body : span.end]]
        images = [line for line in block if line.startswith("    image: ")]
        if len(images) != 1:
            raise GenerationError(
                f"service {span.name!r} has {len(images)} image lines, expected exactly one"
            )
        reference = images[0].split("image: ", 1)[1].strip()
        if span.name in PUBLIC_IMAGES:
            if reference != PUBLIC_IMAGES[span.name]:
                raise GenerationError(
                    f"service {span.name!r} kept {reference!r}, expected "
                    f"{PUBLIC_IMAGES[span.name]!r}"
                )
            continue
        match = re.fullmatch(r"(.+)@sha256:[0-9a-f]{64}", reference)
        if match is None:
            raise GenerationError(f"service {span.name!r} is not pinned by digest: {reference!r}")
        if span.name not in SERVICE_IMAGES:
            raise GenerationError(f"service {span.name!r} has no declared image source")
        if expected[SERVICE_IMAGES[span.name]].pinned != reference:
            raise GenerationError(
                f"service {span.name!r} pins {reference!r}, not the digest from images.txt"
            )
        pinned += 1
    return services, pinned


def verify_mounts(text: str, tag: str, scripts: dict[str, str]) -> dict[str, str]:
    """Every mounted script must point at an attachment, and no mount may escape."""
    lines = text.splitlines(keepends=True)
    mounted: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^      - (?P<source>\./\S+?):(?P<target>/\S+):ro\s*$", line)
        if match is None:
            continue
        source = match["source"]
        if not source.startswith("./"):
            raise GenerationError(f"mount is not a relative path: {line!r}")
        name = source[2:]
        if name in scripts:
            raise GenerationError(
                f"mount still points at the checkout file {name!r}; it has to name the "
                "attachment so a download of the release assets is enough"
            )
        if name.startswith(f"speechnexus-{tag}-") and name.endswith(".py"):
            mounted[name] = match["target"]
    for script in scripts:
        if attachment_name(tag, script) not in mounted:
            raise GenerationError(f"no service mounts the attached copy of {script}")
    unknown = set(mounted) - {attachment_name(tag, script) for script in scripts}
    if unknown:
        raise GenerationError(f"generated compose mounts unexpected files: {sorted(unknown)}")
    return {target: name for name, target in mounted.items()}


def sha256(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--compose", default="deploy/compose.yaml", type=Path)
    parser.add_argument("--images", required=True, type=Path, help="speechnexus-<tag>-images.txt")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True, help="40-hex commit the images were built from")
    parser.add_argument("--migration-head", required=True, help="revision id read out of the image")
    parser.add_argument(
        "--migration-head-image", required=True, help="image@digest it was read from"
    )
    parser.add_argument("--out-dir", default=Path(), type=Path)
    parser.add_argument("--created-at", default=None, help="ISO-8601 UTC; defaults to now")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise GenerationError(f"--commit must be a 40-hex commit, got {args.commit!r}")
    if not re.fullmatch(r"[0-9]{4}_[A-Za-z0-9_]+", args.migration_head):
        raise GenerationError(
            f"--migration-head {args.migration_head!r} is not a migration revision id; "
            "read it out of the published image instead of writing it down"
        )
    created_at = args.created_at or datetime.now(UTC).replace(microsecond=0).isoformat()

    images: dict[str, Image] = {}
    for line in args.images.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        image = Image.parse(line.strip())
        if image.repository in images:
            raise GenerationError(f"images.txt names {image.repository} twice")
        images[image.repository] = image
    required = set(SERVICE_IMAGES.values())
    missing = sorted(required - set(images))
    extra = sorted(set(images) - required - COMPOSE_EXTERNAL_IMAGES)
    if missing:
        raise GenerationError(f"images.txt does not name {', '.join(missing)}")
    if extra:
        raise GenerationError(
            f"images.txt names {', '.join(extra)}, which neither a compose service nor "
            f"{', '.join(sorted(COMPOSE_EXTERNAL_IMAGES))} accounts for; update "
            "SERVICE_IMAGES and the workflow image list together"
        )

    source = args.compose.read_text(encoding="utf-8").splitlines(keepends=True)
    _, spans = service_spans(source)
    # Resolve the service decisions before anything else: a compose file that grew a
    # service is the first thing that has to fail, and it has to name the service.
    unknown = sorted({span.name for span in spans} - set(SERVICE_IMAGES) - set(PUBLIC_IMAGES))
    if unknown:
        raise GenerationError(
            f"services without an image decision: {', '.join(unknown)}; add each one to "
            "SERVICE_IMAGES (or PUBLIC_IMAGES) so no service keeps a `build:`"
        )

    compose_dir = args.compose.resolve().parent
    scripts: dict[str, str] = {}
    for script in MOUNTED_SCRIPTS:
        path = compose_dir / script
        if not path.is_file():
            raise GenerationError(f"deploy/compose.yaml mounts {script}, which is missing")
        scripts[script] = path.read_text(encoding="utf-8")
    with_builds = drop_build_anchor(source)
    _, spans = service_spans(with_builds)

    header = HEADER.format(
        commit=args.commit,
        tag=args.tag,
        name=f"speechnexus-{args.tag}-compose.yaml",
        public=PUBLIC_IMAGES["postgres"],
        scripts=", ".join(f"`{attachment_name(args.tag, script)}`" for script in MOUNTED_SCRIPTS),
        glob=ATTACHMENT_GLOB.format(tag=args.tag),
    )
    body: list[str] = []
    cursor = 0
    mounted: dict[str, str] = {}
    for span in spans:
        body.extend(with_builds[cursor : span.start])
        block, repointed = render_service(span, with_builds, images, args.tag)
        body.extend(block)
        for script in repointed:
            mounted[script] = attachment_name(args.tag, script)
        cursor = span.end
    body.extend(with_builds[cursor:])
    text = header + "".join(repoint_checkout_paths(body))

    services, pinned = verify(text, images)
    targets = verify_mounts(text, args.tag, scripts)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    compose_name = f"speechnexus-{args.tag}-compose.yaml"
    manifest_name = f"speechnexus-{args.tag}-release-manifest.json"
    sums_name = f"speechnexus-{args.tag}-SHA256SUMS"
    (args.out_dir / compose_name).write_text(text, encoding="utf-8")
    attached: list[dict[str, object]] = []
    for script in MOUNTED_SCRIPTS:
        name = attachment_name(args.tag, script)
        destination = args.out_dir / name
        shutil.copyfile(compose_dir / script, destination)
        digest, size = sha256(destination)
        attached.append(
            {
                "name": name,
                "sha256": digest,
                "bytes": size,
                "mounted_as": next(
                    target for target, mounted_name in targets.items() if mounted_name == name
                ),
            }
        )

    compose_digest, compose_bytes = sha256(args.out_dir / compose_name)
    images_digest, images_bytes = sha256(args.images)
    manifest = {
        "schema": 1,
        "kind": "speechnexus-release-manifest",
        "created_at": created_at,
        "how_to_read": (
            "`images` names every published digest and the compose services that run it. "
            "`migration_head.value` is the Alembic head inside the published image; if it "
            "differs from the head the documentation you are reading states, these images "
            "predate those migrations. `assets` is everything this release attaches; the "
            "`speechnexus-<tag>-*.py` files sit next to the compose file and are mounted "
            "from it by relative path."
        ),
        "release": {
            "tag": args.tag,
            "version": args.version,
            "commit": args.commit,
            "short_commit": args.commit[:12],
            "repository": "https://github.com/hrygo/SpeechNexus",
        },
        "migration_head": {
            "value": args.migration_head,
            "read_from": args.migration_head_image,
            "read_by": (
                "alembic ScriptDirectory.get_current_head() executed inside that image; "
                "not transcribed from the repository"
            ),
        },
        "images": [
            {
                "repository": repository,
                "reference": image.reference,
                "digest": image.digest,
                "services": sorted(
                    service
                    for service, source_repo in SERVICE_IMAGES.items()
                    if source_repo == repository
                ),
                "in_compose": any(
                    source_repo == repository for source_repo in SERVICE_IMAGES.values()
                ),
            }
            for repository, image in sorted(images.items())
        ],
        "third_party_images": [
            {
                "service": service,
                "image": image,
                "digest_pinned": False,
                "note": (
                    "upstream tag, anonymously pullable from Docker Hub; the release compose "
                    "pins no digest for it, so re-pulling it can deliver a newer patch"
                ),
            }
            for service, image in sorted(PUBLIC_IMAGES.items())
        ],
        "compose": {
            "asset": compose_name,
            "services": services,
            "digest_pinned_services": pinned,
            "build_sections": 0,
            "mounted_scripts": sorted(mounted),
        },
        "assets": [
            {"name": args.images.name, "sha256": images_digest, "bytes": images_bytes},
            {"name": compose_name, "sha256": compose_digest, "bytes": compose_bytes},
            *attached,
            {"name": manifest_name, "sha256": None, "note": "this file"},
            {
                "name": sums_name,
                "sha256": None,
                "note": "sha256 of every other asset; the file to verify the set against",
            },
        ],
    }
    (args.out_dir / manifest_name).write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )

    print(f"{compose_name}: {len(services)} services, {pinned} digest-pinned, 0 build sections")
    for service in sorted(SERVICE_IMAGES):
        print(f"  {service}: {images[SERVICE_IMAGES[service]].pinned}")
    print(f"  postgres: {PUBLIC_IMAGES['postgres']} (upstream tag, not digest-pinned)")
    for entry in attached:
        print(f"  {entry['name']} -> {entry['mounted_as']}")
    print(f"{manifest_name}: migration head {args.migration_head}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GenerationError as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
