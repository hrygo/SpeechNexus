"""Canonical JSON and command digests must be reproducible byte for byte."""

from __future__ import annotations

import pytest

from speechnexus_gateway.domain.canonical import (
    JsonValue,
    UnsupportedCanonicalJsonError,
    canonical_json,
    command_digest,
)


def test_digest_is_sha256_of_canonical_json() -> None:
    digest = command_digest({"b": 1, "a": "x"})
    # `printf '%s' '{"a":"x","b":1}' | sha256sum` produces the same digest.
    assert digest == ("sha256:cdab067e9f3beb32d1252cfd63e492592fecbf591b0d08cadb24bb17f3864246")
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_key_order_and_whitespace_do_not_change_the_digest() -> None:
    assert canonical_json({"b": [1, 2], "a": {"z": None, "y": True}}) == (
        '{"a":{"y":true,"z":null},"b":[1,2]}'
    )


def test_keys_sort_by_utf16_code_units() -> None:
    # U+10000 is the surrogate pair D800 DC00, so it sorts before U+FFFF.
    assert canonical_json({"\uffff": 1, "\U00010000": 2}) == '{"\U00010000":2,"\uffff":1}'


def test_strings_escape_only_what_is_required() -> None:
    assert canonical_json({'a"\\\n\x01\u00e9': "v"}) == '{"a\\"\\\\\\n\\u0001\u00e9":"v"}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (-0.0, "0"),
        (3.0, "3"),
        (100.0, "100"),
        (0.7, "0.7"),
        (1e-5, "0.00001"),
        (1e-7, "1e-7"),
        (1e21, "1e+21"),
        (1.5e-300, "1.5e-300"),
        (-2.5, "-2.5"),
        (2**53 - 1, "9007199254740991"),
    ],
)
def test_numbers_follow_ecmascript_serialization(value: float, expected: str) -> None:
    assert canonical_json(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -(2**53),
        2**53,
        {1: "not a string key"},
        {b"k": "bytes key"},
    ],
)
def test_values_outside_the_subset_are_rejected(value: object) -> None:
    with pytest.raises(UnsupportedCanonicalJsonError):
        canonical_json(value)  # type: ignore[arg-type]


def test_nested_structures_and_booleans_round_trip() -> None:
    command: JsonValue = {
        "schema_version": "1",
        "effective_parameters": {"temperature": 0.7, "seed": None, "stream": False},
        "inputs": [{"artifact_id": "a-1", "offset_ms": 0}],
    }
    reordered: JsonValue = {
        "inputs": [{"offset_ms": 0, "artifact_id": "a-1"}],
        "effective_parameters": {"stream": False, "seed": None, "temperature": 0.7},
        "schema_version": "1",
    }
    assert canonical_json(command).startswith('{"effective_parameters":')
    assert command_digest(command) == command_digest(reordered)
