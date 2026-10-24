"""Frozen configuration semantics, bounded files and safe activation failures."""

import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from speechnexus_gateway.app import _build_control, create_app
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.infrastructure.recovery_configuration import load_recovery_configuration
from speechnexus_gateway.settings import Settings
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.support.gateway_recovery import recovery_document
from tests.support.runtime_controller_tls import TLSFiles
from tests.support.runtime_controller_tls import controller_tls as controller_tls
from tests.unit.gateway.test_barrier_http import settings_for

TOKEN = "synthetic-independent-node-token"


def configured(tmp_path: Path, path: Path) -> Settings:
    return settings_for(
        tmp_path,
        recovery_config_file=path,
        recovery_node_component_token=SecretStr(TOKEN),
    )


def test_valid_symlink_configuration_activates_without_opening_a_database(
    tmp_path: Path, controller_tls: TLSFiles
) -> None:
    path = tmp_path / "private.json"
    value = recovery_document(ca=controller_tls.certificate)
    # JSON Schema integers include integral floats; consumers normalize only after validation.
    value["batch_size"], value["node"]["port"] = 3.0, 8443.0
    path.write_text(json.dumps(value))
    link = tmp_path / "config-link"
    link.symlink_to(path)
    result = load_recovery_configuration(configured(tmp_path, link))
    assert result is not None
    assert result.batch_size == 3 and type(result.batch_size) is int
    assert result.interval_seconds == 5 and result.pass_budget_seconds == 10
    assert str(path) not in repr(result) and TOKEN not in repr(result)


def test_omitted_configuration_preserves_the_existing_control_behavior(tmp_path: Path) -> None:
    assert load_recovery_configuration(settings_for(tmp_path)) is None


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "empty",
        "oversized",
        "directory",
        "fifo",
        "duplicate",
        "nonfinite",
        "invalid-ca",
        "large-ca",
        "fifo-ca",
        "ambiguous-bindings",
        "invalid-json",
        "invalid-credential",
        "extra-secret",
        "wrong-type",
    ],
)
def test_invalid_configuration_fails_before_ownership_or_database_activation(
    tmp_path: Path, controller_tls: TLSFiles, bad: str
) -> None:
    path = tmp_path / "private-sensitive-config.json"
    value = recovery_document(ca=controller_tls.certificate)
    if bad == "ambiguous-bindings":
        value["bindings"].append(value["bindings"][0] | {"manifest_id": "other-manifest"})
    elif bad in {"invalid-ca", "large-ca", "fifo-ca"}:
        ca = tmp_path / "private-ca"
        if bad == "fifo-ca":
            os.mkfifo(ca)
        else:
            ca.write_text("invalid" if bad == "invalid-ca" else "x" * 1048577)
        value["node"]["tls_ca_file"] = str(ca)
    elif bad == "extra-secret":
        value["token"] = TOKEN
    elif bad == "wrong-type":
        value["batch_size"] = "50"
    path.write_text(json.dumps(value))
    if bad == "duplicate":
        path.write_text('{"schema_version":"1","schema_version":"2"}')
    elif bad == "nonfinite":
        path.write_text('{"budget":NaN}')
    elif bad == "invalid-json":
        path.write_text("invalid " + TOKEN)
    elif bad == "empty":
        path.write_bytes(b"")
    elif bad == "oversized":
        path.write_bytes(b" " * 524289)
    elif bad in {"missing", "directory", "fifo"}:
        path.unlink()
        if bad == "directory":
            path.mkdir()
        elif bad == "fifo":
            os.mkfifo(path)
    settings = configured(tmp_path, path)
    if bad == "invalid-credential":
        settings = settings.model_copy(update={"recovery_node_component_token": SecretStr("short")})
    with pytest.raises(ValueError) as caught:
        _build_control(settings)
    assert str(caught.value) == "invalid gateway recovery configuration"
    assert caught.value.__suppress_context__
    assert not list(tmp_path.glob("*.lock"))
    assert TOKEN not in str(caught.value) and str(path) not in str(caught.value)


@pytest.mark.parametrize("missing", ["recovery_config_file", "recovery_node_component_token"])
def test_recovery_settings_must_be_paired(tmp_path: Path, missing: str) -> None:
    settings = configured(tmp_path, tmp_path / "config.json")
    values = settings.model_dump(exclude={missing})
    with pytest.raises(ValidationError, match="requires both"):
        Settings(**values)


def test_recovery_requires_control_absolute_paths_and_independent_credentials(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="absolute configuration"):
        configured(tmp_path, Path("relative.json"))
    with pytest.raises(ValidationError, match="independent node credential"):
        settings_for(
            tmp_path,
            recovery_config_file=tmp_path / "config.json",
            recovery_node_component_token=SecretStr("synthetic-gateway-component-token"),
        )
    with pytest.raises(ValidationError, match="requires execution control"):
        Settings(
            control_database_url=SecretStr("postgresql://unused"),
            recovery_config_file=tmp_path / "config.json",
            recovery_node_component_token=SecretStr(TOKEN),
        )


def test_injected_control_cannot_silently_skip_configured_recovery(tmp_path: Path) -> None:
    ledger, ingress = InMemorySubmissionLedger(), fake_ingress()
    with pytest.raises(ValueError, match="default control lifespan"):
        create_app(
            settings=configured(tmp_path, tmp_path / "config.json"),
            barrier_control=GatewayBarrierService(ingress=ingress, ledger=ledger),
            execution_query=ExecutionQueryService(ingress=ingress, ledger=ledger),
        )
