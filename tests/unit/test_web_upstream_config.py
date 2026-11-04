"""Security regression tests for the nginx entrypoint's upstream configuration."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/web/19-configure-upstreams.envsh"
VALID = {
    "WEB_SPEECH_UPSTREAM": "https://speech.example:443",
    "WEB_WORKBENCH_UPSTREAM": "http://workbench:8000",
    "WEB_DNS_RESOLVER": "127.0.0.11",
}


def configure(changes: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sh",
            "-c",
            '. "$1" && printf "%s\\n%s\\n" "$WEB_SPEECH_HOST" "$WEB_WORKBENCH_HOST"',
            "sh",
            str(SCRIPT),
        ],
        env={**os.environ, **VALID, **changes},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_valid_upstreams_export_hosts() -> None:
    result = configure({})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["speech.example", "workbench"]


@pytest.mark.parametrize("variable", list(VALID))
@pytest.mark.parametrize("suffix", ["\n; proxy_ssl_verify off; #", "\n"])
def test_multiline_and_trailing_newline_are_rejected(variable: str, suffix: str) -> None:
    result = configure({variable: VALID[variable] + suffix})
    assert result.returncode != 0
    assert f"invalid {variable}" in result.stderr


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("WEB_SPEECH_UPSTREAM", "https://speech.example:65536"),
        ("WEB_WORKBENCH_UPSTREAM", "http://-host:8000"),
        ("WEB_DNS_RESOLVER", "127.0.0.256"),
    ],
)
def test_invalid_configuration_is_rejected(variable: str, value: str) -> None:
    result = configure({variable: value})
    assert result.returncode != 0
    assert f"invalid {variable}" in result.stderr
