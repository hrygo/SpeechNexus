#!/usr/bin/env python3
"""Report which synchronous-TTS private inputs an operator still has to replace.

The reference deployment assembles synchronous TTS from files that live outside
git: the private recipe (`SPEECHNEXUS_SPEECH_RECIPE_FILE`), the gateway's private
`file_tts_v2` config and its controlled endpoint descriptor. Every one of them is
schema-valid while still carrying the committed example's synthetic identities, so
"the loaders accept it" says nothing about "the values were replaced".

This tool answers exactly one question: **is a field still the committed example's
value, and do the files agree with each other?** It does not answer "is this
deployment able to accept a request". Values with no local oracle — budgets, the
real engine address, whether a policy revision equals the voice qualification —
are reported as undecidable instead of being folded into a pass, and there is no
"ready" verdict to mistake for one.

Exit codes: 0 = no field still carries the example's value and no internal
inconsistency; 1 = placeholder or inconsistency found; 2 = inputs unreadable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from speechnexus_backend.speech.infrastructure.tts_recipe import load_tts_recipe
from speechnexus_gateway.infrastructure.vllm_tts import load_controlled_tts_endpoint

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = ROOT / "deploy" / "private"
DEFAULT_RECIPE = "speech-recipe.json"
DEFAULT_GATEWAY = "gateway-file-tts.json"
DEFAULT_ENDPOINT = "gateway-file-tts-endpoint.json"
EXAMPLE_SUFFIX = ".example.json"
# The one substring the committed examples use to mark synthetic identities.
SYNTHETIC_MARKER = "-compose-example"
# The example's credential is all zeros: format-valid (64 hex) and useless on
# purpose, so a format check cannot catch an unreplaced token.
ZERO_CREDENTIAL = "0" * 64

State = Literal["checked", "placeholder", "inconsistent", "undecidable"]


@dataclass(frozen=True, slots=True)
class Item:
    field: str
    state: State
    detail: str = ""


def _example(name: str) -> dict[str, object]:
    path = EXAMPLE_DIR / f"{name.removesuffix('.json')}{EXAMPLE_SUFFIX}"
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"{path} is not a JSON object")
    return cast(dict[str, object], document)


def _dig(document: object, path: str) -> object:
    node = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _identity(
    items: list[Item], actual: dict[str, object], sample: dict[str, object], path: str
) -> None:
    """A field whose example value must not survive: an id, digest or address."""
    value = _dig(actual, path)
    if value in (None, ""):
        items.append(Item(path, "inconsistent", "value is missing"))
    elif value == _dig(sample, path):
        items.append(Item(path, "placeholder", f"{value!r} is the committed example's value"))
    else:
        items.append(Item(path, "checked", f"{value!r}"))


def _semantic(
    items: list[Item], actual: dict[str, object], sample: dict[str, object], path: str
) -> None:
    """A field the example may legitimately share: an enum, flag or budget.

    Equality proves nothing, so both cases are undecidable: only the operator can
    confirm the value against the registered plan.
    """
    value = _dig(actual, path)
    if value is None:
        items.append(Item(path, "inconsistent", "value is missing"))
    elif value == _dig(sample, path):
        items.append(Item(path, "undecidable", f"{value!r} equals the example but may be correct"))
    else:
        items.append(Item(path, "undecidable", f"{value!r} differs from the example"))


def _pool(items: list[Item], document: dict[str, object], path: str) -> None:
    """The TTS pool is a domain constant, not identity: `tts-batch` or nothing."""
    value = _dig(document, path)
    if value != "tts-batch":
        items.append(Item(path, "inconsistent", f"{value!r} is not the only legal TTS pool"))
    else:
        items.append(Item(path, "checked", "'tts-batch' is the only legal TTS pool"))


def _marker(items: list[Item], document: dict[str, object], label: str, seen: set[str]) -> None:
    """A replaced-looking value that still spells the example's synthetic marker."""
    for path in _paths(document):
        if path in seen:
            continue
        value = _dig(document, path)
        if isinstance(value, str) and SYNTHETIC_MARKER in value:
            items.append(
                Item(
                    f"{label}.{path}",
                    "placeholder",
                    f"{value!r} still carries {SYNTHETIC_MARKER!r}",
                )
            )


def _paths(document: object, prefix: str = "") -> Iterator[str]:
    if isinstance(document, dict):
        for key, value in document.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                yield from _paths(value, f"{path}.")
            else:
                yield path


