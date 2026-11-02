#!/usr/bin/env python3
"""Exercise the Controller image on an isolated Linux Docker CI runner.

The TLS server and certificates are synthetic and exist only for this run. The
normal and negative builds use fresh BuildKit and uv caches; a failed negative
case only counts when its output identifies the expected failure.
"""

from __future__ import annotations

import base64
import functools
import http.server
import io
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = "deploy/images/node-controller.Dockerfile"
CONSTRAINTS = "deploy/images/node-controller-build-constraints.txt"
SYNTHETIC_WHEEL = "local_ca_probe-1.0-py3-none-any.whl"
HATCHLING_HASH = "415290b84f464d2884303fca8974d38e7b85579069fab02a15db0828fbb1446d"


def run(command: list[str], *, expect: str | None = None, timeout: int = 900) -> str:
    env = dict(os.environ, DOCKER_BUILDKIT="1")
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, env=env)
    output = result.stdout + result.stderr
    if expect is None:
        if result.returncode:
            raise RuntimeError(f"command failed ({result.returncode}): {command[0]}\n{output}")
    elif result.returncode == 0 or expect.lower() not in output.lower():
        raise RuntimeError(f"expected failure {expect!r}: {command[0]}\n{output}")
    return output


def build(context: Path, *arguments: str, expect: str | None = None) -> str:
    return run(
        [
            "docker",
            "build",
            "--no-cache",
            "--progress=plain",
            "--file",
            str(context / DOCKERFILE),
            *arguments,
            str(context),
        ],
        expect=expect,
    )


def assert_tls_failure(output: str, url: str, *, hostname: bool = False) -> None:
    # Keep BuildKit steps separate and bind the rejection to uv's target request
    # error, before any later request. An unrelated registry error cannot satisfy it.
    steps: dict[str, list[str]] = {}
    for line in output.splitlines():
        match = re.match(r"^#(\d+)\s+(?:\d+(?:\.\d+)?\s+)?(.*)$", line)
        if match:
            steps.setdefault(match.group(1), []).append(match.group(2))
    request = r"error sending request for url\s*\(\s*" + re.escape(url) + r"\s*\)"
    category = (
        ("certificate not valid for name", "notvalidforname") if hostname else ("unknownissuer",)
    )
    for lines in steps.values():
        content = re.sub(r"[│├╰─▶]", "", "\n".join(lines))
        for match in re.finditer(request, content, re.IGNORECASE):
            cause = content[match.end() :]
            next_request = re.search(r"error sending request for url", cause, re.IGNORECASE)
            if next_request:
                cause = cause[: next_request.start()]
            lower = cause.lower()
            if "invalid peer certificate" in lower and any(item in lower for item in category):
                return
    raise RuntimeError(f"No matching TLS rejection for target request {url}\n{output}")


def negative_context(destination: Path) -> None:
    # Copy exactly the metadata and sources used by the Dockerfile, not user data.
    files = [
        "pyproject.toml",
        "uv.lock",
        "backend/README.md",
        "backend/pyproject.toml",
        "execution-gateway/README.md",
        "execution-gateway/pyproject.toml",
        "packages/contracts/pyproject.toml",
        "packages/media/README.md",
        "packages/media/pyproject.toml",
        "node-controller/README.md",
        "node-controller/pyproject.toml",
        DOCKERFILE,
        CONSTRAINTS,
        "deploy/images/node-controller-with-ca.sh",
    ]
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    shutil.copytree(ROOT / "node-controller/src", destination / "node-controller/src")


def make_ca(directory: Path, prefix: str) -> tuple[Path, Path]:
    key, certificate = directory / f"{prefix}.key", directory / f"{prefix}.crt"
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={prefix}",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ]
    )
    return key, certificate


def make_server_certificate(directory: Path, ca_key: Path, ca_cert: Path) -> tuple[Path, Path]:
    key, request, cert = (
        directory / "server.key",
        directory / "server.csr",
        directory / "server.crt",
    )
    extensions = directory / "server.ext"
    extensions.write_text("subjectAltName=DNS:host.docker.internal\nextendedKeyUsage=serverAuth\n")
    run(
        [
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=host.docker.internal",
            "-keyout",
            str(key),
            "-out",
            str(request),
        ]
    )
    run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            str(extensions),
            "-out",
            str(cert),
        ]
    )
    return key, cert


