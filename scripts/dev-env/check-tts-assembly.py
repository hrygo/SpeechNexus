#!/usr/bin/env python3
"""Report which synchronous-TTS private inputs an operator still has to replace.

The reference deployment assembles synchronous TTS from files that live outside
git: the private recipe (`SPEECHNEXUS_SPEECH_RECIPE_FILE`), the gateway's private
`file_tts_v2` config and its controlled endpoint descriptor. Every one of them is
schema-valid while still carrying the committed example's synthetic identities, so
"the loaders accept it" says nothing about "the values were replaced".

All three documents are judged by **their own production loaders**; a document a
loader rejects is red here, and the fields the loader's own rules can name are
named. Above that, this tool answers two questions: *is a field still the committed
example's value?* and *do the files agree with each other?* A value with a local
oracle (a domain constant, a legal set, a mutual relation, the clock) is decided; a
value without one (plan budgets, the real engine address, the registration
generation) is reported as undecidable; and the leaves only their loader checks are
listed by name instead of being silently skipped.

Agreement is read from the runtime, not from pairing fields that look alike.
`recipe.model_id` is the public `model` (binding id) the caller sends, while the
endpoint's `served_model` is the engine's own model name. Admission never compares
the two, so they must stay in their own namespaces and equality is red. The two
directions fail differently: the engine name in `model_id` is refused by admission
on the first request (`stage=recipe_mismatch`, DEL-538), while the public name in
`served_model` passes admission and is only rejected later by the engine as an
unknown model. The recipe's public model is not paired with a plan here because no
plan document is an input; this namespace rule is the offline-decidable part.

Exit codes: 0 = nothing is still the example's value and the files agree. That is
**not** "ready to accept a request"; 1 = placeholder or inconsistency found;
2 = the private directory is missing.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import SecretStr

from speechnexus_backend.speech.infrastructure.tts_recipe import load_tts_recipe
from speechnexus_gateway.file_tts_runtime import load_file_tts_configuration
from speechnexus_gateway.infrastructure.vllm_tts import load_controlled_tts_endpoint
from speechnexus_gateway.settings import Settings as GatewaySettings

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
# Constants the domain or the loaders force; a different value cannot work at all.
TTS_POOL = "tts-batch"
TTS_TASK = "CustomVoice"
TTS_SAMPLE_RATE_HZ = 24000
ENGINE_FORMATS = ("wav", "pcm")
# Legal sets: the value must be one of them, but the choice is a plan fact.
SERVICE_CLASSES = ("realtime", "interactive", "batch")
ARCHITECTURES = ("unified_memory", "discrete_device_memory")
LOADING_POLICIES = ("resident", "on_demand")
SPEED_BOUNDS = (0.25, 4.0)

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


def _paths(document: object, prefix: str = "") -> Iterator[str]:
    if isinstance(document, dict):
        for key, value in document.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                yield from _paths(value, f"{path}.")
            else:
                yield path


class Report:
    """Collects one item per judged leaf, so coverage is measurable."""

    def __init__(self) -> None:
        self.items: list[Item] = []
        self.judged: set[str] = set()

    def add(self, item: Item, *, bare: str | None = None) -> None:
        self.items.append(item)
        self.judged.add(bare if bare is not None else item.field)

    def identity(
        self, actual: dict[str, object], sample: dict[str, object], path: str, label: str
    ) -> None:
        """A field whose example value must not survive: an id, digest or address."""
        value = _dig(actual, path)
        if value in (None, ""):
            self.add(Item(f"{label}.{path}", "inconsistent", "value is missing"), bare=path)
        elif value == _dig(sample, path):
            self.add(
                Item(
                    f"{label}.{path}",
                    "placeholder",
                    f"{value!r} is the committed example's value",
                ),
                bare=path,
            )
        else:
            self.add(Item(f"{label}.{path}", "checked", f"{value!r}"), bare=path)

    def constant(
        self,
        document: dict[str, object],
        path: str,
        allowed: tuple[object, ...],
        label: str,
    ) -> None:
        """A value its loader or the domain forces; a different value cannot work."""
        value = _dig(document, path)
        if value not in allowed:
            self.add(
                Item(
                    f"{label}.{path}",
                    "inconsistent",
                    f"{value!r} is not one of the only legal values {allowed}",
                ),
                bare=path,
            )
        else:
            self.add(
                Item(f"{label}.{path}", "checked", f"{value!r} is the only legal value"),
                bare=path,
            )

    def legal(
        self,
        document: dict[str, object],
        path: str,
        allowed: tuple[object, ...],
        label: str,
    ) -> None:
        """A value from a legal set whose choice is still a registration fact."""
        value = _dig(document, path)
        if value not in allowed:
            self.add(
                Item(
                    f"{label}.{path}",
                    "inconsistent",
                    f"{value!r} is not in the legal set {allowed}",
                ),
                bare=path,
            )
        else:
            self.add(
                Item(
                    f"{label}.{path}",
                    "checked",
                    f"{value!r} is legal; whether the registered plan uses it is a "
                    "registration fact",
                ),
                bare=path,
            )

    def relation(self, field: str, detail: str, bare: str) -> None:
        """A relation its production loader already enforced on the accepted files."""
        self.add(Item(field, "checked", detail), bare=bare)

    def undecidable(self, field: str, detail: str, bare: str) -> None:
        self.add(Item(field, "undecidable", detail), bare=bare)


def _load_gateway(
    directory: Path, gateway_path: Path, endpoint_path: Path, node: str, gateway: str
) -> tuple[bool, str, list[str]]:
    """Run the gateway's own `load_file_tts_configuration` on a staged copy.

    Only two things are rewritten: `controlled_endpoint_file` (a container path)
    becomes the resolved local descriptor, and an unresolvable `tls_ca_file` is
    nulled and named. Everything else -- schema, enums, ranges, the
    voice/native-speaker relation, the target's node and gateway -- is judged by
    the production loader itself.
    """
    document = json.loads(gateway_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        return False, "the gateway configuration is not a JSON object", []
    notes: list[str] = []
    document["controlled_endpoint_file"] = str(endpoint_path)
    objects = document.get("objects")
    ca_file = objects.get("tls_ca_file") if isinstance(objects, dict) else None
    if isinstance(ca_file, str) and ca_file and not Path(ca_file).is_file():
        local = directory / Path(ca_file).name
        assert isinstance(objects, dict)
        objects["tls_ca_file"] = str(local) if local.is_file() else None
        if objects["tls_ca_file"] is None:
            notes.append(
                f"tls_ca_file {ca_file!r} 无法在本地解析；loader 在无 CA 的情况下运行，"
                "该字段的真实性未验证"
            )
    with tempfile.TemporaryDirectory() as temporary:
        staged = Path(temporary) / DEFAULT_GATEWAY
        staged.write_text(json.dumps(document), encoding="utf-8")
        staged.chmod(0o600)
        settings = GatewaySettings.model_construct(
            node_id=node,
            gateway_id=gateway,
            file_tts_config_file=staged,
            file_tts_object_access_key=SecretStr("check-only-access-key"),
            file_tts_object_secret_key=SecretStr("check-only-secret-key"),
        )
        try:
            load_file_tts_configuration(settings)
        except ValueError as error:
            return False, str(error), notes
    return True, "", notes


def _judge_gateway_rules(
    report: Report,
    gateway_doc: dict[str, object],
    endpoint_doc: dict[str, object],
    *,
    node: str,
    gateway: str,
) -> None:
    """Name the fields the gateway loader's own rules reject, one probe each."""
    target = _dig(endpoint_doc, "target")
    if _dig(target, "host_node_id") != node:
        report.add(
            Item(
                "endpoint.target.host_node_id",
                "inconsistent",
                f"gateway 的 node_id 是 {node!r}（来自配方 plan），descriptor 写的是 "
                f"{_dig(target, 'host_node_id')!r}",
            ),
            bare="target.host_node_id",
        )
    if _dig(target, "gateway_id") != gateway:
        report.add(
            Item(
                "endpoint.target.gateway_id",
                "inconsistent",
                f"gateway 的 gateway_id 是 {gateway!r}（来自配方 plan），descriptor 写的是 "
                f"{_dig(target, 'gateway_id')!r}",
            ),
            bare="target.gateway_id",
        )
    speed = _dig(gateway_doc, "tts_policy.speed")
    low, high = SPEED_BOUNDS
    if isinstance(speed, bool) or not isinstance(speed, int | float) or not low <= speed <= high:
        report.add(
            Item(
                "gateway.tts_policy.speed",
                "inconsistent",
                f"{speed!r} 超出 {low}–{high}；gateway 的 loader 会拒绝整个配置",
            ),
            bare="tts_policy.speed",
        )
    else:
        report.relation(
            "gateway.tts_policy.speed",
            f"{speed!r} 在 {low}–{high} 之内",
            bare="tts_policy.speed",
        )
    worker = _dig(gateway_doc, "worker_limits.max_result_bytes")
    within_bounds = (
        isinstance(worker, int)
        and not isinstance(worker, bool)
        and 1024 <= worker <= 64 * 1024 * 1024
    )
    if within_bounds:
        report.relation(
            "gateway.worker_limits.max_result_bytes",
            f"{worker!r} 在 1 KiB–64 MiB 之内",
            bare="worker_limits.max_result_bytes",
        )
    else:
        report.add(
            Item(
                "gateway.worker_limits.max_result_bytes",
                "inconsistent",
                f"{worker!r} 超出 1 KiB–64 MiB；gateway 的 loader 会拒绝整个配置",
            ),
            bare="worker_limits.max_result_bytes",
        )


