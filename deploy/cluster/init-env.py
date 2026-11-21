#!/usr/bin/env python3
"""Render the two-host runtime environment files from the reference example.

Outputs, both mode 0600 and never committed:

* ``.runtime/cluster/compose.env`` - the application-plane values, straight from
  ``deploy/compose.env.example`` with every ``change-me*`` literal replaced by one
  random token per distinct literal (so the DSNs inline and the
  ``*_ROLE_PASSWORD`` keys stay consistent).
* ``.runtime/cluster/compute.env`` - the compute-plane values, derived from the
  above. Database URLs are re-pointed at the bridge Pod's tunnel
  (``127.0.0.1:15440``), because the compute host has no route to the in-cluster
  Service name. ``deploy/cluster/compute/compose.yaml`` reads these names with
  Compose's own interpolation, so the committed file holds no credentials.

The two ``sn_disable_*`` roles exist in the reference runbook but not in the example
file. This script provisions their passwords and DSNs on first run and appends them
to an existing file, so the binding-disable worker has a real identity instead of an
empty value.

Re-running keeps existing secrets and only fills in what is missing.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "deploy/compose.env.example"
VERSIONS = ROOT / "deploy/cluster/versions.env"
TARGET = ROOT / ".runtime/cluster/compose.env"
COMPUTE = ROOT / ".runtime/cluster/compute.env"
LITERAL = re.compile(r"change-me[-a-z0-9]*")
TUNNEL_PG = ("127.0.0.1", 15440)

DISABLE_ROLES = {
    "SPEECHNEXUS_DISABLE_SCAN_ROLE_PASSWORD": (
        "SPEECHNEXUS_DISABLE_DATABASE_URL",
        "sn_disable_scan",
    ),
    "SPEECHNEXUS_DISABLE_SETTLE_ROLE_PASSWORD": (
        "SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL",
        "sn_disable_settle",
    ),
}


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def ensure_disable_roles(text: str) -> str:
    """Give the two binding-disable roles a password and a DSN, once."""
    values = parse(text)
    lines = text.splitlines()
    for password_key, (dsn_key, role) in DISABLE_ROLES.items():
        password = values.get(password_key)
        if not password:
            password = secrets.token_urlsafe(32)
            lines.append(f"{password_key}={password}")
            values[password_key] = password
        dsn = f"postgresql://{role}:{password}@postgres:5432/speechnexus"
        replaced = False
        for index, line in enumerate(lines):
            if line.startswith(f"{dsn_key}=") and not line.partition("=")[2].strip():
                lines[index] = f"{dsn_key}={dsn}"
                replaced = True
        if not replaced and dsn_key not in values:
            lines.append(f"{dsn_key}={dsn}")
        values[dsn_key] = dsn
    return "\n".join(lines) + "\n"


def to_tunnel(dsn: str) -> str:
    parts = urlsplit(dsn)
    host, port = TUNNEL_PG
    netloc = parts.netloc.replace(f"@{parts.hostname}", f"@{host}", 1)
    if parts.port:
        netloc = netloc.replace(f":{parts.port}", f":{port}", 1)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def preserved_engine_address() -> str | None:
    """Keep the operator-set engine address across renders.

    The address is the controller-created container's network address, which only
    the operator knows after a prepare. Rewriting the file must not drop it.
    """
    if not COMPUTE.exists():
        return None
    for line in COMPUTE.read_text().splitlines():
        if line.startswith("ENGINE_ADDRESS="):
            return line.partition("=")[2].strip() or None
    return None


def render_compute(values: dict[str, str], versions: dict[str, str]) -> str:
    model_dir = versions["ASR_MODEL_DIR"]
    silero = values.get("SPEECHNEXUS_SILERO_VAD_PATH") or str(
        Path(model_dir).parent / "silero-vad/silero_vad.onnx"
    )
    entries = {
        "PYTHON_IMAGE": versions["PYTHON_IMAGE"],
        "CONTROLLER_IMAGE": versions.get("CONTROLLER_IMAGE")
        or f"speechnexus-node-controller:{versions['REPO_SHA']}",
        "COMPUTE_GATEWAY_CONTROL_DATABASE_URL": to_tunnel(
            values["SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL"]
        ),
        "COMPUTE_GATEWAY_QUERY_DATABASE_URL": to_tunnel(
            values["SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL"]
        ),
        "COMPUTE_JOB_DISPATCH_DATABASE_URL": to_tunnel(
            values["SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL"]
        ),
        "COMPUTE_DISABLE_DATABASE_URL": to_tunnel(values["SPEECHNEXUS_DISABLE_DATABASE_URL"]),
        "COMPUTE_DISABLE_SETTLEMENT_DATABASE_URL": to_tunnel(
            values["SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL"]
        ),
        "COMPONENT_TOKEN": values["SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL"],
        "MINIO_ROOT_USER": values["MINIO_ROOT_USER"],
        "MINIO_ROOT_PASSWORD": values["MINIO_ROOT_PASSWORD"],
        "OBJECT_STORAGE_BUCKET": values.get("SPEECHNEXUS_OBJECT_STORAGE_BUCKET")
        or "speechnexus-results",
        "SILERO_VAD_PATH": silero,
        # Deliberately no default: the address is the controller-created container's,
        # only known after a prepare. An empty value makes Compose refuse to start the
        # shim with a message naming the variable, instead of guessing.
        "ENGINE_ADDRESS": preserved_engine_address() or "",
    }
    header = (
        "# Compute-plane values for deploy/cluster/compute/compose.yaml.\n"
        "# Generated by deploy/cluster/init-env.py; never committed.\n"
    )
    return header + "".join(f"{key}={value}\n" for key, value in entries.items())


def main() -> int:
    versions = parse(VERSIONS.read_text())
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    COMPUTE.parent.mkdir(parents=True, exist_ok=True)

    if TARGET.exists():
        text = ensure_disable_roles(TARGET.read_text())
        unchanged = text == TARGET.read_text()
        TARGET.write_text(text)
        os.chmod(TARGET, 0o600)
        print(f"{'kept' if unchanged else 'updated'} {TARGET}")
    else:
        text = EXAMPLE.read_text()
        replacements = {literal: secrets.token_urlsafe(32) for literal in set(LITERAL.findall(text))}
        for literal, token in replacements.items():
            text = text.replace(literal, token)
        leftover = LITERAL.findall(text)
        if leftover:
            raise SystemExit(f"unreplaced placeholders remain: {sorted(set(leftover))}")
        text = ensure_disable_roles(text)
        TARGET.write_text(text)
        os.chmod(TARGET, 0o600)
        print(f"wrote {TARGET} with {len(replacements)} generated secrets")

    values = parse(TARGET.read_text())
    missing = [
        key
        for key in (
            "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL",
            "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL",
            "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL",
            "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
            "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD",
        )
        if not values.get(key)
    ]
    if missing:
        raise SystemExit(f"compose.env is missing values for: {', '.join(missing)}")

    COMPUTE.write_text(render_compute(values, versions))
    os.chmod(COMPUTE, 0o600)
    print(f"wrote {COMPUTE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())