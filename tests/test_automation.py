from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_automation import ValidationFailure, validate  # noqa: E402
from deployment_operation import (  # noqa: E402
    OperationError,
    validated_plan,
)
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
from verify_registry_evidence import (  # noqa: E402
    BUILDKIT_BUILD_TYPE,
    VerificationError,
    verify as verify_registry_evidence,
)


class AutomationValidationTests(unittest.TestCase):
    def test_deployment_request_workflow_is_narrow_and_oidc_only(self) -> None:
        path = ROOT / ".github" / "workflows" / "reusable-deployment-request.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        inputs = workflow["on"]["workflow_call"]["inputs"]

        self.assertEqual(
            set(inputs),
            {
                "project_id",
                "environment",
                "pipeline_id",
                "source_sha",
                "definition_hash",
                "migration_heads_json",
                "rollback_compatible",
                "reason",
            },
        )
        self.assertEqual(
            workflow["permissions"],
            {"id-token": "write"},
        )
        request = workflow["jobs"]["request"]
        self.assertEqual(request["environment"], "${{ inputs.environment }}")
        self.assertEqual(request["timeout-minutes"], "5")
        self.assertNotIn("secrets", path.read_text())
        self.assertIn(
            "/v1/deployments/promotion-intents",
            request["steps"][0]["run"],
        )
        self.assertIn("Idempotency-Key", request["steps"][0]["run"])
        self.assertIn("request_fingerprint", request["steps"][0]["run"])
        self.assertIn("len(migration_heads) > 32", request["steps"][0]["run"])
        self.assertNotIn("subprocess", request["steps"][0]["run"])

    def test_deployment_helper_accepts_narrow_v1_plan(self) -> None:
        plan = {
            "version": "v1",
            "operation_id": "operation-1",
            "pipeline_id": "backend",
            "environment_id": "dev",
            "source_revision": "a" * 40,
        }

        validated = validated_plan(plan, "operation-1")

        self.assertEqual(validated, (plan, "backend", "dev", "a" * 40))

    def test_deployment_helper_rejects_invalid_narrow_v1_plan(self) -> None:
        plan = {
            "version": "v1",
            "operation_id": "another-operation",
            "pipeline_id": "backend",
            "environment_id": "dev",
            "source_revision": "a" * 40,
        }

        with self.assertRaisesRegex(OperationError, "different deployment operation"):
            validated_plan(plan, "operation-1")

    def test_deployment_helper_accepts_only_absent_legacy_version(self) -> None:
        legacy = {
            "operation": {
                "id": "operation-1",
                "pipeline_id": "backend",
                "environment_id": "dev",
            },
            "release": {"source_revision": "a" * 40},
        }
        unknown = {**legacy, "version": "v2"}

        self.assertEqual(
            validated_plan(legacy, "operation-1"),
            (legacy, "backend", "dev", "a" * 40),
        )
        with self.assertRaisesRegex(
            OperationError, "unsupported deployment plan version"
        ):
            validated_plan(unknown, "operation-1")

    def test_repository_manifest_is_valid(self) -> None:
        document = validate(ROOT / "automation.yaml", repository_root=ROOT)
        self.assertEqual(
            document["metadata"]["repository"], "learny-technologies/.github"
        )

    def test_pipeline_accepts_manifest_defined_environment_ids(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/repository-automation-v1alpha1.schema.json").read_text()
        )
        pipeline_schema = {
            "$schema": schema["$schema"],
            "$ref": "#/$defs/pipeline",
            "$defs": schema["$defs"],
        }
        pipeline = {
            "id": "platform",
            "components": ["runtime"],
            "environments": ["dev", "platform-production"],
            "executor": "scripts/delivery.py",
        }
        jsonschema.Draft202012Validator(pipeline_schema).validate(pipeline)
        pipeline["environments"] = ["Platform Production"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(pipeline_schema).validate(pipeline)

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

    def test_generated_workflows_do_not_require_github_attestations(self) -> None:
        shared_ref = "a" * 40
        self.assertNotIn("attestations:", source_gate(shared_ref))
        rendered_image = image_workflow(shared_ref)
        self.assertNotIn("attestations:", rendered_image)
        self.assertNotIn("automation_ref", rendered_image)

        publication = (
            ROOT / ".github" / "workflows" / "reusable-oci-publish.yml"
        ).read_text()
        deployment = (
            ROOT / ".github" / "workflows" / "reusable-dokploy-deploy.yml"
        ).read_text()
        self.assertNotIn("actions/attest-build-provenance", publication)
        self.assertNotIn("attestations:", publication)
        self.assertIn("attestations: read", deployment)
        self.assertIn("attestations: read", deploy_workflow("a" * 40))
        self.assertIn("provenance: mode=max,version=v1", publication)
        self.assertIn("sbom: true", publication)
        self.assertIn(
            "io.learny.automation.revision="
            "${{ steps.claim.outputs.automation_revision }}",
            publication,
        )

        artifact_helper = (ROOT / "scripts" / "artifact_operation.py").read_text()
        registry_verifier = (
            ROOT / "scripts" / "verify_registry_evidence.py"
        ).read_text()
        self.assertNotIn("attestation-url", artifact_helper)
        self.assertIn('"buildkit_provenance": True', artifact_helper)
        self.assertIn('"sbom": True', artifact_helper)
        self.assertIn("io.learny.automation.revision", registry_verifier)
        self.assertIn(".Provenance.SLSA", registry_verifier)
        self.assertIn(".SBOM.SPDX", registry_verifier)

    def test_portable_registry_evidence_binds_source_and_automation(self) -> None:
        repository = "learny-technologies/control-plane-workspace"
        source_revision = "7d80cf6cc0c0244c464915fcb4c4875a62911d26"
        automation_revision = "b" * 40
        source_url = f"https://github.com/{repository}"
        labels = {
            "org.opencontainers.image.revision": source_revision,
            "org.opencontainers.image.source": source_url,
            "io.learny.automation.revision": automation_revision,
        }
        provenance = json.loads(
            (ROOT / "tests" / "fixtures" / "buildkit-v1-provenance.json").read_text()
        )
        evidence = (labels, provenance, "SPDX-2.3")
        with mock.patch(
            "verify_registry_evidence.registry_evidence",
            return_value=evidence,
        ):
            verify_registry_evidence(
                image=(
                    "ghcr.io/learny-technologies/control-plane-api@sha256:" + "c" * 64
                ),
                repository=repository,
                source_revision=source_revision,
                automation_revision=automation_revision,
            )

    def test_portable_registry_evidence_fails_closed_on_revision_drift(self) -> None:
        repository = "learny-technologies/example"
        source_url = f"https://github.com/{repository}"
        labels = {
            "org.opencontainers.image.revision": "d" * 40,
            "org.opencontainers.image.source": source_url,
            "io.learny.automation.revision": "b" * 40,
        }
        provenance = {
            "buildDefinition": {"buildType": BUILDKIT_BUILD_TYPE},
            "runDetails": {
                "builder": {"id": f"{source_url}/actions/runs/123/attempts/1"},
                "metadata": {
                    "buildkit_metadata": {
                        "vcs": {
                            "revision": "a" * 40,
                            "source": source_url,
                        }
                    }
                },
            },
        }
        evidence = (labels, provenance, "SPDX-2.3")
        with (
            mock.patch(
                "verify_registry_evidence.registry_evidence",
                return_value=evidence,
            ),
            self.assertRaisesRegex(VerificationError, "does not match"),
        ):
            verify_registry_evidence(
                image="ghcr.io/learny-technologies/example@sha256:" + "c" * 64,
                repository=repository,
                source_revision="a" * 40,
                automation_revision="b" * 40,
            )

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

    def test_deployment_keeps_executor_and_release_revisions_separate(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "reusable-dokploy-deploy.yml"
        ).read_text()

        claim = workflow.index("Claim Control Plane deployment operation")
        automation = workflow.index("Check out trusted automation implementation")
        delivery = workflow.index("Check out exact repository delivery implementation")
        release = workflow.index("Check out exact release source")
        resolve = workflow.index("Resolve authorized delivery executor")

        self.assertLess(claim, automation)
        self.assertLess(automation, delivery)
        self.assertLess(delivery, release)
        self.assertLess(release, resolve)
        self.assertIn("ref: ${{ github.workflow_sha }}", workflow)
        self.assertIn("delivery-source/automation.yaml", workflow)
        self.assertIn("--repository-root delivery-source", workflow)
        self.assertIn(
            'python "delivery-source/${{ steps.pipeline.outputs.executor }}" execute',
            workflow,
        )
        self.assertNotIn(
            'python "release-source/${{ steps.pipeline.outputs.executor }}" execute',
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
        rendered_deploy = deploy_workflow("a" * 40)
        self.assertIn(
            "environment:\n        description: Control Plane-authorized target environment",
            rendered_deploy,
        )
        self.assertNotIn("type: choice", rendered_deploy)
        self.assertNotIn("options: [dev, staging, production]", rendered_deploy)
        self.assertIn(
            "DOKPLOY_API_KEY: ${{ secrets.DOKPLOY_API_KEY }}",
            rendered_deploy,
        )
        self.assertIn(
            "DOKPLOY_API_TOKEN: ${{ secrets.DOKPLOY_API_TOKEN }}",
            rendered_deploy,
        )
        parsed_workflow = yaml.load(
            workflow,
            Loader=yaml.BaseLoader,
        )
        declared_secrets = parsed_workflow["on"]["workflow_call"]["secrets"]
        self.assertEqual(
            set(declared_secrets),
            {
                "DOKPLOY_API_KEY",
                "DOKPLOY_API_TOKEN",
                "DOKPLOY_APPLICATION_ID",
                "DOKPLOY_STICKIFY_CORE_APPLICATION_ID",
                "DOKPLOY_STICKIFY_MIGRATION_APPLICATION_ID",
                "DOKPLOY_STICKIFY_WORKER_APPLICATION_ID",
                "DOKPLOY_URL",
            },
        )
        self.assertIn("Execute Control Plane delivery", workflow)
        self.assertIn(
            "github.repository == 'learny-technologies/control-plane-workspace'",
            workflow,
        )
        self.assertIn(
            "CONTROL_PLANE_DOKPLOY_API_KEY: ${{ secrets.DOKPLOY_API_KEY }}",
            workflow,
        )
        self.assertIn("Execute platform observability delivery", workflow)
        self.assertIn(
            "github.repository == 'learny-technologies/platform-observability'",
            workflow,
        )
        self.assertIn(
            '{"claim_attempted":true,"completed":false,"sequence":0}',
            workflow,
        )
        attempted = workflow.index(
            '{"claim_attempted":true,"completed":false,"sequence":0}'
        )
        claim_post = workflow.index(
            "with urllib.request.urlopen(claim_request, timeout=30)"
        )
        self.assertLess(attempted, claim_post)
        self.assertIn("Record fail-closed claim processing failure", workflow)
        self.assertIn(
            '"failure_stage": "claim_processing_failed"',
            workflow,
        )
        self.assertNotIn(
            "DOKPLOY_API_TOKEN",
            parsed_workflow["jobs"]["deploy"]["env"],
        )
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

    def test_product_manifest_accepts_registered_executor_contracts(self) -> None:
        document = {
            "apiVersion": "platform.learny.technology/v1alpha1",
            "kind": "Product",
            "metadata": {"id": "sample"},
            "repositories": [
                {
                    "id": "api",
                    "repository_id": "2",
                    "role": "source",
                }
            ],
            "components": [{"id": "api", "repository": "api"}],
            "environments": [
                {
                    "id": "dev",
                    "state": "managed",
                    "deployments": [{"component": "api", "state": "managed"}],
                }
            ],
            "delivery": {
                "pipelines": [
                    {
                        "id": "api",
                        "repository": "api",
                        "repository_id": "2",
                        "deployment": {
                            "workflow": ".github/workflows/deploy.yml",
                            "ref": "main",
                            "executor": {
                                "repository": "learny-technologies/delivery-executors",
                                "workflow": ".github/workflows/rollout.yml",
                                "revision": "8" * 40,
                            },
                        },
                        "publication": {
                            "workflow": ".github/workflows/image.yml",
                            "ref": "main",
                            "executor": {
                                "repository": "learny-technologies/build-executors",
                                "workflow": ".github/workflows/publish.yml",
                                "revision": "9" * 40,
                            },
                        },
                        "components": ["api"],
                        "environments": ["dev"],
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "project.yaml"
            manifest.write_text(yaml.safe_dump(document, sort_keys=False))

            self.assertEqual(validate_product(manifest), document)

    def test_product_manifest_rejects_invalid_registered_executor_revision(
        self,
    ) -> None:
        document = {
            "apiVersion": "platform.learny.technology/v1alpha1",
            "kind": "Product",
            "metadata": {"id": "sample"},
            "repositories": [
                {
                    "id": "api",
                    "repository_id": "2",
                    "role": "source",
                }
            ],
            "components": [{"id": "api", "repository": "api"}],
            "environments": [
                {
                    "id": "dev",
                    "state": "managed",
                    "deployments": [{"component": "api", "state": "managed"}],
                }
            ],
            "delivery": {
                "pipelines": [
                    {
                        "id": "api",
                        "repository": "api",
                        "repository_id": "2",
                        "deployment": {
                            "workflow": ".github/workflows/deploy.yml",
                            "executor": {
                                "repository": "learny-technologies/delivery-executors",
                                "workflow": ".github/workflows/rollout.yml",
                                "revision": "8" * 40,
                            },
                        },
                        "publication": {
                            "workflow": ".github/workflows/image.yml",
                            "executor": {
                                "repository": "learny-technologies/build-executors",
                                "workflow": ".github/workflows/publish.yml",
                                "revision": "main",
                            },
                        },
                        "components": ["api"],
                        "environments": ["dev"],
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "project.yaml"
            manifest.write_text(yaml.safe_dump(document, sort_keys=False))
            with self.assertRaisesRegex(
                ProductValidationFailure,
                "publication executor must pin a full revision",
            ):
                validate_product(manifest)

    def test_product_manifest_rejects_null_or_partial_registered_contracts(
        self,
    ) -> None:
        legacy_revision = "9" * 40
        registered_workflow = {
            "workflow": ".github/workflows/deploy.yml",
            "executor": {
                "repository": "learny-technologies/delivery-executors",
                "workflow": ".github/workflows/rollout.yml",
                "revision": legacy_revision,
            },
        }
        cases = (
            {
                "automation_revision": legacy_revision,
                "deployment": None,
                "publication": None,
            },
            {
                "automation_revision": legacy_revision,
                "deployment": registered_workflow,
            },
        )

        for pipeline in cases:
            with self.subTest(pipeline=pipeline):
                with self.assertRaisesRegex(
                    ProductValidationFailure,
                    "must register a (deployment|publication) workflow",
                ):
                    from validate_product_manifest import (
                        validate_pipeline_automation,
                    )

                    validate_pipeline_automation("api", pipeline)


if __name__ == "__main__":
    unittest.main()
