from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_automation import ValidationFailure, validate  # noqa: E402
from render_repository_workflows import image_workflow, source_gate  # noqa: E402
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
        self.assertIn("attestations: write", image_workflow(shared_ref))

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
            runner = generated.read_text()
            self.assertIn('f"{revision}^{{tree}}"', runner)
            self.assertIn("seen_commands: set[str] = set()", runner)
            workflow = (root / ".github" / "workflows" / "source-gate.yml").read_text()
            self.assertIn("name: Automation contract", workflow)
            self.assertNotIn("name: Validation gate", workflow)

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
            '--expected-environment "${{ inputs.environment }}"',
            workflow,
        )
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
