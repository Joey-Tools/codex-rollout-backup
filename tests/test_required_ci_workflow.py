from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = REPO_ROOT / ".github/workflows/ci.yml"
REQUIRED_WORKFLOW_PATH = REPO_ROOT / ".github/workflows/required-ci.yml"


def top_level_job_ids(workflow: str) -> list[str]:
    in_jobs = False
    job_ids: list[str] = []
    for line in workflow.splitlines():
        if line == "jobs:":
            in_jobs = True
            continue
        if in_jobs and line and not line.startswith(" "):
            break
        if (
            in_jobs
            and line.startswith("  ")
            and not line.startswith("    ")
            and line.endswith(":")
        ):
            job_ids.append(line[2:-1])
    return job_ids


def jobs_block(workflow: str) -> str:
    return workflow[workflow.index("jobs:\n") :]


def workflow_call_block(workflow: str) -> str:
    lines = workflow.splitlines()
    start = lines.index("  workflow_call:")
    end = start + 1
    while end < len(lines) and (not lines[end] or lines[end].startswith("    ")):
        end += 1
    return "\n".join(lines[start:end]).rstrip("\n")


def checkout_step_blocks(workflow: str) -> list[str]:
    lines = workflow.splitlines()
    blocks: list[str] = []
    for start, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("- uses: actions/checkout@"):
            continue
        step_indent = len(line) - len(stripped)
        end = start + 1
        while end < len(lines):
            candidate = lines[end]
            candidate_stripped = candidate.lstrip()
            candidate_indent = len(candidate) - len(candidate_stripped)
            if candidate_stripped and candidate_indent <= step_indent:
                break
            end += 1
        blocks.append("\n".join(lines[start:end]))
    return blocks


def without_checkout_target_binding(workflow: str) -> str:
    return workflow.replace(
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          repository: ${{ inputs.repository }}\n"
        "          ref: ${{ inputs.ref }}\n"
        "          persist-credentials: false\n",
        "      - uses: actions/checkout@v4\n",
    )


class RequiredCiWorkflowTests(unittest.TestCase):
    def test_entry_wraps_only_the_required_macos_test(self) -> None:
        workflow = REQUIRED_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertEqual(
            workflow_call_block(workflow),
            "  workflow_call:\n"
            "    inputs:\n"
            "      repository:\n"
            "        required: true\n"
            "        type: string\n"
            "      ref:\n"
            "        required: true\n"
            "        type: string",
        )
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertEqual(top_level_job_ids(workflow), ["test"])
        self.assertIn("runs-on: macos-latest", workflow)
        self.assertIn(
            "bash -n scripts/*.sh tests/test_codex_launchd_scripts.sh",
            workflow,
        )
        self.assertIn("bash tests/test_codex_launchd_scripts.sh", workflow)
        self.assertIn(
            "python3 -B tests/test_required_ci_workflow.py",
            workflow,
        )
        for forbidden in (
            "pull_request:",
            "pull_request_target:",
            "push:",
            "ubuntu-latest",
            "secrets.",
            "contents: write",
            "id-" + "token: write",
            "statuses: write",
        ):
            self.assertNotIn(forbidden, workflow)

    def test_every_checkout_binds_the_requested_repository_and_ref(self) -> None:
        workflow = REQUIRED_WORKFLOW_PATH.read_text(encoding="utf-8")
        checkout_steps = checkout_step_blocks(workflow)

        self.assertGreater(len(checkout_steps), 0)
        for checkout_step in checkout_steps:
            self.assertIn(
                "        with:\n"
                "          repository: ${{ inputs.repository }}\n"
                "          ref: ${{ inputs.ref }}\n"
                "          persist-credentials: false",
                checkout_step,
            )

    def test_existing_and_reusable_workflows_share_the_required_job(self) -> None:
        ci_workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        required_workflow = REQUIRED_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertEqual(top_level_job_ids(ci_workflow), ["test"])
        self.assertEqual(
            jobs_block(ci_workflow),
            jobs_block(without_checkout_target_binding(required_workflow)),
        )
        self.assertIn(
            "python3 -B tests/test_required_ci_workflow.py",
            jobs_block(ci_workflow),
        )


if __name__ == "__main__":
    unittest.main()