def make_wheel(path: Path) -> None:
    metadata = "local_ca_probe-1.0.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("local_ca_probe/__init__.py", "")
        archive.writestr(
            f"{metadata}/METADATA", "Metadata-Version: 2.1\nName: local-ca-probe\nVersion: 1.0\n"
        )
        archive.writestr(
            f"{metadata}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: local-ci\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{metadata}/RECORD", "")


def assert_image_omits(image: str, needle: bytes, directory: Path) -> None:
    """Examine every image layer and metadata for the synthetic secret bytes."""
    saved = directory / f"{image.replace(':', '-')}.tar"
    run(["docker", "save", "--output", str(saved), image])
    with tarfile.open(saved, "r") as outer:
        manifest_file = outer.extractfile("manifest.json")
        if manifest_file is None:
            raise RuntimeError("image archive has no manifest")
        import json

        manifest = json.load(manifest_file)
        layers = {item for entry in manifest for item in entry["Layers"]}
        seen_layers: set[str] = set()
        needles = (needle, base64.b64encode(needle), b"".join(needle.splitlines()[1:-1]))
        for member in outer:
            if not member.isfile():
                continue
            stream = outer.extractfile(member)
            if stream is None:
                continue
            if member.name in layers:
                seen_layers.add(member.name)
                with tarfile.open(fileobj=stream, mode="r:*") as layer:
                    for entry in layer:
                        if entry.isfile():
                            file_stream = layer.extractfile(entry)
                            if file_stream is not None and _contains(file_stream, needles):
                                raise RuntimeError(
                                    f"synthetic CA appeared in layer file {entry.name}"
                                )
            elif member.size <= 8_000_000 and _contains(stream, needles):
                raise RuntimeError(f"synthetic CA appeared in image metadata {member.name}")
        if seen_layers != layers:
            raise RuntimeError("failed to inspect every image layer")


def _contains(stream: io.BufferedIOBase, needles: tuple[bytes, ...]) -> bool:
    tail = b""
    overlap = max(map(len, needles)) - 1
    while chunk := stream.read(65536):
        data = tail + chunk
        if any(needle in data for needle in needles):
            return True
        tail = data[-overlap:]
    return False


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