def _agreement(items: list[Item], pairs: tuple[tuple[str, object, object], ...]) -> None:
    for label, left, right in pairs:
        if left != right:
            items.append(Item(f"agreement: {label}", "inconsistent", f"{left!r} != {right!r}"))


def check(directory: Path) -> list[Item]:
    """Judge every field of the three private documents; never raise on content."""
    items: list[Item] = []
    paths = {
        "recipe": directory / DEFAULT_RECIPE,
        "gateway": directory / DEFAULT_GATEWAY,
        "endpoint": directory / DEFAULT_ENDPOINT,
    }
    for label, candidate in paths.items():
        if not candidate.is_file():
            items.append(Item(f"{label}", "inconsistent", f"missing private input: {candidate}"))
    if items:
        return items

    try:
        recipe = load_tts_recipe(paths["recipe"])
        endpoint = load_controlled_tts_endpoint(paths["endpoint"])
    except ValueError as error:
        items.append(Item("loader", "inconsistent", f"rejected by the production loader: {error}"))
        return items

    recipe_doc = json.loads(paths["recipe"].read_text(encoding="utf-8"))
    gateway_doc = json.loads(paths["gateway"].read_text(encoding="utf-8"))
    endpoint_doc = json.loads(paths["endpoint"].read_text(encoding="utf-8"))
    samples = {
        "recipe": _example(DEFAULT_RECIPE),
        "gateway": _example(DEFAULT_GATEWAY),
        "endpoint": _example(DEFAULT_ENDPOINT),
    }

    for path in (
        "model_id",
        "binding_revision_id",
        "manifest_id",
        "policy_revision_id",
        "recipe_digest",
        "stage.stage_id",
        "stage.instance_id",
        "runtime_plan.binding.target_node_id",
        "runtime_plan.binding.gateway_id",
        "runtime_plan.binding.approved_template_id",
        "runtime_plan.topology_digest",
        "runtime_plan.qualification_digest",
    ):
        _identity(items, recipe_doc, samples["recipe"], path)
    for path in (
        "runtime_plan.binding.binding_revision_id",
        "runtime_plan.binding.manifest_id",
        "runtime_plan.binding.config_digest",
    ):
        _identity(items, recipe_doc, samples["recipe"], path)
    _pool(items, recipe_doc, "runtime_plan.binding.pool_id")
    _pool(items, endpoint_doc, "target.pool_id")
    for path in (
        "service_class",
        "quota_dimensions",
        "runtime_plan.memory_architecture",
        "runtime_plan.max_slots",
        "runtime_plan.realtime_reserved_slots",
        "runtime_plan.peak.unified_physical_memory_bytes",
        "runtime_plan.peak.device_memory_bytes",
        "runtime_plan.peak.host_memory_bytes",
        "runtime_plan.peak.cpu_decode_millicores",
        "runtime_plan.peak.host_pcm_buffer_bytes",
        "runtime_plan.peak.scratch_disk_bytes",
        "runtime_plan.steady.unified_physical_memory_bytes",
        "runtime_plan.steady.device_memory_bytes",
        "runtime_plan.per_slot.device_memory_bytes",
    ):
        _semantic(items, recipe_doc, samples["recipe"], path)

    # Expiry is decidable, and it is not a file-level matter of taste: the factory
    # answers an expired recipe by keeping synchronous speech switched off.
    expires = recipe.approved.admission_expires_at_us
    now_us = time.time_ns() // 1_000
    # Expiry is decided by the clock, not by comparison with the example: the
    # example's far-future value is usable as-is, a past one disables the factory.
    if expires <= now_us:
        items.append(
            Item("admission_expires_at_us", "inconsistent", f"{expires} is already in the past")
        )
    else:
        items.append(Item("admission_expires_at_us", "checked", f"{expires} is still ahead"))

    for path in (
        "target.host_node_id",
        "target.gateway_id",
        "target.instance_id",
        "target.binding_revision_id",
        "served_model",
        "address",
        "port",
    ):
        _identity(items, endpoint_doc, samples["endpoint"], path)
    if endpoint.bearer_token == ZERO_CREDENTIAL:
        items.append(
            Item(
                "bearer_token",
                "placeholder",
                "the example's all-zero placeholder; the descriptor loads, so an unreplaced "
                "token surfaces only as an engine 401",
            )
        )
    elif endpoint.bearer_token == _dig(samples["endpoint"], "bearer_token"):
        items.append(Item("bearer_token", "placeholder", "equals the committed example"))
    else:
        items.append(Item("bearer_token", "checked", "64 hex characters, not the example"))

    for path in (
        "tts_policy.config_revision",
        "builtin_voice_binding.voice_id",
        "builtin_voice_binding.voice_revision_id",
    ):
        _identity(items, gateway_doc, samples["gateway"], path)
    for path in (
        "builtin_voice_binding.native_speaker_id",
        "tts_policy.voice",
        "tts_policy.task",
        "tts_policy.sample_rate_hz",
    ):
        _semantic(items, gateway_doc, samples["gateway"], path)

    binding = recipe.approved.stages[0].runtime_plan.binding
    stage = _dig(recipe_doc, "stage")
    target = _dig(endpoint_doc, "target")
    voice = _dig(gateway_doc, "builtin_voice_binding")
    policy = _dig(gateway_doc, "tts_policy")
    _agreement(
        items,
        (
            (
                "recipe.binding_revision_id == endpoint.target.binding_revision_id",
                recipe.approved.binding_revision_id,
                endpoint.binding_revision_id,
            ),
            (
                "recipe.manifest_id == plan.binding.manifest_id",
                recipe.approved.manifest_id,
                binding.manifest_id,
            ),
            (
                "recipe.stage.instance_id == endpoint.target.instance_id",
                _dig(stage, "instance_id"),
                _dig(target, "instance_id"),
            ),
            (
                "plan.node == endpoint.target.host_node_id",
                binding.target_node_id,
                _dig(target, "host_node_id"),
            ),
            (
                "plan.gateway == endpoint.target.gateway_id",
                binding.gateway_id,
                _dig(target, "gateway_id"),
            ),
            ("plan.pool == endpoint.target.pool_id", binding.pool_id, _dig(target, "pool_id")),
            (
                "recipe.model_id == endpoint.served_model",
                _dig(recipe_doc, "model_id"),
                endpoint.served_model,
            ),
            (
                "recipe.service_class == plan.service_class",
                recipe.approved.service_class,
                recipe.approved.stages[0].runtime_plan.service_class,
            ),
            (
                "gateway.tts_policy.voice == builtin_voice_binding.native_speaker_id",
                _dig(policy, "voice"),
                _dig(voice, "native_speaker_id"),
            ),
        ),
    )
    seen: set[str] = set()
    for item in items:
        seen.add(item.field)
        seen.add(item.field.split(".", 1)[-1])
    for document, label in (
        (recipe_doc, "recipe"),
        (endpoint_doc, "endpoint"),
        (gateway_doc, "gateway"),
    ):
        _marker(items, document, label, seen)
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True, help="resolved private dir")
    args = parser.parse_args(argv)
    directory: Path = args.private_dir
    if not directory.is_dir():
        print(
            f"tts assembly unreadable: private directory {directory} does not exist",
            file=sys.stderr,
        )
        return 2
    items = check(directory)
    grouped: dict[str, list[Item]] = {
        "placeholder": [],
        "inconsistent": [],
        "undecidable": [],
        "checked": [],
    }
    for item in items:
        grouped[item.state].append(item)
    for state, label in (
        ("placeholder", "仍是样例值"),
        ("inconsistent", "不一致"),
        ("undecidable", "无法从文件判定"),
        ("checked", "本地检查通过"),
    ):
        if not grouped[state]:
            continue
        print(f"{label} ({len(grouped[state])})")
        for item in grouped[state]:
            print(f"  {item.field}: {item.detail}")
    print(
        f"summary: {len(grouped['checked'])} 本地检查通过 / "
        f"{len(grouped['placeholder'])} 仍是样例值 / "
        f"{len(grouped['inconsistent'])} 不一致 / "
        f"{len(grouped['undecidable'])} 无法从文件判定"
    )
    print(
        "scope: 只覆盖「文件级替换与文件内一致性」。受理另需 TTS engine_instances 注册、plan 摘要与"
        "预算与注册值一致、policy_revision_id 等于声音资格的 gateway_policy_revision_id、"
        "该 binding 下已发布且未退役的内建声音，见 deploy/README.md「Synchronous TTS assembly」。"
    )
    blocked = bool(grouped["placeholder"] or grouped["inconsistent"])
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
