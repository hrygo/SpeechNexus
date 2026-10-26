"""Keep parameter identities bounded without changing test inputs."""

import hashlib


def pytest_make_parametrize_id(val: object, argname: str) -> str | None:
    if not isinstance(val, str | bytes) or len(val) <= 256:
        return None
    # Hash before pytest expands Unicode escapes; preserve every original value.
    payload = val.encode("utf-8", errors="surrogatepass") if isinstance(val, str) else val
    digest = hashlib.sha256(payload).hexdigest()
    return f"{argname}-{type(val).__name__}-{len(val)}-sha256-{digest}"
