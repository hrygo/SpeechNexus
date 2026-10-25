"""Audit original AC scope, source mappings and evidence; never change acceptance state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "docs/audits/2026-10-25-team-c-acceptance-map.json"
# Freeze scope independently of editable mappings. Change only with an authorized
# scope decision; implementation/evidence updates never change this baseline.
FROZEN_SCOPE_SHA256 = "14806c9db4c599636c579f1e9609fe4cbdeea76fd3c7105577215a5de48a7576"
DIMENSIONS = {
    "synthetic",
    "unit",
    "browser",
    "real_service",
    "persistence",
    "object_storage",
    "real_model",
    "physical_device",
    "assistive_technology",
    "target_deployment",
    "physical_cluster",
    "network_boundary",
    "load_duration",
}
IMPLEMENTATION_STATES = {
    "not_audited",
    "c_pending",
    "dependency_pending",
    "implemented",
    "verified",
}
ACCEPTANCE_STATES = {"not_run", "passed", "failed", "dependency_pending", "unknown"}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def local_file(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("reference must stay inside its declared root")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError("reference is absent or outside its declared root")
    return result


def original_scope(text: str) -> tuple[dict[str, str], dict[str, str]]:
    originals: dict[str, str] = {}
    conditions: dict[str, str] = {}
    for block in re.split(r"(?=^### C\d\d/AC\d+\s*$)", text, flags=re.MULTILINE):
        heading = re.match(r"### (C\d\d/AC\d+)\s*\n", block)
        if not heading:
            continue
        quoted = re.search(r"^> (.+)$", block, re.MULTILINE)
        if not quoted:
            raise ValueError(f"{heading[1]} has no original AC text")
        if heading[1] in originals:
            raise ValueError(f"duplicate original AC {heading[1]}")
        originals[heading[1]] = quoted[1]
        for identifier, requirement in re.findall(
            r"^- `(C\d\d/AC\d+/S\d+)`：(.+)$", block, re.MULTILINE
        ):
            if identifier in conditions:
                raise ValueError(f"duplicate original condition {identifier}")
            conditions[identifier] = requirement.rstrip("。")
    if not originals or not conditions:
        raise ValueError("original scope is empty")
    return originals, conditions


def reference_problem(reference: dict[str, Any], root: Path) -> str | None:
    try:
        path = local_file(root, reference["path"])
        expected = reference.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            return "missing or malformed file digest"
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            return "file digest changed; semantic revalidation is required"
        symbol = reference.get("symbol")
        if symbol is not None and (not symbol or symbol not in path.read_text()):
            return "symbol is absent"
    except OSError, ValueError, KeyError, TypeError:
        return "missing, malformed or escaping reference"
    return None


def metadata_problem(proof: dict[str, Any], evidence_root: Path) -> str | None:
    for key in ("metadata", "source_manifest"):
        if key in proof:
            problem = reference_problem(
                {
                    "path": proof[key],
                    "sha256": proof.get(key + "_sha256"),
                },
                evidence_root,
            )
            if problem:
                return f"{key}: {problem}"
    return None


def source_capture_matches(
    proof: dict[str, Any],
    item: dict[str, Any],
    root: Path,
    evidence_root: Path,
) -> bool:
    try:
        capture = json.loads(local_file(evidence_root, proof["source_manifest"]).read_text())
        if capture["source_head"] != proof["source_head"]:
            return False
        files = capture["files"]
        if not isinstance(files, dict) or not files:
            return False
        for path, expected in files.items():
            if hashlib.sha256(local_file(root, path).read_bytes()).hexdigest() != expected:
                return False
        referenced = {
            reference["path"]
            for key in ("implementation_refs", "consumer_refs", "test_refs")
            for reference in item.get(key, [])
        }
        return bool(referenced) and referenced <= files.keys()
    except OSError, ValueError, KeyError, TypeError:
        return False


def protected_inputs(matrix: Any, root: Path, evidence_root: Path) -> set[Path]:
    """Collect paths before shape validation, including malformed entries."""
    paths: set[Path] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"path", "metadata", "source_manifest", "source_document"} and isinstance(
                    child, str
                ):
                    paths.update((base / child).resolve() for base in (root, evidence_root))
                    if key == "source_manifest":
                        try:
                            capture = json.loads(local_file(evidence_root, child).read_text())
                            files = capture.get("files", {})
                            if isinstance(files, dict):
                                paths.update(
                                    (root / path).resolve()
                                    for path in files
                                    if isinstance(path, str)
                                )
                        except OSError, ValueError, TypeError, AttributeError:
                            pass  # Path is protected; validation reports malformed bytes.
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(matrix)
    return paths


def validate(
    matrix: dict[str, Any],
    root: Path,
    evidence_root: Path,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    def add(code: str, identifier: str, detail: str) -> None:
        findings.append({"code": code, "id": identifier, "detail": detail})

    originals, required = original_scope(local_file(root, matrix["source_document"]).read_text())
    frozen = sha256(
        json.dumps(
            [originals, required],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if frozen != FROZEN_SCOPE_SHA256:
        add(
            "scope_digest", "scope", "original scope differs from the independently frozen baseline"
        )
    if not isinstance(matrix.get("head_sha"), str) or not re.fullmatch(
        r"[0-9a-f]{40}", matrix["head_sha"]
    ):
        add("version_invalid", "scope", "head must be a full immutable commit SHA")
    entries = matrix["entries"]
    ac_ids = [entry["id"] for entry in entries]
    if len(ac_ids) != len(set(ac_ids)):
        add("duplicate_ac", "scope", "original AC identifiers must be unique")
    if set(ac_ids) != set(originals):
        add("ac_set", "scope", "matrix AC set differs from original scope")
    conditions = [item for entry in entries for item in entry["semantic_conditions"]]
    identifiers = [item["id"] for item in conditions]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(required):
        add("condition_set", "scope", "condition identifiers differ from original scope")
    for entry in entries:
        identifier = entry["id"]
        if entry["source_ac"] != originals.get(identifier):
            add("source_text", identifier, "original AC text differs")
        if sha256(entry["source_ac"]) != entry["source_sha256"]:
            add("source_digest", identifier, "original AC digest differs")
        if any(
            not item["id"].startswith(identifier + "/S") for item in entry["semantic_conditions"]
        ):
            add("condition_parent", identifier, "condition belongs to another AC")

    evidence = matrix["evidence_registry"]
    for identifier, item in evidence.items():
        if problem := reference_problem(item, evidence_root):
            add("evidence_invalid", identifier, problem)
        if problem := metadata_problem(item, evidence_root):
            add("evidence_invalid", identifier, problem)
        if set(item.get("dimensions", [])) - DIMENSIONS:
            add("qualification_invalid", identifier, "unknown evidence dimension")
    for item in conditions:
        identifier = item["id"]
        if item["requirement"].rstrip("。") != required.get(identifier):
            add("condition_text", identifier, "condition differs from its source decomposition")
        if item["implementation_status"] not in IMPLEMENTATION_STATES:
            add("implementation_state", identifier, "unknown implementation state")
        if item["acceptance_status"] not in ACCEPTANCE_STATES:
            add("acceptance_state", identifier, "unknown acceptance state")
        if not item.get("owner"):
            add("owner_missing", identifier, "condition needs an owner")
        dependencies = item.get("dependency_artifacts", [])
        for dependency in dependencies:
            if not all(
                dependency.get(key) for key in ("owner", "artifact", "unblock_condition", "impact")
            ):
                add(
                    "dependency_incomplete",
                    identifier,
                    "dependency needs owner, artifact, release and impact",
                )
        waiting = item["implementation_status"] == "dependency_pending"
        if waiting and not dependencies:
            add("dependency_incomplete", identifier, "pending dependency has no named artifact")
        for key in ("implementation_refs", "consumer_refs", "test_refs"):
            references = item.get(key, [])
            if not references and not waiting:
                add("mapping_missing", identifier, key)
            for reference in references:
                if problem := reference_problem(reference, root):
                    add(
                        "reference_invalid",
                        identifier,
                        f"{key}: {reference.get('path')}: {problem}",
                    )
        for reference in item.get("evidence_refs", []):
            if reference not in evidence:
                add("evidence_unknown", identifier, reference)
        dimensions = set(item.get("required_dimensions", []))
        if dimensions - DIMENSIONS:
            add("qualification_invalid", identifier, "unknown required dimension")
        if item["acceptance_status"] != "passed":
            continue
        review = item.get("acceptance_review", {})
        if not all(review.get(key) for key in ("reviewer", "date", "reason")):
            add("acceptance_review", identifier, "passing needs an explicit semantic review")
        if not dimensions:
            add("qualification_missing", identifier, "passing needs a defined qualification scope")
        if item["implementation_status"] not in {"implemented", "verified"}:
            add("evidence_insufficient", identifier, "incomplete implementation cannot pass")
        supporting = [evidence[key] for key in item.get("evidence_refs", []) if key in evidence]
        if not any(
            proof.get("result_status") == "passed"
            and proof.get("source_head") == matrix["head_sha"]
            and bool(proof.get("scope"))
            and dimensions <= set(proof.get("dimensions", []))
            and reference_problem(proof, evidence_root) is None
            and metadata_problem(proof, evidence_root) is None
            and source_capture_matches(proof, item, root, evidence_root)
            for proof in supporting
        ):
            add(
                "evidence_insufficient",
                identifier,
                "no passing proof at the declared version and dimensions",
            )
    return findings


def report(matrix: dict[str, Any], root: Path, evidence_root: Path) -> dict[str, Any]:
    findings = validate(matrix, root, evidence_root)
    conditions = [item for entry in matrix["entries"] for item in entry["semantic_conditions"]]
    counts = Counter(item["acceptance_status"] for item in conditions)
    return {
        "scope": "Acceptance-map audit; this does not release a Gate or complete the Goal.",
        "head_sha": matrix["head_sha"],
        "original_acs": len(matrix["entries"]),
        "conditions": len(conditions),
        "acceptance_counts": dict(counts),
        "finding_counts": dict(Counter(item["code"] for item in findings)),
        "accepted": not findings and bool(conditions) and counts["passed"] == len(conditions),
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    protected = {args.matrix.resolve()}
    try:
        matrix = json.loads(args.matrix.read_text())
        protected.update(protected_inputs(matrix, args.root, args.evidence_root))
        result = report(matrix, args.root, args.evidence_root)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"accepted": False, "findings": [{"code": "invalid_input", "detail": str(error)}]}
    if args.output is not None:
        alias = args.output.resolve() in protected
        if args.output.exists():
            alias = alias or any(path.exists() and args.output.samefile(path) for path in protected)
        if alias:
            parser.error("output must not overwrite any audited input")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "findings"}, ensure_ascii=False
        )
    )
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
