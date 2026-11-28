#!/usr/bin/env python3
"""Publish the file-ASR binding for the minimal cluster and rebind private inputs.

The script derives every digest from the repository's own pins, publishes one
binding through the public admin routes, and rewrites the two private inputs
that pin the binding by name (the transcription recipe and the gateway's
controlled endpoint descriptor). It refuses to invent identities: the binding
id, logical model, capacity manifest and qualification identifiers are
constants of this deployment and are printed with the published revisions.

Usage:
  python3 deploy/cluster/app-plane/publish-binding.py \
      --api http://127.0.0.1:8000 \
      --recipe .runtime/cluster/private/transcription-recipe.json \
      --gateway-endpoint .runtime/cluster/compute/gateway-file-asr-endpoint.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import rfc8785

BINDING_ID = "model-asr-minimal"
LOGICAL_MODEL = "asr-qwen3-1.7b"
CAPACITY_MANIFEST_ID = "capacity-manifest-minimal-asr"
QUALIFICATION_REPORT_ID = "qualification-minimal-asr"
POLICY_REVISION_ID = "policy-revision-minimal-asr"
TEMPLATE_ID = "template-asr-1.7b"
NODE_ID = "node-megacube-1"
GATEWAY_ID = "gateway-megacube-1"
INSTANCE_ID = "instance-asr-1"
POOL_ID = "asr-batch"

ROOT = Path(__file__).resolve().parents[3]


def request_json(method: str, url: str, body: dict | None) -> tuple[int, dict]:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"raw": raw.decode(errors="replace")}


def build_recipe(path: Path) -> dict:
    document = json.loads(
        (ROOT / "deploy/private/transcription-recipe.example.json").read_text(encoding="utf-8")
    )
    document.update(
        {
            # The recipe's ``model_id`` is the *public* model/binding id the
            # transcription route is called with, not the engine's logical model
            # name. The two live in separate namespaces (the engine is launched
            # without ``--served-model-name``), so writing ``LOGICAL_MODEL`` here
            # makes every synchronous request fail admission with
            # ``recipe_mismatch`` -- the DEL-538 failure the assembly checker
            # documents in scripts/dev-env/check-tts-assembly.py.
            "model_id": BINDING_ID,
            "manifest_id": CAPACITY_MANIFEST_ID,
            "policy_revision_id": POLICY_REVISION_ID,
            "stage": {
                "stage_id": "stage-asr-minimal",
                "instance_id": INSTANCE_ID,
                "minimum_slots": 1,
                "reserved_slots": 1,
            },
        }
    )
    binding = document["runtime_plan"]["binding"]
    binding.update(
        {
            "target_node_id": NODE_ID,
            "pool_id": POOL_ID,
            "gateway_id": GATEWAY_ID,
            "approved_template_id": TEMPLATE_ID,
            "manifest_id": CAPACITY_MANIFEST_ID,
        }
    )
    document["runtime_plan"]["memory_architecture"] = "unified_memory"
    document["runtime_plan"]["peak"]["unified_physical_memory_bytes"] = 12884901888
    document["runtime_plan"]["steady"]["unified_physical_memory_bytes"] = 12884901888
    document["runtime_plan"]["per_slot"]["unified_physical_memory_bytes"] = 0
    for key in ("peak", "steady", "per_slot"):
        # A unified-memory host charges everything through the unified field: the
        # capacity vector rejects charging the same bytes as host or device RAM.
        document["runtime_plan"][key]["device_memory_bytes"] = 0
        document["runtime_plan"][key]["host_memory_bytes"] = 0
    return document


def seal_recipe(document: dict) -> dict:
    body = {key: value for key, value in document.items() if key != "recipe_digest"}
    document["recipe_digest"] = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    return document


def bind_revision(document: dict, revision_id: str) -> dict:
    document["binding_revision_id"] = revision_id
    if "binding_publication_id" in document:
        document["binding_publication_id"] = revision_id
    document["runtime_plan"]["binding"]["binding_revision_id"] = revision_id
    return document


def build_draft(recipe: dict) -> dict:
    pin = json.loads((ROOT / "deploy/engines/file-asr-audio/artifact-pin.json").read_text())
    runtime = json.loads(
        (
            ROOT / "deploy/engines/vllm-asr-lifecycle/src/speechnexus_vllm_asr/runtime-pin.json"
        ).read_text()
    )
    definition = json.loads(
        (ROOT / "deploy/engines/file-asr-audio/binding-definition.json").read_text()
    )
    artifacts = [entry["artifact"] for entry in pin["model"]["files"]]
    artifacts.append(pin["vad"]["artifact"])
    return {
        "binding_id": BINDING_ID,
        "logical_model": LOGICAL_MODEL,
        "tasks": definition["tasks"],
        "engine": {
            "kind": "vllm",
            "image_digest": runtime["image"],
            "runtime_version": runtime["version"],
        },
        "artifact_set": artifacts,
        "execution_recipe": {
            "recipe_revision": definition["execution_recipe"]["recipe_revision"],
            "capacity_manifest_id": CAPACITY_MANIFEST_ID,
            "stage_roles": definition["execution_recipe"]["stage_roles"],
            "requires_complete_reservation": definition["execution_recipe"].get(
                "requires_complete_reservation", True
            ),
        },
        "parameter_domains": definition["parameter_domains"],
        "execution_defaults": {
            "asr": {
                "language": "auto",
                "turn_detection": {
                    "mode": "server_vad",
                    "silence_duration_ms": recipe["asr"]["silence_duration_ms"],
                },
                "finalization": "full_segment",
                "preview_interval_ms": recipe["asr"]["preview_interval_ms"],
                "max_segment_ms": 20000,
                "rollback_tokens": 0,
                "final_deadline_ms": recipe["asr"]["final_deadline_ms"],
                "auxiliary_deadline_ms": recipe["asr"]["auxiliary_deadline_ms"],
                "alignment": "off",
                "diarization": "off",
                "vocabulary_set_revision": None,
                "temperature": recipe["asr"]["temperature"],
                "timeout_seconds": 120,
            }
        },
        "release_tier": "experimental",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--gateway-endpoint", type=Path)
    arguments = parser.parse_args()

    api = arguments.api.rstrip("/")
    recipe = seal_recipe(build_recipe(arguments.recipe))
    draft = build_draft(recipe)

    status, body = request_json("POST", f"{api}/v1/nexus/admin/model-bindings", draft)
    if status != 201:
        print(f"draft failed: {status} {json.dumps(body)[:400]}", file=sys.stderr)
        return 1
    revision = body["revision"]
    print(f"draft revision {revision}")

    listing_status, listing = request_json("GET", f"{api}/v1/nexus/admin/model-bindings", None)
    head = None
    if listing_status == 200:
        for binding in listing.get("bindings", listing if isinstance(listing, list) else []):
            if isinstance(binding, dict) and binding.get("binding_id") == BINDING_ID:
                revisions = binding.get("revisions") or []
                published = [item for item in revisions if item.get("state") == "published"]
                if published:
                    head = published[-1].get("revision")
    print(f"current published head: {head}")

    publication = {
        "expected_revision": revision,
        "expected_current_revision": head,
        "capacity_manifest_id": CAPACITY_MANIFEST_ID,
        "qualification_report_id": QUALIFICATION_REPORT_ID,
    }
    status, body = request_json(
        "POST", f"{api}/v1/nexus/admin/model-bindings/{BINDING_ID}/publications", publication
    )
    if status != 201:
        print(f"publication failed: {status} {json.dumps(body)[:400]}", file=sys.stderr)
        return 1
    revision_id = body["revision_id"]
    print(f"published revision id {revision_id}")

    status, body = request_json(
        "POST",
        f"{api}/v1/nexus/admin/model-bindings/{BINDING_ID}/revisions/{revision}/activations",
        {"expected_current_revision": revision},
    )
    if status != 200:
        print(f"activation failed: {status} {json.dumps(body)[:400]}", file=sys.stderr)
        return 1
    print("activated")

    recipe = bind_revision(recipe, revision_id)
    recipe["binding_publication_id"] = revision_id
    sealed = seal_recipe({key: value for key, value in recipe.items() if key != "recipe_digest"})
    arguments.recipe.parent.mkdir(parents=True, exist_ok=True)
    arguments.recipe.write_text(json.dumps(sealed, indent=2) + "\n", encoding="utf-8")
    arguments.recipe.chmod(0o600)
    print(f"recipe written to {arguments.recipe}")

    if arguments.gateway_endpoint is not None and arguments.gateway_endpoint.exists():
        endpoint = json.loads(arguments.gateway_endpoint.read_text(encoding="utf-8"))
        endpoint["target"]["binding_revision_id"] = revision_id
        endpoint["target"]["instance_id"] = INSTANCE_ID
        endpoint["target"]["host_node_id"] = NODE_ID
        endpoint["target"]["gateway_id"] = GATEWAY_ID
        arguments.gateway_endpoint.write_text(json.dumps(endpoint, indent=2) + "\n")
        print(f"gateway endpoint rebound to {revision_id}")

    print(
        json.dumps(
            {
                "binding_id": BINDING_ID,
                "logical_model": LOGICAL_MODEL,
                "revision": revision,
                "revision_id": revision_id,
                "capacity_manifest_id": CAPACITY_MANIFEST_ID,
                "qualification_report_id": QUALIFICATION_REPORT_ID,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
