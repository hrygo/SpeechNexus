"""The one rule for an execution-gateway origin that carries component credentials.

Every process that presents ``SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL``
to the execution gateway shares this rule: the Job dispatcher, the TTS settlement
worker and the binding-disable / TTS-recovery command lines. The credential is a
bearer token, so plain HTTP may only be used where it cannot cross a network
interface: loopback. A deployment whose gateway lives on another host or on a
Compose bridge must either use HTTPS or state, once and exactly, that it accepts
the credential travelling in clear text on that network:

``SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN=http://gateway:8010``

The opt-in is an origin, not a network classification: the configured gateway
origin must equal it byte for byte. There is no private-range, service-name or
prefix heuristic, so adding the key never widens the rule beyond the one origin
the operator wrote. The key itself is validated as a bare ``http`` origin, which
is what stops a trailing path from turning the comparison into a prefix match.
"""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

ALLOW_PLAINTEXT_ORIGIN_KEY = "SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PLAINTEXT_SCHEMES = frozenset({"http"})


def _unusable_origin(label: str) -> ValueError:
    return ValueError(f"{label} requires an origin without credentials or path")


def _unusable_opt_in() -> ValueError:
    return ValueError(
        f"{ALLOW_PLAINTEXT_ORIGIN_KEY} must be one exact http origin such as "
        "http://gateway:8010, without a path, query, fragment or credentials"
    )


def _has_whitespace(value: str) -> bool:
    return any(character.isspace() for character in value)


def gateway_origin(value: str, *, label: str) -> tuple[str, SplitResult]:
    """Return the canonical origin of ``value`` and its parsed form, or refuse it.

    Canonicalisation is limited to what the URL grammar itself states: an empty
    path and a single trailing slash denote the same origin, so they both render
    as ``scheme://host[:port]``. Host and port bytes are kept as written (the
    standard parser lowercases the scheme and host name); nothing else is
    normalised, in particular a default port is not dropped.
    """
    if not value or _has_whitespace(value):
        raise _unusable_origin(label)
    parsed = urlsplit(value)
    try:
        _ = parsed.port
    except ValueError:
        raise _unusable_origin(label) from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise _unusable_origin(label)
    return f"{parsed.scheme}://{parsed.netloc}", parsed


def plaintext_origin_opt_in(value: str | None) -> str | None:
    """Validate the explicit opt-in itself; an empty value means "not opted in".

    A value that is not exactly one ``http`` origin is refused even when the
    gateway uses HTTPS: an authorisation that could never match is a configuration
    defect, and accepting it would teach operators that the key is decorative.
    """
    if value is None or value == "":
        return None
    if _has_whitespace(value):
        raise _unusable_opt_in()
    parsed = urlsplit(value)
    try:
        _ = parsed.port
    except ValueError:
        raise _unusable_opt_in() from None
    if (
        parsed.scheme not in _PLAINTEXT_SCHEMES
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or f"{parsed.scheme}://{parsed.netloc}" != value
    ):
        raise _unusable_opt_in()
    return value


def require_safe_gateway_origin(
    value: str, *, allow_plaintext_origin: str | None, label: str
) -> str:
    """Apply the shared rule and return the canonical origin, or raise ``ValueError``.

    HTTPS is always accepted; plain HTTP is accepted on loopback or when the
    operator opted in to exactly this origin. Everything else is refused with a
    message that names the opt-in key and the origin it would have to allow.
    """
    origin, parsed = gateway_origin(value, label=label)
    allowance = plaintext_origin_opt_in(allow_plaintext_origin)
    if parsed.scheme == "https" or parsed.hostname in _LOOPBACK_HOSTS or origin == allowance:
        return origin
    raise ValueError(
        f"{label} plain HTTP is allowed only for loopback; set "
        f"{ALLOW_PLAINTEXT_ORIGIN_KEY}={origin} to accept this exact origin"
    )
