#!/usr/bin/env python3
"""Render the web front ends' upstream addresses from the live Services.

ClusterIPs are assigned by the cluster, not chosen by us, so they must not live in
the repository. This reads them from the running Services and writes a ConfigMap the
two front-end Deployments consume as `WEB_SPEECH_UPSTREAM` and
`WEB_WORKBENCH_UPSTREAM`.

Why the address and not the Service DNS name: this cluster answers `*.svc` with
Docker Desktop's synthetic addresses (`2001:2::…`), which nginx cannot connect to,
while the IPv4 ClusterIP works. The tradeoff is that the value must be re-rendered
whenever the Services are recreated — which is exactly why it is generated here
instead of committed.

Re-run after any change that recreates `api` or `workbench-api`:

    python3 deploy/cluster/app-plane/render-web-upstreams.py
"""

from __future__ import annotations

import json
import subprocess
import sys

NAMESPACE = "speechnexus"
CONFIGMAP = "web-upstreams"
SERVICES = {
    "WEB_SPEECH_UPSTREAM": ("api", 8000),
    "WEB_WORKBENCH_UPSTREAM": ("workbench-api", 8000),
}


def cluster_ip(service: str) -> str:
    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "svc", service, "-o", "json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"cannot read Service {service}: {result.stderr.strip()}")
    address = json.loads(result.stdout)["spec"].get("clusterIP", "")
    if not address or address == "None":
        raise SystemExit(f"Service {service} has no ClusterIP")
    return address


def dns_resolver() -> str:
    """The cluster DNS address the front ends' nginx config refers to."""
    result = subprocess.run(
        [
            "kubectl", "-n", "kube-system", "get", "svc", "kube-dns",
            "-o", "jsonpath={.spec.clusterIP}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    address = result.stdout.strip()
    if result.returncode != 0 or not address:
        raise SystemExit(f"cannot read the cluster DNS address: {result.stderr.strip()}")
    return address


def main() -> int:
    data = {
        key: f"http://{cluster_ip(service)}:{port}"
        for key, (service, port) in SERVICES.items()
    }
    data["WEB_DNS_RESOLVER"] = dns_resolver()
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": CONFIGMAP, "namespace": NAMESPACE},
        "data": data,
    }
    applied = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(manifest),
        text=True,
        capture_output=True,
        check=False,
    )
    if applied.returncode != 0:
        sys.stderr.write(applied.stderr)
        return 1
    for key, value in data.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())