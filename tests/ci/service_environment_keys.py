"""Machine-readable registry of settings surfaces, prefixes and non-setting planes.

Issue #674: a variable that carries a service prefix but matches no declared
setting is dropped at load time, so a typo or a key placed in the wrong process
looks like a configured capability. The runtime rule (``speechnexus_backend
.environment``, mirrored in the gateway and node-controller packages) refuses the
name instead. That rule is only as good as the two things it depends on: the set
of settings a surface declares, and the set of prefixed names that are
legitimately *not* settings.

Both are derived here from the sources with :mod:`ast`, never copied by hand:

``SURFACES``
    Every settings class, its package, the operator name of the process that
    constructs it, and its module path. Discovery walks the four package roots
    for ``BaseSettings`` subclasses and for the hand-written ``_ENV_PREFIX``
    dataclass, so a new class is red until it is classified here.
``SERVICE_PREFIXES``
    The one environment prefix each service declares. A new
    ``model_config(env_prefix=...)`` is red until it is registered, and a
    registered prefix that no longer exists is red as well.
``RUNTIME_REGISTRIES``
    Where each service keeps its ``PLANES`` tuple. The guard reads that tuple
    from the source and checks it against the derived key sets and the token
    inventory, and compares the three copies of the scan engine by AST.
``ARTIFACTS``
    The files whose ``SPEECHNEXUS_``-prefixed tokens define the deployment
    inventory: Compose, the example env file, the CI workflow, the deployment
    scripts and the test harness. Every token found there must be a derived
    settings key or match a registered non-setting plane, so a new variable in
    any of them forces a classification decision.

The derived data answers the false-positive question directly: a name that the
repository itself puts into a container environment (``deploy/compose.yaml``)
must be a declared setting or a registered plane, otherwise the guard is red
before anyone ships a refusal that breaks that deployment.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BACKEND_PREFIX = "SPEECHNEXUS_"
WORKBENCH_PREFIX = "SPEECHNEXUS_WORKBENCH_"
GATEWAY_PREFIX = "SPEECHNEXUS_GATEWAY_"
CONTROLLER_PREFIX = "SPEECHNEXUS_CONTROLLER_"
INSPECTION_PREFIX = "SPEECHNEXUS_INSPECTION_"

PACKAGE_ROOTS = (
    "backend/src",
    "execution-gateway/src",
    "node-controller/src",
    "asset-inspector/src",
)

BACKEND_ENVIRONMENT = "backend/src/speechnexus_backend/environment.py"
GATEWAY_ENVIRONMENT = "execution-gateway/src/speechnexus_gateway/environment.py"
CONTROLLER_ENVIRONMENT = "node-controller/src/speechnexus_node_controller/environment.py"

# Runtime modules that carry a copy of the scan engine, and the constant in each
# that holds the non-setting registry for that surface.
RUNTIME_REGISTRIES: dict[str, tuple[str, str]] = {
    "backend": (BACKEND_ENVIRONMENT, "PLANES"),
    "gateway": (GATEWAY_ENVIRONMENT, "PLANES"),
    "controller": (CONTROLLER_ENVIRONMENT, "PLANES"),
}
WORKBENCH_REGISTRY = (BACKEND_ENVIRONMENT, "WORKBENCH_PLANES")

# The scan engine is duplicated on purpose (a package may not import another
# service); these are the definitions that must stay identical.
SHARED_DEFINITIONS = (
    "NonSettingPlane",
    "declared_environment_keys",
    "environment_prefixes",
    "_matching_plane",
    "close_candidates",
    "unknown_prefixed_variables",
    "refuse_unrecognised_prefixed_environment",
    "_describe_unknown",
)


@dataclass(frozen=True)
class Surface:
    """One settings class: which service owns it and which process constructs it."""

    id: str
    service: str
    entrypoint: str
    module: str
    cls: str
    kind: str = "settings"


@dataclass(frozen=True)
class ServicePrefix:
    """The environment namespace one service declares, and where it scans it."""

    service: str
    prefix: str
    scans: frozenset[str] = field(default_factory=frozenset)
    registry: tuple[str, str] | None = None


SURFACES: tuple[Surface, ...] = (
    Surface(
        "backend",
        "backend",
        "the speech backend process",
        "backend/src/speechnexus_backend/settings.py",
        "Settings",
    ),
    Surface(
        "workbench",
        "workbench",
        "the workbench service",
        "backend/src/speechnexus_backend/workbench/runtime_settings.py",
        "WorkbenchSettings",
    ),
    Surface(
        "gateway",
        "gateway",
        "the execution gateway service",
        "execution-gateway/src/speechnexus_gateway/settings.py",
        "Settings",
    ),
    Surface(
        "controller",
        "controller",
        "the node controller service",
        "node-controller/src/speechnexus_node_controller/settings.py",
        "Settings",
    ),
    Surface(
        "asset_inspector",
        "asset-inspector",
        "the asset-inspector service",
        "asset-inspector/src/speechnexus_asset_inspector/settings.py",
        "InspectionSettings",
        kind="from_env",
    ),
    Surface(
        "job_dispatch",
        "backend",
        "the job-dispatch worker entrypoint",
        "backend/src/speechnexus_backend/job_dispatch_runtime.py",
        "JobDispatchSettings",
    ),
    Surface(
        "binding_disable",
        "backend",
        "the binding-disable worker entrypoint",
        "backend/src/speechnexus_backend/binding_disable_worker.py",
        "BindingDisableWorkerSettings",
    ),
    Surface(
        "job_cancellation",
        "backend",
        "the job-cancellation worker entrypoint",
        "backend/src/speechnexus_backend/job_cancellation_runtime.py",
        "JobCancellationSettings",
    ),
    Surface(
        "runtime_delivery",
        "backend",
        "the runtime-delivery worker entrypoint",
        "backend/src/speechnexus_backend/runtime_delivery_worker.py",
        "RuntimeDeliverySettings",
    ),
    Surface(
        "tts_settlement",
        "backend",
        "the TTS settlement worker entrypoint",
        "backend/src/speechnexus_backend/tts_settlement_worker.py",
        "TtsSettlementWorkerSettings",
    ),
    Surface(
        "voice_evidence_worker",
        "backend",
        "the voice-evidence worker entrypoint",
        "backend/src/speechnexus_backend/voice_evidence_worker.py",
        "VoiceEvidenceWorkerSettings",
    ),
    Surface(
        "record_evidence",
        "backend",
        "the voice-capability evidence command line",
        "backend/src/speechnexus_backend/record_voice_capability_evidence.py",
        "VoiceEvidenceSettings",
    ),
    Surface(
        "binding_publish",
        "backend",
        "the binding publication command line",
        "backend/src/speechnexus_backend/publish_binding.py",
        "BindingPublishSettings",
    ),
    Surface(
        "source_approval",
        "backend",
        "the source-approval command line",
        "backend/src/speechnexus_backend/approve_builtin_voice_source.py",
        "SourceApprovalSettings",
    ),
    Surface(
        "revoke_voice",
        "backend",
        "the builtin-voice revocation command line",
        "backend/src/speechnexus_backend/revoke_builtin_voice.py",
        "RevocationSettings",
    ),
    Surface(
        "qualification",
        "backend",
        "the builtin-voice qualification command line",
        "backend/src/speechnexus_backend/run_builtin_voice_qualification.py",
        "QualificationSettings",
    ),
    Surface(
        "recover_tts",
        "backend",
        "the TTS recovery command line",
        "backend/src/speechnexus_backend/recover_tts.py",
        "TtsRecoverySettings",
    ),
)

SERVICE_PREFIXES: tuple[ServicePrefix, ...] = (
    ServicePrefix(
        service="backend",
        prefix=BACKEND_PREFIX,
        scans=frozenset({"backend", "gateway", "workbench", "controller", "asset-inspector"}),
        registry=RUNTIME_REGISTRIES["backend"],
    ),
    ServicePrefix(
        service="workbench",
        prefix=WORKBENCH_PREFIX,
        scans=frozenset({"self"}),
        registry=WORKBENCH_REGISTRY,
    ),
    ServicePrefix(
        service="gateway",
        prefix=GATEWAY_PREFIX,
        scans=frozenset({"self"}),
        registry=RUNTIME_REGISTRIES["gateway"],
    ),
    ServicePrefix(
        service="controller",
        prefix=CONTROLLER_PREFIX,
        scans=frozenset({"self"}),
        registry=RUNTIME_REGISTRIES["controller"],
    ),
    ServicePrefix(service="asset-inspector", prefix=INSPECTION_PREFIX),
)

# Deployment-plane sources. A prefixed token that appears in any of these is a
# real variable some deployment or check uses, so refusing it would be a
# regression: it must be a derived settings key or a registered plane.
ARTIFACTS = (
    "deploy/compose.yaml",
    "deploy/compose.env.example",
    "deploy/init-object-storage.py",
    "deploy/job-startup-gate.py",
    ".github/workflows/ci.yml",
    "scripts/dev-env",
    "tests/integration",
)

TOKEN = "SPEECHNEXUS_[A-Z0-9_]+"


def read(path: str | Path) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def artifact_files() -> tuple[Path, ...]:
    """Return every file under :data:`ARTIFACTS`, expanded to concrete paths."""
    files: list[Path] = []
    for entry in ARTIFACTS:
        target = ROOT / entry
        if target.is_dir():
            files.extend(sorted(p for p in target.rglob("*") if p.is_file()))
        else:
            files.append(target)
    return tuple(files)


def inventory_tokens() -> frozenset[str]:
    """Return every ``SPEECHNEXUS_``-prefixed token the declared artifacts mention."""
    import re

    pattern = re.compile(TOKEN)
    tokens: set[str] = set()
    for path in artifact_files():
        if path.suffix not in {
            ".py",
            ".md",
            ".yml",
            ".yaml",
            ".json",
            ".example",
            ".sh",
            ".ts",
            "",
        }:
            continue
        for token in pattern.findall(path.read_text(encoding="utf-8", errors="ignore")):
            if not token.endswith("_"):
                tokens.add(token)
    return frozenset(tokens)


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, (ast.AnnAssign, ast.Assign)):
        return _statement_name(node)
    return None


def _statement_value(statement: ast.stmt) -> ast.AST | None:
    """Return the assigned expression of an annotated or plain assignment."""
    if isinstance(statement, (ast.AnnAssign, ast.Assign)):
        return statement.value
    return None


def _statement_name(statement: ast.stmt) -> str | None:
    """Return the single target name of an assignment, or ``None``."""
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


def _constant(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _flag(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _keyword(call: ast.AST, name: str) -> ast.AST | None:
    if not isinstance(call, ast.Call):
        return None
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


#: Modules read as text because this interpreter cannot parse their source.
#:
#: ``pyproject.toml`` targets Python 3.14, so product sources use PEP 758
#: exception lists (``except A, B:``), which the system interpreter CI runs this
#: file with (3.12) rejects. Every read that falls back to text is recorded here:
#: the tests assert the record holds exactly the modules the local parser
#: rejects, that each recorded module still yields its declarations and keys, and
#: that a settings-bearing module whose text has no extractable declaration fails
#: loudly instead of being skipped.
TEXT_READS: set[str] = set()

#: Modules whose settings declarations came from the text reader during
#: discovery, separately from :data:`TEXT_READS` (which records the key/prefix
#: reads). Both are asserted against the modules this interpreter rejects, so a
#: fallback that runs without being recorded, or a recorded fallback that yields
#: nothing, is red.
DISCOVERY_TEXT_READS: set[str] = set()

_SETTINGS_MARKERS = ("BaseSettings", "_ENV_PREFIX")
_CLASS_LINE = re.compile(r"^class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:", re.MULTILINE)
_FIELD_LINE = re.compile(r"^ {4}(\w+)\s*:", re.MULTILINE)
_ALIAS_ARGUMENT = re.compile(r"""validation_alias\s*=\s*["']([A-Za-z0-9_]+)["']""")
_ENV_PREFIX_ASSIGNMENT = re.compile(r"""^_ENV_PREFIX\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_ENV_PREFIX_CONCAT = re.compile(r"""_ENV_PREFIX\s*\+\s*["']([A-Za-z0-9_]+)["']""")
_ENV_PREFIX_CONFIG = re.compile(r"""env_prefix\s*=\s*["']([^"']+)["']""")
_FROM_ENV_METHOD = re.compile(r"^\s+def from_env\(", re.MULTILINE)

_PEP758_PROBE = "try:\n    pass\nexcept ValueError, TypeError:\n    pass\n"


def parse_source(source: str) -> ast.Module | None:
    """Return the AST, or ``None`` when this interpreter cannot read the source."""
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def can_parse_pep758() -> bool:
    """Whether this interpreter accepts the PEP 758 exception list used by the sources."""
    return parse_source(_PEP758_PROBE) is not None


def _unreadable(path: str) -> AssertionError:
    return AssertionError(
        f"{path} cannot be parsed by this interpreter "
        f"(Python {sys.version_info.major}.{sys.version_info.minor}) and no text "
        "reader covers it; refusing to skip a settings source silently"
    )


def _class_blocks(source: str) -> list[tuple[str, str, str]]:
    """Return ``(name, bases, block)`` for every top-level class, by indentation."""
    lines = source.splitlines()
    blocks: list[tuple[str, str, str]] = []
    for index, line in enumerate(lines):
        match = _CLASS_LINE.match(line)
        if match is None:
            continue
        end = len(lines)
        for following in range(index + 1, len(lines)):
            candidate = lines[following]
            if candidate and not candidate[0].isspace() and not candidate.startswith("#"):
                end = following
                break
        blocks.append((match.group(1), match.group(2) or "", "\n".join(lines[index:end])))
    return blocks


def _class_block(source: str, cls: str, label: str) -> str:
    for name, _, block in _class_blocks(source):
        if name == cls:
            return block
    raise AssertionError(f"{cls} is not declared in {label}")


def declarations_from_ast(tree: ast.Module, source: str) -> frozenset[str]:
    """Return the settings class names of a parsed module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "BaseSettings":
                names.add(node.name)
            if isinstance(base, ast.Attribute) and base.attr == "BaseSettings":
                names.add(node.name)
    if "_ENV_PREFIX = " in source:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(item, ast.FunctionDef) and item.name == "from_env" for item in node.body
            ):
                names.add(node.name)
    return frozenset(names)


def declarations_from_text(source: str) -> frozenset[str]:
    """Return the settings class names of a module this interpreter cannot parse."""
    blocks = _class_blocks(source)
    names = {name for name, bases, _ in blocks if "BaseSettings" in bases}
    if "_ENV_PREFIX = " in source:
        names.update(name for name, _, block in blocks if _FROM_ENV_METHOD.search(block))
    return frozenset(names)


def declared_settings_classes(source: str, label: str) -> frozenset[str]:
    """Return the settings class names in ``source``, parsed or read as text."""
    tree = parse_source(source)
    if tree is not None:
        return declarations_from_ast(tree, source)
    names = declarations_from_text(source)
    if not names:
        raise AssertionError(
            f"{_unreadable(label).args[0]} "
            "and the text reader found no settings declaration; refusing to skip it silently"
        )
    return names


def keys_from_text(source: str, cls: str, kind: str, label: str) -> dict[str, str]:
    """Return ``{field: environment name}`` for a surface this interpreter cannot parse."""
    block = _class_block(source, cls, label)
    if kind == "from_env":
        assignment = _ENV_PREFIX_ASSIGNMENT.search(source)
        prefix = assignment.group(1) if assignment else ""
        return {
            suffix.lower(): f"{prefix}{suffix}" for suffix in _ENV_PREFIX_CONCAT.findall(source)
        }
    config = _ENV_PREFIX_CONFIG.search(block)
    prefix = config.group(1) if config else ""
    chunks: list[tuple[str, str]] = []
    for line in block.splitlines():
        match = _FIELD_LINE.match(line)
        if match is not None:
            chunks.append((match.group(1), line))
        elif chunks:
            chunks[-1] = (chunks[-1][0], f"{chunks[-1][1]}\n{line}")
    keys: dict[str, str] = {}
    for name, chunk in chunks:
        if name == "model_config":
            continue
        alias = _ALIAS_ARGUMENT.search(chunk)
        keys[name] = alias.group(1) if alias else f"{prefix}{name.upper()}"
    return keys


def prefix_from_text(source: str, cls: str, kind: str, label: str) -> str:
    """Return the declared prefix of a surface this interpreter cannot parse."""
    if kind == "from_env":
        assignment = _ENV_PREFIX_ASSIGNMENT.search(source)
        if assignment:
            return assignment.group(1)
        raise AssertionError(f"{cls} in {label} declares no _ENV_PREFIX")
    config = _ENV_PREFIX_CONFIG.search(_class_block(source, cls, label))
    if config:
        return config.group(1)
    raise AssertionError(f"{cls} in {label} declares no env_prefix")


def _class_node(tree: ast.Module, label: str, cls: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return node
    raise AssertionError(f"{cls} is not declared in {label}")


def derived_keys(surface: Surface) -> dict[str, str]:
    """Return ``{field: environment name}`` derived from the surface's source."""
    source = read(surface.module)
    tree = parse_source(source)
    if tree is None:
        TEXT_READS.add(surface.module)
        keys = keys_from_text(source, surface.cls, surface.kind, surface.module)
        if not keys:
            raise AssertionError(
                f"{surface.id}: {_unreadable(surface.module).args[0]} "
                "and the text reader derived no key; refusing to check an empty key set"
            )
        return keys
    return keys_from_ast(tree, surface, source)


def keys_from_ast(tree: ast.Module, surface: Surface, source: str) -> dict[str, str]:
    node = _class_node(tree, surface.module, surface.cls)
    if surface.kind == "from_env":
        prefix = ""
        for statement in tree.body:
            if _name_of(statement) == "_ENV_PREFIX":
                prefix = _constant(_statement_value(statement)) or ""
        keys: dict[str, str] = {}
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            for argument in call.args:
                if (
                    isinstance(argument, ast.BinOp)
                    and isinstance(argument.op, ast.Add)
                    and isinstance(argument.left, ast.Name)
                    and argument.left.id == "_ENV_PREFIX"
                ):
                    suffix = _constant(argument.right)
                    if suffix:
                        keys[suffix.lower()] = f"{prefix}{suffix}"
        return keys
    prefix = ""
    for statement in node.body:
        config = _statement_value(statement)
        if _name_of(statement) == "model_config" and config is not None:
            for call in ast.walk(config):
                value = _keyword(call, "env_prefix")
                if value is not None:
                    prefix = _constant(value) or ""
    keys = {}
    for statement in node.body:
        if not isinstance(statement, ast.AnnAssign):
            continue
        target = _name_of(statement)
        if target in (None, "model_config"):
            continue
        alias = None
        if statement.value is not None:
            for call in ast.walk(statement.value):
                value = _keyword(call, "validation_alias")
                if value is not None:
                    alias = _constant(value)
        keys[target] = alias or f"{prefix}{target.upper()}"
    return keys


def surface_keys() -> dict[str, frozenset[str]]:
    """Return ``{surface id: derived environment keys}`` for every registered surface."""
    return {surface.id: frozenset(derived_keys(surface).values()) for surface in SURFACES}


def service_keys(service: str) -> frozenset[str]:
    """Return every derived key owned by ``service``."""
    keys: set[str] = set()
    for surface in SURFACES:
        if surface.service == service:
            keys.update(derived_keys(surface).values())
    return frozenset(keys)


def declared_prefix(service: str) -> str:
    """Return the environment prefix ``service`` declares.

    Derived from ``model_config`` for pydantic surfaces and from ``_ENV_PREFIX``
    for the hand-written one, so a new prefix cannot appear silently.
    """
    surface = next(item for item in SURFACES if item.service == service)
    source = read(surface.module)
    tree = parse_source(source)
    if tree is None:
        TEXT_READS.add(surface.module)
        return prefix_from_text(source, surface.cls, surface.kind, surface.module)
    return prefix_from_ast(tree, surface.cls, surface.kind, surface.module)


def prefix_from_ast(tree: ast.Module, cls: str, kind: str, label: str) -> str:
    """Return the prefix a parsed surface declares."""
    if kind == "from_env":
        for statement in tree.body:
            if _name_of(statement) == "_ENV_PREFIX":
                return _constant(_statement_value(statement)) or ""
        raise AssertionError(f"{cls} in {label} declares no _ENV_PREFIX")
    node = _class_node(tree, label, cls)
    for statement in node.body:
        config = _statement_value(statement)
        if _name_of(statement) == "model_config" and config is not None:
            for call in ast.walk(config):
                value = _keyword(call, "env_prefix")
                if value is not None:
                    return _constant(value) or ""
    raise AssertionError(f"{cls} in {label} declares no env_prefix")


def discovered_settings_classes() -> frozenset[tuple[str, str]]:
    """Return ``{(module, class)}`` for every settings surface found under the roots."""
    found: set[tuple[str, str]] = set()
    for root in PACKAGE_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if not any(marker in source for marker in _SETTINGS_MARKERS):
                # Cannot declare a settings surface. Skipping the parse keeps this
                # file runnable on the system interpreter, which cannot read the
                # PEP 758 sources the product targets.
                continue
            label = path.relative_to(ROOT).as_posix()
            if parse_source(source) is None:
                DISCOVERY_TEXT_READS.add(label)
            for name in declared_settings_classes(source, label):
                found.add((label, name))
    return frozenset(found)


def discovered_prefixes() -> frozenset[str]:
    """Return every environment prefix declared by a source under the package roots."""
    prefixes: set[str] = set()
    for root in PACKAGE_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "_ENV_PREFIX" not in source and "env_prefix" not in source:
                continue
            label = path.relative_to(ROOT).as_posix()
            tree = parse_source(source)
            if tree is None:
                TEXT_READS.add(label)
                assignment = _ENV_PREFIX_ASSIGNMENT.search(source)
                if assignment:
                    prefixes.add(assignment.group(1))
                for _, _, block in _class_blocks(source):
                    for declared in _ENV_PREFIX_CONFIG.findall(block):
                        prefixes.add(declared)
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and any(
                    isinstance(item, ast.FunctionDef) and item.name == "from_env"
                    for item in node.body
                ):
                    for statement in tree.body:
                        if _name_of(statement) == "_ENV_PREFIX":
                            value = _constant(_statement_value(statement))
                            if value:
                                prefixes.add(value)
            for call in ast.walk(tree):
                prefix_node = _keyword(call, "env_prefix")
                declared = _constant(prefix_node)
                if declared:
                    prefixes.add(declared)
    return frozenset(prefixes)


@dataclass(frozen=True)
class RegisteredPlane:
    """One ``NonSettingPlane`` as declared in a service module, read by AST."""

    prefixes: tuple[str, ...]
    suffixes: tuple[str, ...]
    keys: tuple[str, ...]
    owner: str
    runtime: bool

    def matches(self, name: str) -> bool:
        """Return whether ``name`` is covered by this plane, exactly like the runtime."""
        if name in self.keys:
            return True
        return any(name.startswith(prefix) for prefix in self.prefixes) or any(
            name.endswith(suffix) for suffix in self.suffixes
        )


def registered_planes(module: str, constant: str) -> tuple[RegisteredPlane, ...]:
    """Return the planes declared in ``constant`` of ``module``, read from the source."""
    tree = parse_source(read(module))
    if tree is None:
        raise _unreadable(module)
    for statement in tree.body:
        if _name_of(statement) != constant:
            continue
        value = getattr(statement, "value", None)
        if not isinstance(value, ast.Tuple):
            raise AssertionError(f"{constant} in {module} is not a tuple literal")
        planes: list[RegisteredPlane] = []
        for element in value.elts:
            if not isinstance(element, ast.Call):
                raise AssertionError(f"{constant} in {module} holds a non-call element")
            planes.append(
                RegisteredPlane(
                    prefixes=_string_tuple(_keyword(element, "prefixes")),
                    suffixes=_string_tuple(_keyword(element, "suffixes")),
                    keys=_string_tuple(_keyword(element, "keys")),
                    owner=_constant(_keyword(element, "owner")) or "",
                    runtime=_flag(_keyword(element, "runtime")),
                )
            )
        return tuple(planes)
    raise AssertionError(f"{constant} is not declared in {module}")


def _string_tuple(node: ast.AST | None) -> tuple[str, ...]:
    if not isinstance(node, ast.Tuple):
        return ()
    return tuple(value for value in (_constant(item) for item in node.elts) if value)


def all_planes() -> tuple[RegisteredPlane, ...]:
    """Return every plane registered by any surface, for inventory classification."""
    planes: list[RegisteredPlane] = list(registered_planes(*WORKBENCH_REGISTRY))
    for module, constant in RUNTIME_REGISTRIES.values():
        planes.extend(registered_planes(module, constant))
    return tuple(planes)
