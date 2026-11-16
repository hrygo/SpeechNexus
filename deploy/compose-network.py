"""Derive the Compose subnet and the MinIO static address from one CIDR.

`deploy/compose.yaml` interpolates two host-side variables with a fixed default:

    SPEECHNEXUS_COMPOSE_SUBNET     default 172.28.0.0/24   (networks.default.ipam)
    SPEECHNEXUS_MINIO_STATIC_IP    default 172.28.0.10     (minio ipv4_address)

The second value must stay the 10th host of the first. The file-speech S3 adapter
rejects DNS names, so MinIO keeps a literal address and every private gateway JSON
pins the same literal in `objects.endpoint`. Picking a different subnet by hand
therefore means recomputing that address and editing four JSON copies, which is why
the one supported entry point is a subnet and this script does the rest:

    python3 deploy/compose-network.py 172.30.0.0/24

prints the two assignments for `deploy/compose.env` plus the exact
`objects.endpoint` URL the copied private gateway files must carry. Redirecting the
output appends only the assignments and comments:

    python3 deploy/compose-network.py 172.30.0.0/24 >> deploy/compose.env

Without an argument the script prints the current default, which is also what an
unset variable resolves to. That default is duplicated in `deploy/compose.yaml`
because Compose cannot run this arithmetic; `tests/ci/test_compose_network_addresses.py`
keeps the two in step.

Exit codes: 0 the values were printed; 2 the CIDR cannot serve this deployment.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from dataclasses import dataclass

# The value `deploy/compose.yaml` falls back to when neither variable is set.
DEFAULT_SUBNET = "172.28.0.0/24"

# MinIO's position inside the Compose network. The default 172.28.0.10 is this
# offset inside DEFAULT_SUBNET, and the private gateway examples pin that address.
MINIO_HOST_OFFSET = 10

MINIO_S3_PORT = 9000

PRIVATE_RANGES = tuple(
    ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class SubnetError(ValueError):
    """The CIDR cannot be used for this deployment; the message says why."""


@dataclass(frozen=True)
class ComposeNetwork:
    """The subnet, the MinIO address and the object endpoint derived from it."""

    subnet: ipaddress.IPv4Network
    minio_ip: ipaddress.IPv4Address

    @property
    def endpoint(self) -> str:
        return f"http://{self.minio_ip}:{MINIO_S3_PORT}"

    def env_assignments(self) -> tuple[str, str]:
        return (
            f"SPEECHNEXUS_COMPOSE_SUBNET={self.subnet}",
            f"SPEECHNEXUS_MINIO_STATIC_IP={self.minio_ip}",
        )


def derive(text: str) -> ComposeNetwork:
    """Return the Compose network for ``text``, or raise :class:`SubnetError`."""
    try:
        network = ipaddress.ip_network(text.strip(), strict=True)
    except ValueError as error:
        raise SubnetError(
            f"{text!r} is not an IPv4 network in CIDR form with the host bits clear: {error}"
        ) from error
    if not isinstance(network, ipaddress.IPv4Network):
        raise SubnetError(
            f"{text!r} is not IPv4; Compose ipv4_address and the S3 adapter both need IPv4"
        )
    if not any(network.subnet_of(private) for private in PRIVATE_RANGES):
        raise SubnetError(
            f"{text!r} is not a private (RFC 1918) IPv4 network; the file-speech S3 adapter "
            "requires a private endpoint and a public range would collide with real routes"
        )
    minio_ip = ipaddress.IPv4Address(int(network.network_address) + MINIO_HOST_OFFSET)
    if minio_ip not in network:
        raise SubnetError(
            f"{text!r} has no {MINIO_HOST_OFFSET}th host for MinIO; use a /28 or wider network"
        )
    if minio_ip in (network.network_address, network.broadcast_address):
        raise SubnetError(f"{minio_ip} is the network or broadcast address of {text!r}, not a host")
    return ComposeNetwork(subnet=network, minio_ip=minio_ip)


def render(network: ComposeNetwork) -> str:
    """Return the lines to write into ``deploy/compose.env``."""
    subnet_line, minio_line = network.env_assignments()
    return "\n".join(
        (
            subnet_line,
            minio_line,
            "# objects.endpoint for the copies of deploy/private/gateway-file-*.json:",
            f"# {network.endpoint}",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the Compose subnet and MinIO address derived from one CIDR.",
    )
    parser.add_argument(
        "subnet",
        nargs="?",
        default=DEFAULT_SUBNET,
        help=f"private IPv4 network in CIDR form (default: {DEFAULT_SUBNET})",
    )
    arguments = parser.parse_args(argv)
    try:
        network = derive(arguments.subnet)
    except SubnetError as error:
        print(f"compose-network: {error}", file=sys.stderr)
        return 2
    print(render(network))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
