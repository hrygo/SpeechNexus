"""Safe YAML parsing for contract tests; prefer the optional LibYAML backend."""

from typing import Any

import yaml


def load_yaml(source: str) -> Any:
    # Both loaders use SafeConstructor. Never use Loader/CLoader, and keep a
    # pure-Python fallback for environments without the optional extension.
    # No document cache: callers may mutate results or edit schemas in a test.
    return yaml.load(source, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
