"""Offline guardrails for the opt-in PR-Agent workflow; no model calls."""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/pr-agent-review.yml").read_text()


class PRAgentReviewWorkflowTests(unittest.TestCase):
    def test_trigger_and_trust_boundary(self):
        trigger = WORKFLOW.split("\npermissions:", 1)[0]
        self.assertIn("  pull_request:\n", trigger)
        self.assertIn("types: [opened, reopened, ready_for_review, synchronize]", trigger)
        for forbidden in ("pull_request_target:", "issue_comment:", "workflow_dispatch:"):
            self.assertNotIn(forbidden, trigger)
        for guard in (
            "vars.PR_AGENT_REVIEW_ENABLED == 'true'",
            "head.repo.full_name == github.repository",
            "draft == false",
            "user.type != 'Bot'",
            "author_association == 'OWNER'",
            "author_association == 'MEMBER'",
            "author_association == 'COLLABORATOR'",
        ):
            self.assertIn(guard, WORKFLOW)
        self.assertNotIn("actions/checkout", WORKFLOW)
        self.assertNotIn("uses:", WORKFLOW)
        self.assertIn("permissions: {}", WORKFLOW)
        self.assertIn("      contents: read\n      pull-requests: write", WORKFLOW)
        self.assertNotIn("issues: write", WORKFLOW)

    def test_explicit_configuration_and_review_only(self):
        for setting in (
            "CONFIG__FALLBACK_MODELS: '[]'",
            "CONFIG__USE_REPO_SETTINGS_FILE: 'false'",
            "CONFIG__USE_GLOBAL_SETTINGS_FILE: 'false'",
            "CONFIG__ENABLE_PER_DIRECTORY_SETTINGS: 'false'",
            "CONFIG__REPO_CONTEXT_FILES: '[]'",
            "CONFIG__PROPAGATE_TOOL_ERRORS: 'true'",
            "CONFIG__RESTRICTED_MODE: 'true'",
            "CONFIG__NUM_RETRIES: '0'",
            "CONFIG__RETRY_SAME_MODEL_ON_TIMEOUT: 'false'",
            "PR_REVIEWER__REQUIRE_TICKET_ANALYSIS_REVIEW: 'false'",
            "PR_REVIEWER__ENABLE_REVIEW_LABELS_SECURITY: 'false'",
            "PR_REVIEWER__ENABLE_REVIEW_LABELS_EFFORT: 'false'",
        ):
            self.assertIn(setting, WORKFLOW)
        self.assertIn("CONFIG__MODEL: openai/glm-5.3-flash", WORKFLOW)
        self.assertIn("OPENAI__API_BASE: https://opencode.ai/inference/go/openai/v1", WORKFLOW)
        self.assertIn('--pr_url "$PR_URL" review', WORKFLOW)
        self.assertEqual(WORKFLOW.count("docker run"), 1)
        self.assertRegex(WORKFLOW, r"pragent/pr-agent@sha256:[a-f0-9]{64}")
        self.assertNotIn(" -v ", WORKFLOW)
        self.assertNotIn("--volume", WORKFLOW)

    def test_missing_key_and_session_isolation(self):
        self.assertIn('if [[ -z "$OPENAI_KEY" ]]', WORKFLOW)
        self.assertIn("if: steps.key.outputs.configured == 'true'", WORKFLOW)
        self.assertIn("skipped: OPENCODE_API_KEY is not configured", WORKFLOW)
        self.assertIn('[[ "$REPOSITORY_ID" =~ ^[0-9]+$ && "$PR_NUMBER" =~ ^[0-9]+$ ]]', WORKFLOW)
        self.assertIn('@str {"User-Agent":"pr-agent/0.49.0"', WORKFLOW)
        self.assertIn('"x-opencode-session":"pr-agent-%s-pr-%s"', WORKFLOW)
        self.assertIn('"$REPOSITORY_ID" "$PR_NUMBER"', WORKFLOW)
        self.assertEqual(len(re.findall(r"secrets\.OPENCODE_API_KEY", WORKFLOW)), 2)


if __name__ == "__main__":
    unittest.main()
