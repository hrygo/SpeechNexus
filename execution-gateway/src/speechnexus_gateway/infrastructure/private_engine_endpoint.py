"""Fixed private deployment-network engine endpoint shared by speech adapters."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


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
            or _IDENTIFIER.fullmatch(self.binding_revision_id) is None
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
