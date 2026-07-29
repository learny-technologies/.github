from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_automation import ValidationFailure, validate  # noqa: E402
from render_repository_workflows import (  # noqa: E402
    deploy_workflow,
    image_workflow,
    local_validation,
    source_gate,
)
from validate_product_manifest import (  # noqa: E402
    ValidationFailure as ProductValidationFailure,
)
from validate_product_manifest import validate as validate_product  # noqa: E402


class AutomationValidationTests(unittest.TestCase):
    def test_repository_manifest_is_valid(self) -> None:
        document = validate(ROOT / "automation.yaml", repository_root=ROOT)
        self.assertEqual(
            document["metadata"]["repository"], "learny-technologies/.github"
        )

    def test_unknown_pipeline_component_is_rejected(self) -> None:
        document = yaml.safe_load((ROOT / "automation.yaml").read_text())
        document["delivery"]["pipelines"] = [
            {
                "id": "runtime",
                "components": ["missing"],
                "environments": ["dev"],
                "executor": "scripts/validate_automation.py",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "automation.yaml"
            manifest.write_text(yaml.safe_dump(document, sort_keys=False))
            with self.assertRaisesRegex(ValidationFailure, "unknown artifacts"):
                validate(manifest, repository_root=ROOT)

    def test_manifest_requires_automation_contract_scope(self) -> None:
        document = yaml.safe_load((ROOT / "automation.yaml").read_text())
        document["localValidation"]["scopes"][0]["paths"] = ["automation.yaml"]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "automation.yaml"
            manifest.write_text(yaml.safe_dump(document, sort_keys=False))
            with self.assertRaisesRegex(
                ValidationFailure,
                "automation-contract scope must cover",
            ):
                validate(manifest, repository_root=ROOT)

    def test_manifest_requires_exact_automation_contract_id(self) -> None:
        document = yaml.safe_load((ROOT / "automation.yaml").read_text())
        document["localValidation"]["scopes"][0]["id"] = "automation"
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "automation.yaml"
            manifest.write_text(yaml.safe_dump(document, sort_keys=False))
            with self.assertRaisesRegex(
                ValidationFailure,
                "must use id automation-contract",
            ):
                validate(manifest, repository_root=ROOT)

    def test_manifest_rejects_split_automation_contract_commands(self) -> None:
        document = yaml.safe_load((ROOT / "automation.yaml").read_text())
        contract = document["localValidation"]["scopes"][0]
        contract["commands"].remove("git diff --check")
        document["localValidation"]["scopes"].append(
            {
                "id": "split-whitespace-check",
                "paths": list(contract["paths"]),
                "commands": ["git diff --check"],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "automation.yaml"
            manifest.write_text(yaml.safe_dump(document, sort_keys=False))
            with self.assertRaisesRegex(
                ValidationFailure,
                "automation contract scope is missing commands: git diff --check",
            ):
                validate(manifest, repository_root=ROOT)

    def test_generator_requires_full_shared_sha(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "render_repository_workflows.py"),
                str(ROOT / "automation.yaml"),
                "--output-root",
                str(ROOT),
                "--shared-ref",
                "main",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("full 40-character Git SHA", completed.stderr)

    def test_generated_workflows_use_least_privilege_attestation_permissions(
        self,
    ) -> None:
        shared_ref = "a" * 40
        self.assertNotIn("attestations:", source_gate(shared_ref))
        rendered_image = image_workflow(shared_ref)
        self.assertIn("attestations: write", rendered_image)
        self.assertNotIn("automation_ref", rendered_image)

    def test_artifact_claim_selects_the_authenticated_automation_revision(
        self,
    ) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "reusable-oci-publish.yml"
        ).read_text()
        claim = workflow.index("Claim Control Plane artifact operation")
        checkout = workflow.index("Check out trusted automation implementation")
        self.assertLess(claim, checkout)
        self.assertNotIn("inputs.automation_ref", workflow)
        self.assertIn(
            "ref: ${{ steps.claim.outputs.automation_revision }}",
            workflow,
        )

    def test_generator_emits_compilable_local_validation_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "render_repository_workflows.py"),
                    str(ROOT / "automation.yaml"),
                    "--output-root",
                    str(root),
                    "--shared-ref",
                    "a" * 40,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            generated = root / "scripts" / "validate_local.py"
            subprocess.run(
                [sys.executable, "-m", "py_compile", str(generated)],
                check=True,
            )
            self.assertTrue(generated.stat().st_mode & 0o111)
            runner = generated.read_text()
            self.assertIn('f"{revision}^{{tree}}"', runner)
            self.assertIn("seen_commands: set[str] = set()", runner)
            self.assertIn("def matches_any(", runner)
            self.assertLessEqual(max(len(line) for line in runner.splitlines()), 100)
            workflow = (root / ".github" / "workflows" / "source-gate.yml").read_text()
            self.assertIn("name: Automation contract", workflow)
            self.assertNotIn("name: Validation gate", workflow)

    def test_generated_runner_selects_deletions_and_checks_committed_range(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "validate_local.py"
            generated.write_text(
                local_validation(
                    "learny-technologies/example",
                    [
                        {
                            "id": "automation",
                            "paths": [".github/**"],
                            "commands": ["git diff --check"],
                        }
                    ],
                )
            )
            spec = importlib.util.spec_from_file_location("generated_runner", generated)
            assert spec and spec.loader
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)

            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "automation@example.com"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Automation Test"],
                cwd=repository,
                check=True,
            )
            workflow = repository / ".github" / "workflows" / "old.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: old\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            workflow.unlink()
            subprocess.run(["git", "add", "-u"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "delete"], cwd=repository, check=True
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            previous = Path.cwd()
            try:
                os.chdir(repository)
                merge_base, changed = runner.changed_files(base, head)
            finally:
                os.chdir(previous)

            self.assertEqual(merge_base, base)
            self.assertEqual(changed, [".github/workflows/old.yml"])
            self.assertTrue(
                runner.scope_selected(
                    {"paths": [".github/**"]},
                    changed,
                    False,
                )
            )
            self.assertEqual(
                runner.rendered_command("git diff --check", merge_base, head),
                f"git diff --check {base}..{head}",
            )

    def test_source_gate_ignores_pr_metadata_edits(self) -> None:
        workflow = source_gate("a" * 40)
        self.assertIn("types: [opened, synchronize, reopened]", workflow)
        self.assertNotIn("edited", workflow)

    def test_generated_runner_keeps_contract_only_changes_lightweight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "validate_local.py"
            generated.write_text(
                local_validation(
                    "learny-technologies/example",
                    [
                        {
                            "id": "automation-contract",
                            "paths": ["scripts/validate_local.py", "automation.yaml"],
                            "commands": ["actionlint"],
                        },
                        {
                            "id": "backend",
                            "paths": ["scripts/**", "app/**"],
                            "commands": ["pytest"],
                        },
                    ],
                )
            )
            spec = importlib.util.spec_from_file_location("contract_runner", generated)
            assert spec and spec.loader
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)

            contract_only = runner.selected_scopes(
                ["scripts/validate_local.py"],
                False,
            )
            mixed = runner.selected_scopes(
                ["scripts/validate_local.py", "app/main.py"],
                False,
            )

            self.assertEqual(
                [scope["id"] for scope in contract_only],
                ["automation-contract"],
            )
            self.assertEqual(
                [scope["id"] for scope in mixed],
                ["automation-contract", "backend"],
            )

    def test_provider_runner_separates_contract_and_implementation_changes(
        self,
    ) -> None:
        document = yaml.safe_load((ROOT / "automation.yaml").read_text())
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "validate_local.py"
            generated.write_text(
                local_validation(
                    document["metadata"]["repository"],
                    document["localValidation"]["scopes"],
                )
            )
            spec = importlib.util.spec_from_file_location("provider_runner", generated)
            assert spec and spec.loader
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)

            contract_only = runner.selected_scopes(
                ["scripts/validate_local.py"],
                False,
            )
            mixed = runner.selected_scopes(
                ["scripts/validate_local.py", "scripts/render_repository_workflows.py"],
                False,
            )

            self.assertEqual(
                [scope["id"] for scope in contract_only],
                ["automation-contract"],
            )
            self.assertEqual(
                [scope["id"] for scope in mixed],
                ["automation-contract", "automation-implementation"],
            )

    def test_schema_rejects_mutable_non_ghcr_image(self) -> None:
        schema = json.loads(
            (
                ROOT / "schemas" / "repository-automation-v1alpha1.schema.json"
            ).read_text()
        )
        image_pattern = schema["$defs"]["artifact"]["properties"]["image"]["pattern"]
        self.assertIn("ghcr", image_pattern)

    def test_resolver_rejects_unknown_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "resolve_automation.py"),
                    str(ROOT / "automation.yaml"),
                    "--repository-root",
                    str(ROOT),
                    "--artifact",
                    "missing",
                    "--github-output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unknown artifact", completed.stderr)

    def test_deployment_claim_precedes_authorized_executor_resolution(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "reusable-dokploy-deploy.yml"
        ).read_text()
        claim = workflow.index("Claim Control Plane deployment operation")
        resolve = workflow.index("Resolve authorized delivery executor")
        self.assertLess(claim, resolve)
        self.assertIn(
            '--pipeline "${{ steps.claim.outputs.pipeline_id }}"',
            workflow,
        )
        self.assertNotIn(
            '--pipeline "${{ inputs.pipeline_id }}"',
            workflow,
        )
        self.assertIn("environment: ${{ inputs.environment }}", workflow)
        self.assertIn(
            "EXPECTED_ENVIRONMENT: ${{ inputs.environment }}",
            workflow,
        )
        self.assertNotIn("inputs.automation_ref", workflow)
        self.assertIn(
            "ref: ${{ steps.claim.outputs.automation_revision }}",
            workflow,
        )
        self.assertNotIn("secrets: inherit", deploy_workflow("a" * 40))
        self.assertNotIn("DOKPLOY_API_TOKEN:", workflow.split("steps:", 1)[0])
        self.assertIn("Execute Stiqi Core delivery", workflow)
        self.assertIn("Execute Stiqi landing delivery", workflow)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "py_compile",
                str(ROOT / "scripts" / "deployment_operation.py"),
            ],
            check=True,
        )

    def test_product_manifest_rejects_planned_delivery_target(self) -> None:
        document = {
            "apiVersion": "platform.learny.technology/v1alpha1",
            "kind": "Product",
            "metadata": {"id": "sample"},
            "repositories": [
                {
                    "id": "docs",
                    "repository_id": "1",
                    "role": "source",
                },
                {
                    "id": "api",
                    "repository_id": "2",
                    "role": "component-source",
                },
            ],
            "components": [{"id": "api", "repository": "api"}],
            "environments": [
                {
                    "id": "staging",
                    "state": "planned",
                    "deployments": [{"component": "api", "state": "planned"}],
                }
            ],
            "delivery": {
                "pipelines": [
                    {
                        "id": "api",
                        "repository": "api",
                        "repository_id": "2",
                        "automation_revision": "9" * 40,
                        "components": ["api"],
                        "environments": ["staging"],
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "project.yaml"
            manifest.write_text(yaml.safe_dump(document, sort_keys=False))
            with self.assertRaisesRegex(
                ProductValidationFailure,
                "non-managed environment staging",
            ):
                validate_product(manifest)


if __name__ == "__main__":
    unittest.main()
