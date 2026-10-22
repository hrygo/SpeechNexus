from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema.validators import validator_for
from openapi_spec_validator import validate_spec


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    if not isinstance(document, Mapping):
        raise ValueError("document root must be a mapping")
    return dict(document)


def validate_contract(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    if path.suffix in {".yaml", ".yml"}:
        validate_spec(load_yaml(path))
        print(f"ok   OpenAPI {relative}")
        return

    document = load_yaml(path)
    validator_for(document).check_schema(document)
    print(f"ok   JSON Schema {relative}")


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    contracts = root / "contracts"
    paths = sorted(
        path
        for path in contracts.glob("**/*")
        if path.is_file()
        and (
            path.name.endswith(("openapi.yaml", "openapi.yml"))
            or path.name.endswith(".schema.json")
        )
    )
    if not paths:
        print("skip contracts (none yet)")
        return
    for path in paths:
        validate_contract(path, root)
    print(f"contract validation: passed ({len(paths)} files)")


if __name__ == "__main__":
    main()