def main() -> None:
    image, probe_image = (
        f"speechnexus-controller-ci:{os.getpid()}",
        f"speechnexus-ca-ci:{os.getpid()}",
    )
    with tempfile.TemporaryDirectory(prefix="speechnexus-controller-ci-") as temporary:
        directory = Path(temporary)
        print(
            "Docker versions:",
            run(
                ["docker", "version", "--format", "{{.Client.Version}} {{.Server.Version}}"]
            ).strip(),
            flush=True,
        )
        ca_key, ca_cert = make_ca(directory, "synthetic-root")
        _, wrong_cert = make_ca(directory, "wrong-root")
        # The builder still needs public roots for package downloads. Add the
        # synthetic CA to the runner's public root bundle without persisting it.
        runtime_ca = directory / "runtime-ca-bundle.pem"
        runtime_ca.write_bytes(
            Path("/etc/ssl/certs/ca-certificates.crt").read_bytes() + b"\n" + ca_cert.read_bytes()
        )
        build(
            ROOT,
            "--target",
            "runtime",
            "--tag",
            image,
            "--secret",
            f"id=proxy_ca,src={runtime_ca}",
        )
        print(
            "Runtime image ID:",
            run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip(),
            flush=True,
        )
        run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/app/.venv/bin/python",
                image,
                "-c",
                "import os; from importlib.metadata import distribution, distributions; "
                "assert os.getuid() == 10001; "
                "assert os.access('/var/lib/speechnexus/controller', os.W_OK); "
                "assert not os.access('/app/.venv', os.W_OK); "
                "names = {d.metadata['Name'].lower().replace('_', '-') for d in distributions()}; "
                "assert not names.intersection({'av', 'speechnexus-backend', "
                "'speechnexus-execution-gateway', 'speechnexus-contracts', 'speechnexus-media'}); "
                "entries = distribution('speechnexus-node-controller').entry_points; "
                "assert callable(next(e for e in entries "
                "if e.name == 'speechnexus-node-controller').load()); "
                "import speechnexus_node_controller.app",
            ]
        )
        synthetic_token = "synthetic-controller-token"
        missing_node = run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint=/usr/bin/timeout",
                "--env",
                f"SPEECHNEXUS_CONTROLLER_COMPONENT_TOKEN={synthetic_token}",
                image,
                "10s",
                "/app/.venv/bin/speechnexus-node-controller",
            ],
            expect="production requires an explicit non-default node_id",
        )
        if synthetic_token in missing_node:
            raise RuntimeError("startup validation echoed the synthetic credential")
        print(
            "Entrypoint, dependency isolation, UID, paths and missing node ID verified", flush=True
        )
        for label in ("wrong-hash", "missing-transitive"):
            context = directory / label
            context.mkdir()
            negative_context(context)
            path = context / CONSTRAINTS
            contents = path.read_text()
            if label == "wrong-hash":
                assert HATCHLING_HASH in contents
                contents = contents.replace(HATCHLING_HASH, "0" + HATCHLING_HASH[1:])
                expected = "Hash mismatch"
            else:
                assert any(line.startswith("pluggy==") for line in contents.splitlines())
                contents = (
                    "\n".join(
                        line for line in contents.splitlines() if not line.startswith("pluggy==")
                    )
                    + "\n"
                )
                expected = "no trusted hash was provided"
            path.write_text(contents)
            build(context, "--target", "builder", expect=expected)
            print(f"Build correctly rejected {label}", flush=True)

        server_key, server_cert = make_server_certificate(directory, ca_key, ca_cert)
        served = directory / "served"
        served.mkdir()
        make_wheel(served / SYNTHETIC_WHEEL)
        handler = functools.partial(QuietHandler, directory=str(served))
        server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(server_cert, server_key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            common = [
                "--target",
                "ca-probe",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--add-host",
                "wrong-host.internal:host-gateway",
            ]
            url = f"https://host.docker.internal:{port}/{SYNTHETIC_WHEEL}"
            build(
                ROOT,
                *common,
                "--tag",
                probe_image,
                "--build-arg",
                f"PROBE_WHEEL_URL={url}",
                "--secret",
                f"id=proxy_ca,src={ca_cert}",
            )
            print("Synthetic HTTPS: correct CA accepted", flush=True)
            failure = build(
                ROOT,
                *common,
                "--build-arg",
                f"PROBE_WHEEL_URL={url}",
                expect="error sending request for url",
            )
            assert_tls_failure(failure, url)
            print("Synthetic HTTPS: missing CA rejected", flush=True)
            failure = build(
                ROOT,
                *common,
                "--build-arg",
                f"PROBE_WHEEL_URL={url}",
                "--secret",
                f"id=proxy_ca,src={wrong_cert}",
                expect="error sending request for url",
            )
            assert_tls_failure(failure, url)
            print("Synthetic HTTPS: wrong CA rejected", flush=True)
            wrong_url = f"https://wrong-host.internal:{port}/{SYNTHETIC_WHEEL}"
            failure = build(
                ROOT,
                *common,
                "--build-arg",
                f"PROBE_WHEEL_URL={wrong_url}",
                "--secret",
                f"id=proxy_ca,src={ca_cert}",
                expect="error sending request for url",
            )
            assert_tls_failure(failure, wrong_url, hostname=True)
            print("Synthetic HTTPS: wrong hostname rejected", flush=True)
            assert_image_omits(probe_image, ca_cert.read_bytes(), directory)
            assert_image_omits(image, ca_cert.read_bytes(), directory)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        print("Controller image, hash rejection, synthetic CA and layer checks passed")


if __name__ == "__main__":
    main()
