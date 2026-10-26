"""Fixed private deployment-network engine endpoint shared by speech adapters."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CONFIG_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_CONTROLLED_FIELDS = {"protocol", "target", "address", "port", "served_model", "bearer_token"}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _nonfinite(_: str) -> Any:
    raise ValueError


@dataclass(frozen=True, slots=True)
class ControlledEndpointDescriptor:
    """One private descriptor; the caller still validates its own target shape."""

    address: str = field(repr=False)
    port: int
    served_model: str = field(repr=False)
    bearer_token: str = field(repr=False)
    target: dict[str, Any]


def load_controlled_endpoint_descriptor(path: Path) -> ControlledEndpointDescriptor:
    """Read one private, bounded deployment descriptor owned by the service account."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError
            payload = source.read(16385)
        if len(payload) > 16384:
            raise ValueError
        document = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
        if (
            not isinstance(document, dict)
            or set(document) != _CONTROLLED_FIELDS
            or document["protocol"] != "vllm_api_key_v1"
            or not isinstance(document["target"], dict)
        ):
            raise ValueError
        return ControlledEndpointDescriptor(
            address=document["address"],
            port=document["port"],
            served_model=document["served_model"],
            bearer_token=document["bearer_token"],
            target=document["target"],
        )
    except OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError:
        raise ValueError("invalid private controlled engine deployment descriptor") from None


@dataclass(frozen=True, slots=True)
class PrivateEngineEndpoint:
    """A configured private IP literal, port and frozen served-model identity.

    The address is never derived from a request, environment proxy or redirect,
    so an adapter cannot be pointed at an arbitrary host by caller input.
    """

    address: str = field(repr=False)
    port: int
    served_model: str = field(repr=False)
    binding_revision_id: str
    bearer_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.address) is not str:
            raise ValueError("engine endpoint must use a configured private IP literal")
        try:
            address = ipaddress.ip_address(self.address)
        except ValueError:
            raise ValueError("engine endpoint must use a configured private IP literal") from None
        if not (
            address.is_loopback
            or (
                address.is_private
                and not address.is_unspecified
                and not address.is_multicast
                and not address.is_link_local
                and not address.is_reserved
            )
        ):
            raise ValueError("engine endpoint must remain on the configured deployment network")
        if (
            type(self.port) is not int
            or not 1 <= self.port <= 65535
            or type(self.served_model) is not str
            or _IDENTIFIER.fullmatch(self.served_model) is None
            or type(self.binding_revision_id) is not str
            or _CONFIG_REVISION.fullmatch(self.binding_revision_id) is None
            or (
                self.bearer_token is not None
                and (
                    type(self.bearer_token) is not str
                    or not 1 <= len(self.bearer_token) <= 4096
                    or any(not 33 <= ord(char) <= 126 for char in self.bearer_token)
                )
            )
        ):
            raise ValueError("invalid configured engine endpoint")

    @property
    def base_url(self) -> str:
        address = f"[{self.address}]" if ":" in self.address else self.address
        return f"http://{address}:{self.port}"