def check(
    directory: Path,
) -> tuple[list[Item], dict[str, list[str]], dict[str, list[str]]]:
    """Judge every field of the three private documents; never raise on content."""
    report = Report()
    paths = {
        "recipe": directory / DEFAULT_RECIPE,
        "gateway": directory / DEFAULT_GATEWAY,
        "endpoint": directory / DEFAULT_ENDPOINT,
    }
    missing = [label for label, candidate in paths.items() if not candidate.is_file()]
    if missing:
        return (
            [
                Item(label, "inconsistent", f"missing private input: {paths[label]}")
                for label in missing
            ],
            {},
            {},
        )

    documents: dict[str, dict[str, object]] = {}
    for label, path in paths.items():
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return [Item(label, "inconsistent", f"{path} is not a JSON object")], {}, {}
        documents[label] = cast(dict[str, object], document)
    samples = {
        "recipe": _example(DEFAULT_RECIPE),
        "gateway": _example(DEFAULT_GATEWAY),
        "endpoint": _example(DEFAULT_ENDPOINT),
    }

    try:
        recipe = load_tts_recipe(paths["recipe"])
        endpoint = load_controlled_tts_endpoint(paths["endpoint"])
    except ValueError as error:
        return [Item("loader", "inconsistent", f"生产 loader 拒绝：{error}")], {}, {}

    recipe_doc = documents["recipe"]
    gateway_doc = documents["gateway"]
    endpoint_doc = documents["endpoint"]
    binding = recipe.approved.stages[0].runtime_plan.binding
    accepted, message, notes = _load_gateway(
        directory, paths["gateway"], paths["endpoint"], binding.target_node_id, binding.gateway_id
    )
    if not accepted:
        report.add(
            Item(
                "loader: gateway-file-tts.json",
                "inconsistent",
                "生产 loader（load_file_tts_configuration）拒绝整个配置："
                f"{message}；下面逐项点名其中能被本地规则识别的字段",
            ),
            bare="loader: gateway-file-tts.json",
        )
    for note in notes:
        report.undecidable("gateway.objects.tls_ca_file", note, bare="objects.tls_ca_file")

    for dotted in (
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
        "runtime_plan.binding.binding_revision_id",
        "runtime_plan.binding.manifest_id",
        "runtime_plan.binding.config_digest",
        "runtime_plan.topology_digest",
        "runtime_plan.qualification_digest",
    ):
        report.identity(recipe_doc, samples["recipe"], dotted, "recipe")
    for dotted in (
        "target.host_node_id",
        "target.gateway_id",
        "target.instance_id",
        "target.binding_revision_id",
        "served_model",
        "address",
        "port",
    ):
        report.identity(endpoint_doc, samples["endpoint"], dotted, "endpoint")
    for dotted in (
        "tts_policy.config_revision",
        "builtin_voice_binding.voice_id",
        "builtin_voice_binding.voice_revision_id",
    ):
        report.identity(gateway_doc, samples["gateway"], dotted, "gateway")

    # Constants: one legal value each, enforced by a production loader or the domain.
    report.constant(recipe_doc, "protocol", ("tts_admission_v1",), "recipe")
    report.constant(recipe_doc, "quota_dimensions", (["concurrent_attempts"],), "recipe")
    report.constant(recipe_doc, "runtime_plan.binding.pool_id", (TTS_POOL,), "recipe")
    report.constant(endpoint_doc, "protocol", ("vllm_api_key_v1",), "endpoint")
    report.constant(endpoint_doc, "target.pool_id", (TTS_POOL,), "endpoint")
    report.constant(gateway_doc, "protocol", ("file_tts_v2",), "gateway")
    report.constant(gateway_doc, "tts_policy.task", (TTS_TASK,), "gateway")
    report.constant(gateway_doc, "tts_policy.sample_rate_hz", (TTS_SAMPLE_RATE_HZ,), "gateway")
    report.constant(gateway_doc, "tts_policy.engine_format", ENGINE_FORMATS, "gateway")

    # Legal sets: the value must be one of them, but the choice is a plan fact.
    report.legal(recipe_doc, "service_class", SERVICE_CLASSES, "recipe")
    report.legal(recipe_doc, "runtime_plan.service_class", SERVICE_CLASSES, "recipe")
    report.legal(recipe_doc, "runtime_plan.loading_policy", LOADING_POLICIES, "recipe")
    report.legal(recipe_doc, "runtime_plan.memory_architecture", ARCHITECTURES, "recipe")

    # Relations the accepted loads already enforced.
    report.relation(
        "recipe.runtime_plan.realtime_reserved_slots",
        "域规则要求不超过 max_slots；具体取值属于已批准的 plan",
        bare="runtime_plan.realtime_reserved_slots",
    )
    report.relation(
        "gateway.builtin_voice_binding.native_speaker_id",
        "loader 要求与 tts_policy.voice 相等",
        bare="builtin_voice_binding.native_speaker_id",
    )
    report.relation(
        "gateway.tts_policy.voice",
        "loader 要求与 builtin_voice_binding.native_speaker_id 相等",
        bare="tts_policy.voice",
    )
    _judge_gateway_rules(
        report, gateway_doc, endpoint_doc, node=binding.target_node_id, gateway=binding.gateway_id
    )

    # Expiry is decided by the clock. The example's far-future value is usable
    # as-is, so equality with it is not a placeholder signal -- and the detail
    # says so instead of letting a copied value look checked by accident.
    expires = recipe.approved.admission_expires_at_us
    sample_expires = _dig(samples["recipe"], "admission_expires_at_us")
    now_us = time.time_ns() // 1_000
    if expires <= now_us:
        report.add(
            Item(
                "recipe.admission_expires_at_us",
                "inconsistent",
                f"{expires} is already in the past",
            ),
            bare="admission_expires_at_us",
        )
    elif expires == sample_expires:
        report.add(
            Item(
                "recipe.admission_expires_at_us",
                "checked",
                f"{expires} 是样例的远期值（约 2096 年），可用；刻意接受，不强制轮换",
            ),
            bare="admission_expires_at_us",
        )
    else:
        report.add(
            Item(
                "recipe.admission_expires_at_us",
                "checked",
                f"{expires} is still ahead",
            ),
            bare="admission_expires_at_us",
        )

    if endpoint.bearer_token == ZERO_CREDENTIAL:
        report.add(
            Item(
                "endpoint.bearer_token",
                "placeholder",
                "the example's all-zero placeholder; the descriptor loads, so an unreplaced "
                "token surfaces only as an engine 401",
            ),
            bare="bearer_token",
        )
    elif endpoint.bearer_token == _dig(samples["endpoint"], "bearer_token"):
        report.add(
            Item("endpoint.bearer_token", "placeholder", "equals the committed example"),
            bare="bearer_token",
        )
    else:
        report.add(
            Item("endpoint.bearer_token", "checked", "64 hex characters, not the example"),
            bare="bearer_token",
        )

    # Budget and plan values with no local oracle: the operator must compare them
    # with what registration accepted.
    for dotted in (
        "stage.minimum_slots",
        "stage.reserved_slots",
        "runtime_plan.max_slots",
        "runtime_plan.peak.unified_physical_memory_bytes",
        "runtime_plan.peak.device_memory_bytes",
        "runtime_plan.peak.host_memory_bytes",
        "runtime_plan.peak.cpu_decode_millicores",
        "runtime_plan.peak.host_pcm_buffer_bytes",
        "runtime_plan.peak.scratch_disk_bytes",
        "runtime_plan.steady.unified_physical_memory_bytes",
        "runtime_plan.steady.device_memory_bytes",
        "runtime_plan.steady.host_memory_bytes",
        "runtime_plan.steady.cpu_decode_millicores",
        "runtime_plan.steady.host_pcm_buffer_bytes",
        "runtime_plan.steady.scratch_disk_bytes",
        "runtime_plan.per_slot.unified_physical_memory_bytes",
        "runtime_plan.per_slot.device_memory_bytes",
        "runtime_plan.per_slot.host_memory_bytes",
        "runtime_plan.per_slot.cpu_decode_millicores",
        "runtime_plan.per_slot.host_pcm_buffer_bytes",
        "runtime_plan.per_slot.scratch_disk_bytes",
    ):
        value = _dig(recipe_doc, dotted)
        if value is None:
            continue
        report.undecidable(
            f"recipe.{dotted}",
            f"{value!r} 必须与注册时被接受的 plan 逐项相等；本地无 oracle"
            + ("（与样例相同）" if value == _dig(samples["recipe"], dotted) else ""),
            bare=dotted,
        )
    generation = _dig(endpoint_doc, "target.instance_generation")
    report.undecidable(
        "endpoint.target.instance_generation",
        f"{generation!r} 必须等于注册后的 current_generation；首次注册通常就是 1，"
        "所以与样例相同不构成问题",
        bare="target.instance_generation",
    )

    stage = _dig(recipe_doc, "stage")
    target = _dig(endpoint_doc, "target")
    voice = _dig(gateway_doc, "builtin_voice_binding")
    policy = _dig(gateway_doc, "tts_policy")
    for label, left, right in (
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
            "recipe.service_class == plan.service_class",
            recipe.approved.service_class,
            recipe.approved.stages[0].runtime_plan.service_class,
        ),
        (
            "gateway.tts_policy.voice == builtin_voice_binding.native_speaker_id",
            _dig(policy, "voice"),
            _dig(voice, "native_speaker_id"),
        ),
    ):
        if left != right:
            report.add(
                Item(f"agreement: {label}", "inconsistent", f"{left!r} != {right!r}"),
                bare=f"agreement: {label}",
            )

    # Runtime admission compares `plan.model_id` -- the public `model` the caller
    # sends, i.e. the binding id -- with `recipe.model_id`, and the plan's binding
    # revision with the recipe's (`tts_admission.py`). The engine's `served_model`
    # takes part in neither: the gateway sends it verbatim as the request's `model`
    # to the engine (`vllm_tts.py`), and the reference CustomVoice engine serves an
    # alias such as `qwen3-tts-1p7b-custom`, a different string from the public
    # binding name.
    #
    # The plan is not one of the three private inputs, and the caller's `model` is
    # only knowable per request, so the pair admission compares cannot be replayed
    # offline. What is decidable is the direction that actually failed in DEL-538:
    # the old rule here required the two names to be equal, so an operator who
    # obeyed it wrote the engine alias into the public `model_id`, and the first
    # request failed `stage=recipe_mismatch` with a 503. The names therefore must
    # stay in their own namespaces, and equality is red -- deliberately even for a
    # deployment that could set the engine's `--served-model-name` to the public
    # name: the two namespaces stay separate.
    #
    # This is also the constraint the old equality implied for `served_model`, on
    # top of the identity check above (no blank, not the example's value) and the
    # descriptor loader (identifier shape): the two names are distinct. It does not
    # prove that `served_model` equals the engine's real `--served-model-name`; no
    # file here records that alias (the engine template launches vLLM without the
    # flag, and the artifact pin holds the checkpoint repository, not the served
    # name), so only a real request proves it.
    served_model = endpoint.served_model
    public_model = _dig(recipe_doc, "model_id")
    if served_model and served_model == public_model:
        report.add(
            Item(
                "agreement: recipe.model_id (public model/binding id) != endpoint.served_model",
                "inconsistent",
                f"两个字段都是 {public_model!r}；公开 model/binding id 与引擎模型名属于"
                "不同命名空间。引擎名写进 model_id 会让首次请求得到 stage=recipe_mismatch "
                "503（DEL-538）；公开 model 名写进 served_model 则会在引擎侧被当作未知 "
                "model 拒绝。",
            ),
            bare="agreement: recipe.model_id != endpoint.served_model",
        )

    for document, label in (
        (recipe_doc, "recipe"),
        (endpoint_doc, "endpoint"),
        (gateway_doc, "gateway"),
    ):
        for dotted in _paths(document):
            value = _dig(document, dotted)
            if isinstance(value, str) and SYNTHETIC_MARKER in value and dotted not in report.judged:
                report.add(
                    Item(
                        f"{label}.{dotted}",
                        "placeholder",
                        f"{value!r} still carries {SYNTHETIC_MARKER!r}",
                    ),
                    bare=dotted,
                )

    # Whatever is left is only covered by its production loader: schema, enums,
    # ranges and the relations above. It is listed by name so "no line about it"
    # never reads as "checked" -- and it stays listed even when a loader rejected
    # the file, under a heading that says no check happened at all.
    loader_only: dict[str, list[str]] = {}
    unchecked: dict[str, list[str]] = {}
    for document, label in (
        (recipe_doc, "recipe"),
        (endpoint_doc, "endpoint"),
        (gateway_doc, "gateway"),
    ):
        remaining = [
            dotted
            for dotted in _paths(document)
            if dotted not in report.judged and not dotted.startswith("agreement")
        ]
        if not remaining:
            continue
        if label == "gateway" and not accepted:
            unchecked[label] = remaining
        else:
            loader_only[label] = remaining
    return report.items, loader_only, unchecked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-dir", type=Path, required=True, help="directory holding the three runtime files"
    )
    args = parser.parse_args(argv)
    directory: Path = args.private_dir.expanduser().resolve()
    if not directory.is_dir():
        print(
            f"tts assembly unreadable: private directory {args.private_dir} does not exist",
            file=sys.stderr,
        )
        return 2
    items, loader_only, unchecked = check(directory)
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
    if loader_only:
        total = sum(len(paths) for paths in loader_only.values())
        print(f"仅由生产 loader 校验，无独立 oracle ({total})")
        for label, paths in loader_only.items():
            print(f"  {label}: {', '.join(paths)}")
    if unchecked:
        total = sum(len(paths) for paths in unchecked.values())
        print(f"完全未被校验：该文件的 loader 拒绝了它 ({total})")
        for label, paths in unchecked.items():
            print(f"  {label}: {', '.join(paths)}")
    sample_values = sum(
        1
        for item in grouped["undecidable"]
        if "（与样例相同）" in item.detail or "equals the example" in item.detail
    )
    print(
        f"summary: 仍是样例值 {len(grouped['placeholder'])} 项 / "
        f"不一致 {len(grouped['inconsistent'])} 项 / "
        f"本地检查通过 {len(grouped['checked'])} 项 / "
        f"无法从文件判定 {len(grouped['undecidable'])} 项"
        f"（其中 {sample_values} 项逐字等于样例值）"
    )
    print("exit 0 不代表可受理：它只表示上面「仍是样例值」「不一致」两类为空、文件之间不矛盾。")
    print(
        "scope: 只覆盖「文件级替换与文件内一致性」。受理另需：结算 DSN 让 build_speech 真的装配、"
        "TTS engine_instances 注册、注册值与配方 plan 逐项相等、policy_revision_id 等于声音资格的 "
        "gateway_policy_revision_id、该 binding 下已发布且未退役的内建声音、以及真实引擎；"
        "见 deploy/README.md「Synchronous TTS assembly」。"
    )
    blocked = bool(grouped["placeholder"] or grouped["inconsistent"])
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
