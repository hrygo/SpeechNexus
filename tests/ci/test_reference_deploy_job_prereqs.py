"""Guard the reference deployment's asynchronous Job prerequisites against drift.

`deploy/compose.yaml` can render and start while the deployment still cannot run a
single asynchronous Job: the image build may not include every workspace member,
`minio-init` may stop creating the bucket the gateway writes staging results to,
or the dispatcher container may start the worker before anything checks the
post-claim grants, the migration head or the two Job buckets.

This module parses text only, like the other ``tests/ci`` guards. It runs under
the system interpreter and never imports the backend, builds an image, connects
to PostgreSQL or starts a container. Mutations that must turn it red:

- removing the execution-result bucket from ``deploy/init-object-storage.py``;
- removing the result-bucket probe from ``deploy/job-startup-gate.py`` or
  unwiring the gate from the dispatcher entrypoint;
- dropping a grant group for one role from the provisioning path (the call is
  matched per role, so a group that survives only on another role is red) or
  from its documentation;
- dropping a workspace member's source ``COPY`` line (the guard requires the
  metadata and the source line, not just one occurrence of the member name).

The behaviour that cannot be proven from text - every check runs, a failure
exits 1, a failure never ``exec``s and a clean run does - is proven by
``tests/unit/test_job_startup_gate.py``, which calls ``main()`` with injected
collaborators. The text guard here only requires that the wiring it can see still
exists and that the exit happens after the failure branch.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/compose.yaml"
ENV_EXAMPLE = ROOT / "deploy/compose.env.example"
INIT_STORAGE = ROOT / "deploy/init-object-storage.py"
PYTHON_IMAGE = ROOT / "deploy/images/python.Dockerfile"
GATE = ROOT / "deploy/job-startup-gate.py"
PROVISION = ROOT / "scripts/dev-env/provision-job-roles.py"
MINIMAL_DOC = ROOT / "docs/operations/compose-minimal.md"
RUNBOOK = ROOT / "docs/operations/async-job-runbook.md"
PROJECT = ROOT / "pyproject.toml"

DISPATCH_GRANT_GROUPS = (
    "job_runtime_grants",
    "job_dispatch_grants",
    "public_job_grants",
    "cancellation_reconciliation_grants",
    "coordination_runtime_grants",
)
JOB_DSNS = (
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL",
)
ARTIFACT = "ARTIFACT_BUCKET_ENV"
RESULT = "RESULT_BUCKET_ENV"
WORKSPACE_MEMBER = re.compile(r"^members\s*=\s*\[(?P<members>[^\]]*)\]", re.MULTILINE)
SERVICE_HEADER = re.compile(r"(?m)^  (?P<name>[a-z][a-z0-9-]*):\n")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def service_block(document: str, name: str) -> str:
    """Return one Compose service block without its siblings."""
    header = re.search(rf"(?m)^  {re.escape(name)}:\n", document)
    if header is None:
        return ""
    following = SERVICE_HEADER.search(document, header.end())
    end = following.start() if following is not None else len(document)
    return document[header.start() : end]


def workspace_members() -> list[str]:
    match = WORKSPACE_MEMBER.search(read(PROJECT))
    if match is None:
        raise AssertionError("pyproject.toml declares no uv workspace members")
    return re.findall(r'"([^"]+)"', match.group("members"))


class ReferenceDeployBuildTests(unittest.TestCase):
    def test_the_python_image_copies_every_uv_workspace_member(self) -> None:
        # The image runs `uv sync --all-packages`, which resolves every workspace
        # member. A member whose metadata is missing fails the build; a member whose
        # src is missing still builds (an empty wheel) and then fails at import, so
        # both COPY lines are required, not just one occurrence of the name.
        dockerfile = read(PYTHON_IMAGE)
        missing = []
        for member in workspace_members():
            metadata = re.search(
                rf"(?m)^COPY [^\n]*\b{re.escape(member)}/(pyproject\.toml|README\.md)\b",
                dockerfile,
            )
            sources = re.search(
                rf"(?m)^COPY [^\n]*\b{re.escape(member)}/src\b",
                dockerfile,
            )
            if metadata is None:
                missing.append(f"{member}: metadata COPY")
            if sources is None:
                missing.append(f"{member}: src COPY")
        self.assertEqual(
            missing,
            [],
            "deploy/images/python.Dockerfile must COPY every uv workspace member's "
            f"metadata and src: {missing}",
        )


class ObjectStorageInitTests(unittest.TestCase):
    def bucket_block(self) -> str:
        source = read(INIT_STORAGE)
        match = re.search(r"(?ms)^    buckets = \{(?P<body>.*?)^    \}", source)
        self.assertIsNotNone(match, "init-object-storage.py declares no buckets mapping")
        assert match is not None
        return match.group("body")

    def test_the_execution_result_bucket_is_created_and_versioned(self) -> None:
        # The gateway writes staging results into the private configs'
        # `objects.result_bucket`; the dispatcher reads them back from
        # SPEECHNEXUS_EXECUTION_RESULT_BUCKET. Without this entry the bucket is
        # missing and the first publication fails after a Job was claimed.
        block = self.bucket_block()
        self.assertIn("SPEECHNEXUS_EXECUTION_RESULT_BUCKET", block)
        self.assertIn("SPEECHNEXUS_OBJECT_STORAGE_BUCKET", block)
        self.assertIn("SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET", block)
        source = read(INIT_STORAGE)
        self.assertIn("set_bucket_versioning(bucket, VersioningConfig(ENABLED))", source)
        self.assertIn("bucket ready: {bucket} (versioning enabled)", source)

    def test_minio_init_receives_the_same_result_bucket_variable(self) -> None:
        block = service_block(read(COMPOSE), "minio-init")
        self.assertIn(
            "SPEECHNEXUS_EXECUTION_RESULT_BUCKET: "
            "${SPEECHNEXUS_EXECUTION_RESULT_BUCKET:-speechnexus-results}",
            block,
        )


class DispatcherStartupGateTests(unittest.TestCase):
    def test_the_dispatcher_entrypoint_is_the_gate_not_the_worker(self) -> None:
        worker = service_block(read(COMPOSE), "job-dispatch-worker")
        self.assertIn('entrypoint: ["python", "/opt/speechnexus/job-startup-gate.py"]', worker)
        self.assertIn('command: ["speechnexus-job-dispatch-worker"]', worker)
        self.assertIn("./job-startup-gate.py:/opt/speechnexus/job-startup-gate.py:ro", worker)
        self.assertIn(
            "${SPEECHNEXUS_PRIVATE_CONFIG_DIR:?set SPEECHNEXUS_PRIVATE_CONFIG_DIR in compose.env}"
            ":/run/speechnexus-private:ro",
            worker,
        )
        self.assertIn("SPEECHNEXUS_JOB_GATE_PRIVATE_DIR: /run/speechnexus-private", worker)

    def test_the_gate_probes_both_job_buckets_before_the_worker_runs(self) -> None:
        # Deleting the result-bucket probe must turn this test red: that probe is
        # what makes a missing/unversioned execution bucket fail before a claim
        # instead of at the first result publication.
        gate = read(GATE)
        self.assertIn('RESULT_BUCKET_ENV = "SPEECHNEXUS_EXECUTION_RESULT_BUCKET"', gate)
        self.assertIn('ARTIFACT_BUCKET_ENV = "SPEECHNEXUS_OBJECT_STORAGE_BUCKET"', gate)
        references = re.search(r"(?ms)def referenced_buckets\(.*?return \(", gate)
        self.assertIsNotNone(references, "the gate must collect the referenced buckets")
        assert references is not None
        self.assertIn(ARTIFACT, references.group(0))
        self.assertIn(RESULT, references.group(0))
        self.assertIn("client.bucket_exists(bucket.name)", gate)
        self.assertIn("client.get_bucket_versioning(bucket.name)", gate)
        self.assertIn("versioning.status != ENABLED", gate)

    def test_the_gate_execs_only_after_the_failure_branch(self) -> None:
        # The exact ordering is proven behaviourally by
        # tests/unit/test_job_startup_gate.py; a text guard can at least prove the
        # exec is delegated to the injected callable and sits after `if failures:`.
        gate = read(GATE)
        self.assertIn("execute: Callable[[str, Sequence[str]], Any]", gate)
        self.assertIn("execute(command[0], command)", gate)
        self.assertNotIn("os.execvp(command[0], command)", gate)
        failure_branch = gate.index("if failures:")
        exec_call = gate.index("execute(command[0], command)")
        self.assertLess(failure_branch, exec_call)
        self.assertIn("return 1", gate[failure_branch:exec_call])

    def test_the_gate_rejects_a_lane_the_bootstrap_does_not_assemble(self) -> None:
        # A kind inside the dispatchable union but outside the assembled lanes used
        # to be a `pending` line; the worker then claimed it and failed after claim.
        gate = read(GATE)
        self.assertIn("def gateway_lane_findings(", gate)
        self.assertIn("DISPATCHABLE_JOB_KIND_UNION", gate)
        self.assertIn("assembles no gateway lane", gate)
        self.assertNotIn("is not assembled by this bootstrap; its result_bucket", gate)
        self.assertIn("GATEWAY_CONFIGS", gate)
        self.assertIn('"asr": "gateway-file-asr.json"', gate)
        self.assertIn('"tts": "gateway-file-tts.json"', gate)

    def test_the_gate_covers_the_post_claim_write_columns(self) -> None:
        # Every relation the dispatcher writes after a claim must be named: the Job
        # transition, the result Artifact publication and the capacity release.
        gate = read(GATE)
        self.assertIn("has_column_privilege", gate)
        for table in (
            "speech_jobs",
            "coordination_jobs",
            "speech_job_events",
            "speech_job_cancellation_not_submitted",
            "speech_artifacts",
            "admission_leases",
            "admission_attempts",
            "workspace_quotas",
            "host_nodes",
            "engine_instances",
        ):
            self.assertIn(f'"{table}"', gate)
        self.assertIn("ARTIFACT_INSERT_COLUMNS", gate)
        self.assertIn("RELEASE_WRITE_TABLES", gate)
        self.assertIn("DISPATCH_STARTUP_PROBES", gate)
        self.assertIn("DISPATCH_VOICE_VALIDATION_PROBES", gate)
        self.assertIn("DISPATCH_ROLE_GUARD_SQL", gate)

    def test_the_gate_fails_closed_on_an_unreadable_migration_head(self) -> None:
        gate = read(GATE)
        self.assertIn("upgrade prerequisite", gate)
        self.assertIn("GRANT SELECT ON {table} TO {target}", gate)
        self.assertNotIn("migration head not verifiable", gate)

    def test_the_gate_names_the_external_engine_and_api_dsns_instead_of_failing(self) -> None:
        gate = read(GATE)
        self.assertIn("real engine and a registered ready instance are a deployment", gate)
        self.assertIn("503 job_unavailable", gate)
        self.assertIn("pending:", gate)

    def test_the_gate_reports_the_migration_profile(self) -> None:
        gate = read(GATE)
        self.assertIn("alembic_version", gate)
        self.assertIn("--profile migration run --rm migrate", gate)


class JobRoleProvisioningTests(unittest.TestCase):
    def statements_body(self) -> str:
        source = read(PROVISION)
        match = re.search(r"(?ms)^def statements\(.*?(?=^def |\Z)", source)
        self.assertIsNotNone(match, "provisioning script declares no statements()")
        assert match is not None
        return match.group(0)

    def test_each_role_installs_its_own_grant_groups(self) -> None:
        # Matching the role argument (not just the group name) is what makes a
        # group that survives only on another role red: the gate checks the
        # dispatch role, so a dispatch call removed is a real regression.
        body = self.statements_body()
        required = {
            "writer": (
                "job_runtime_grants",
                "job_dispatch_grants",
                "public_job_grants",
                "voice_draft_grants",
                "public_job_submission_grants",
                "job_cancellation_fence_grants",
                "cancellation_not_submitted_grants",
                "cancellation_reconciliation_grants",
            ),
            "dispatch": (
                "job_runtime_grants",
                "job_dispatch_grants",
                "public_job_grants",
                "cancellation_reconciliation_grants",
            ),
            "admission": ("public_job_admission_grants",),
        }
        missing = []
        for role, groups in required.items():
            for group in groups:
                if re.search(rf"{group}\(\s*schema,\s*{role}\s*\)", body) is None:
                    missing.append(f"{group} for {role}")
        if (
            re.search(r"coordination_runtime_grants\(\s*schema,\s*speech_role=dispatch", body)
            is None
        ):
            missing.append("coordination_runtime_grants for dispatch")
        self.assertEqual(missing, [], f"provisioning script omits role grants: {missing}")
        source = read(PROVISION)
        for name in DISPATCH_GRANT_GROUPS:
            self.assertIn(f"{name}(", source)
        self.assertIn("voice_validation_worker_grants", source)
        self.assertIn('if "voice_validation" in kinds:', source)
        self.assertIn("public_job_admission_grants", source)
        self.assertIn("WRITER_BASELINE_SELECT", source)
        self.assertIn("if len({writer, admission, dispatch}) != 3", source)

    def test_both_documents_point_at_the_single_provisioning_script(self) -> None:
        for document in (MINIMAL_DOC, RUNBOOK):
            self.assertIn("scripts/dev-env/provision-job-roles.py", read(document))

    def test_the_minimal_runbook_documents_the_three_job_roles(self) -> None:
        document = read(MINIMAL_DOC)
        for role in ("sn_job_writer", "sn_job_admission", "sn_job_dispatch"):
            self.assertIn(f"`{role}`", document)
        for grant in DISPATCH_GRANT_GROUPS:
            self.assertIn(grant, document)
        self.assertIn("voice_validation_worker_grants", document)
        for password in (
            "SPEECHNEXUS_JOB_WRITER_ROLE_PASSWORD",
            "SPEECHNEXUS_JOB_ADMISSION_ROLE_PASSWORD",
            "SPEECHNEXUS_JOB_DISPATCH_ROLE_PASSWORD",
        ):
            self.assertIn(f"export {password}=", document)

    def test_the_documents_keep_the_engine_as_a_deployment_input(self) -> None:
        runbook = read(RUNBOOK)
        self.assertIn("真实引擎与已注册 `ready` 实例", runbook)
        self.assertIn("真实引擎", read(MINIMAL_DOC))


class JobDsnsStayOptionalTests(unittest.TestCase):
    def test_the_three_job_dsns_stay_blank_in_the_example_environment(self) -> None:
        # Prefilling any of them with a placeholder makes the base api service
        # refuse to start before the roles exist, so the opt-in must stay opt-in.
        lines = read(ENV_EXAMPLE).splitlines()
        for key in JOB_DSNS:
            self.assertIn(f"{key}=", lines, f"{key} must be declared")
        self.assertIn("SPEECHNEXUS_JOB_WRITER_ROLE_PASSWORD=change-me-job-writer-role", lines)

    def test_the_compose_api_service_forwards_the_job_writer_and_admission_dsns(self) -> None:
        api = service_block(read(COMPOSE), "api")
        for key in JOB_DSNS[:2]:
            self.assertIn(f"{key}: ${{{key}:-}}", api)


if __name__ == "__main__":
    unittest.main()
