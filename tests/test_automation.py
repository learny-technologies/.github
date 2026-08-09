from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from delivery_contract import (  # noqa: E402
    PRODUCTION_ENVIRONMENTS,
    ContractError,
    plan_document,
    record_reference,
    validate_result,
)
from render_repository_workflows import (  # noqa: E402
    deploy_workflow,
    image_workflow,
    local_validation,
    validation_workflow,
)
from validate_automation import ValidationFailure, validate  # noqa: E402
from validate_product_manifest import validate as validate_product  # noqa: E402
from verify_registry_evidence import (  # noqa: E402
    BUILDKIT_BUILD_TYPE,
    VerificationError,
)
from verify_registry_evidence import (  # noqa: E402
    verify as verify_registry_evidence,
)


def record(contract: str = "v3") -> dict[str, str]:
    return {
        "contract": contract,
        "delivery_contract": "github-actions/v1",
        "task_id": "TASK-A8C05070-DFA6-4EB4-9183-EF948BEB3FF5",
        "repository": "learny-technologies/engineering-handbook-workspace",
        "path": "docs/execution/EXEC-task.md",
        "revision": "a" * 40,
        "content_digest": "b" * 64,
    }


def initialize_runtime_repo(root: Path) -> str:
    (root / "scripts").mkdir()
    (root / "deploy").mkdir()
    (root / "Dockerfile").write_text("FROM scratch\n")
    (root / "deploy" / "runtime.yml").write_text("services: {}\n")
    (root / "scripts" / "delivery.py").write_text("print('delivery')\n")
    record_path = root / "docs" / "execution" / "EXEC-task.md"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        "---\n"
        "linked_to: TASK-A8C05070-DFA6-4EB4-9183-EF948BEB3FF5\n"
        "record_contract: v3\n"
        "delivery_contract: github-actions/v1\n"
        "status: frozen\n"
        "target: production_release\n"
        "---\n\n"
        "# Test execution record\n"
    )
    manifest = {
        "apiVersion": "automation.learny.technology/v1alpha1",
        "kind": "RepositoryAutomation",
        "metadata": {
            "repository": "learny-technologies/example",
            "project": "example",
            "profiles": ["service"],
        },
        "localValidation": {
            "scopes": [
                {
                    "id": "automation-contract",
                    "paths": [
                        ".github/workflows/**",
                        "scripts/validate_local.py",
                        "automation.yaml",
                    ],
                    "commands": ["actionlint", "git diff --check"],
                }
            ]
        },
        "sourceGate": {"enabled": True, "checkName": "Automation contract"},
        "artifacts": [
            {
                "id": "api",
                "paths": ["Dockerfile"],
                "image": "ghcr.io/learny-technologies/example",
                "context": ".",
                "dockerfile": "Dockerfile",
                "platforms": ["linux/amd64"],
            }
        ],
        "delivery": {
            "pipelines": [
                {
                    "id": "backend",
                    "components": ["api"],
                    "environments": [
                        "dev",
                        "staging",
                        "production",
                        "platform-production",
                    ],
                    "executor": "scripts/delivery.py",
                    "definition": "deploy/runtime.yml",
                }
            ]
        },
    }
    (root / "automation.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class AutomationValidationTests(unittest.TestCase):
    def test_repository_manifest_is_valid(self) -> None:
        document = validate(ROOT / "automation.yaml", repository_root=ROOT)
        self.assertEqual(
            document["metadata"]["repository"], "learny-technologies/.github"
        )

    def test_runtime_pipeline_requires_project_and_definition(self) -> None:
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
            "environments": ["dev"],
            "executor": "scripts/delivery.py",
            "definition": "deploy/runtime.yml",
        }
        jsonschema.Draft202012Validator(pipeline_schema).validate(pipeline)
        del pipeline["definition"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(pipeline_schema).validate(pipeline)

    def test_unknown_pipeline_component_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_runtime_repo(root)
            document = yaml.safe_load((root / "automation.yaml").read_text())
            document["delivery"]["pipelines"][0]["components"] = ["missing"]
            (root / "automation.yaml").write_text(
                yaml.safe_dump(document, sort_keys=False)
            )
            with self.assertRaisesRegex(ValidationFailure, "unknown artifacts"):
                validate(root / "automation.yaml", repository_root=root)

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

    def test_thin_workflows_use_direct_github_contract(self) -> None:
        shared = "a" * 40
        validation = validation_workflow(shared)
        image = image_workflow(shared)
        deploy = deploy_workflow(shared)
        self.assertIn("reusable-validate.yml@" + shared, validation)
        self.assertIn("source_sha", image)
        self.assertIn("execution_record_json", image)
        self.assertIn("EXECUTION_RECORD_APP_ID", image)
        self.assertIn("EXECUTION_RECORD_APP_PRIVATE_KEY", image)
        self.assertIn("EXECUTION_RECORD_APP_ID", deploy)
        self.assertIn("EXECUTION_RECORD_APP_PRIVATE_KEY", deploy)
        self.assertIn("reusable-deploy.yml@" + shared, deploy)
        self.assertIn("expected_fingerprint", deploy)
        self.assertNotIn("DOKPLOY_API_TOKEN", deploy)
        self.assertNotIn("Control Plane", validation + image + deploy)
        self.assertNotIn("operation_id", validation + image + deploy)
        self.assertNotIn("secrets: inherit", deploy)

    def test_shared_deploy_has_no_product_specific_dispatch(self) -> None:
        workflow = (ROOT / ".github/workflows/reusable-deploy.yml").read_text()
        for forbidden in (
            "stickify-core",
            "stiqi-web-landing",
            "trace-workspace",
            "control-plane-workspace",
            "platform-observability",
        ):
            self.assertNotIn(forbidden, workflow)
        self.assertIn("delivery-source/", workflow)
        self.assertIn(".deployment-plan.json", workflow)
        self.assertIn(".deployment-result.json", workflow)
        self.assertIn(
            "LEARNY_EXECUTOR_CONFIGURATION_JSON: ${{ toJSON(vars) }}", workflow
        )
        self.assertIn("LEARNY_EXECUTOR_CREDENTIAL", workflow)
        self.assertNotIn("RELEASE_LEDGER", workflow)
        self.assertNotIn("release ledger", workflow.lower())
        self.assertIn(
            "actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349",
            workflow,
        )
        self.assertIn("permission-contents: read", workflow)
        self.assertIn("repositories: engineering-handbook-workspace", workflow)
        self.assertIn(
            "token: ${{ steps.execution-record-token.outputs.token }}", workflow
        )
        self.assertNotIn("DOKPLOY_", workflow)
        self.assertLess(
            workflow.index("Execute repository-owned delivery"),
            workflow.index("Create canonical GitHub Deployment"),
        )
        self.assertIn(
            '"rollback_eligible": result["rollback_eligible"] is True', workflow
        )
        self.assertIn(
            '"artifact_verification": plan["artifact_verification"]', workflow
        )
        self.assertIn('publisher = plan["artifact_verification"][component]["publisher"]', workflow)
        self.assertIn('re.escape(publisher["repository"])', workflow)
        self.assertIn('re.escape(publisher["workflow"])', workflow)
        self.assertIn('re.escape(automation)', workflow)
        self.assertNotIn('/.github/workflows/image.yml@refs/heads/.+$', workflow)
        self.assertIn(
            "EXECUTOR_CREDENTIAL: ${{ secrets.EXECUTOR_CREDENTIAL }}",
            deploy_workflow("a" * 40),
        )
        self.assertIn(
            "LEARNY_EXECUTOR_CREDENTIAL: ${{ secrets.EXECUTOR_CREDENTIAL }}",
            workflow,
        )

    def test_publication_is_on_demand_and_reuses_verified_digest(self) -> None:
        workflow = (ROOT / ".github/workflows/reusable-oci-publish.yml").read_text()
        self.assertIn("Resolve reusable artifact", workflow)
        self.assertIn("verify_registry_evidence.py", workflow)
        self.assertIn("cosign verify", workflow)
        self.assertIn(
            "learny-technologies/\\\\.github/\\\\.github/workflows/reusable-oci-publish\\\\.yml@${AUTOMATION_REVISION}",
            workflow,
        )
        self.assertNotIn("${GITHUB_REPOSITORY}/.github/workflows/image.yml", workflow)
        self.assertIn("provenance: mode=max,version=v1", workflow)
        self.assertIn("sbom: true", workflow)
        self.assertIn("release.json", workflow)
        self.assertIn("actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349", workflow)
        self.assertIn("permission-contents: read", workflow)
        self.assertIn("repositories: engineering-handbook-workspace", workflow)
        self.assertIn("token: ${{ steps.execution-record-token.outputs.token }}", workflow)
        self.assertIn("EXECUTION_RECORD_APP_PRIVATE_KEY:\n        required: true", workflow)
        self.assertNotIn("Control Plane", workflow)

    def test_generated_runner_supports_v3_execution_records(self) -> None:
        runner = local_validation(
            "learny-technologies/example",
            [
                {
                    "id": "automation-contract",
                    "paths": [
                        ".github/workflows/**",
                        "scripts/validate_local.py",
                        "automation.yaml",
                    ],
                    "commands": ["actionlint", "git diff --check"],
                }
            ],
        )
        self.assertIn('contract_value not in {"v2", "v3"}', runner)
        self.assertIn("execution_record.delivery_contract", runner)

    def test_external_actions_are_pinned_to_full_sha(self) -> None:
        pattern = re.compile(r"^\s*uses:\s*[^\s]+@([^\s#]+)", re.MULTILINE)
        for path in (ROOT / ".github" / "workflows").glob("*.yml"):
            for revision in pattern.findall(path.read_text()):
                self.assertRegex(revision, r"^[0-9a-f]{40}$", path.name)

    def test_product_manifest_validator_accepts_current_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = {
                "apiVersion": "platform.learny.technology/v1alpha1",
                "kind": "Product",
                "metadata": {"id": "example", "name": "Example"},
                "repositories": [
                    {
                        "id": "docs",
                        "repository_id": "2",
                        "url": "https://github.com/learny-technologies/example-docs",
                        "role": "source",
                    },
                    {
                        "id": "core",
                        "repository_id": "1",
                        "url": "https://github.com/learny-technologies/example",
                        "role": "component-source",
                    },
                ],
                "components": [{"id": "api", "repository": "core", "state": "managed"}],
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
                            "id": "backend",
                            "repository": "core",
                            "repository_id": "1",
                            "components": ["api"],
                            "environments": ["dev"],
                            "automation_revision": "a" * 40,
                            "definition": "deploy/runtime.yml",
                        }
                    ]
                },
            }
            path = root / "project.yaml"
            path.write_text(yaml.safe_dump(document))
            validate_product(path)


class DeliveryContractTests(unittest.TestCase):
    def test_runtime_delivery_accepts_only_v3_record(self) -> None:
        self.assertEqual(
            record_reference(record(), delivery_required=True)["contract"], "v3"
        )
        with self.assertRaisesRegex(
            ContractError, "requires execution record contract v3"
        ):
            record_reference(record("v2"), delivery_required=True)

    def plan_args(
        self, root: Path, revision: str, **overrides: object
    ) -> SimpleNamespace:
        images = {"api": "ghcr.io/learny-technologies/example@sha256:" + "c" * 64}
        record_value = record()
        record_value["revision"] = revision
        record_value["content_digest"] = hashlib.sha256(
            (root / record_value["path"]).read_bytes()
        ).hexdigest()
        values: dict[str, object] = {
            "source_root": root,
            "delivery_root": root,
            "automation_root": ROOT,
            "source_sha": revision,
            "delivery_revision": revision,
            "automation_revision": "d" * 40,
            "pipeline_id": "backend",
            "environment": "dev",
            "images_json": json.dumps(images),
            "artifact_verification_json": "{}",
            "migration_heads_json": "[]",
            "execution_record_json": json.dumps(record_value),
            "execution_record_root": root,
            "operation_type": "promotion",
            "reason": "Deploy exact validated source",
            "actor": "averdalv",
            "actor_id": "16990544",
            "run_id": "1",
            "run_attempt": "1",
            "run_url": "https://github.com/example/actions/runs/1",
            "expected_fingerprint": "",
            "staging_evidence_json": "{}",
            "rollback_compatible": True,
            "break_glass": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_plan_fingerprint_ignores_run_identity_but_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = initialize_runtime_repo(root)
            first = plan_document(self.plan_args(root, revision))
            second = plan_document(
                self.plan_args(root, revision, run_id="2", run_attempt="3")
            )
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            with self.assertRaisesRegex(ContractError, "fingerprint changed"):
                plan_document(
                    self.plan_args(
                        root,
                        revision,
                        expected_fingerprint="f" * 64,
                    )
                )

    def test_plan_rejects_execution_record_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = initialize_runtime_repo(root)
            args = self.plan_args(root, revision)
            reference = json.loads(args.execution_record_json)
            reference["content_digest"] = "f" * 64
            args.execution_record_json = json.dumps(reference)
            with self.assertRaisesRegex(ContractError, "content digest"):
                plan_document(args)

    def test_rollback_preserves_original_artifact_publisher_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = initialize_runtime_repo(root)
            binding = {
                "api": {
                    "mode": "registry",
                    "publisher": {
                        "repository": "learny-technologies/.github",
                        "workflow": ".github/workflows/reusable-oci-publish.yml",
                        "revision": "e" * 40,
                    },
                }
            }
            plan = plan_document(
                self.plan_args(
                    root,
                    revision,
                    operation_type="rollback",
                    artifact_verification_json=json.dumps(binding),
                )
            )
            self.assertEqual(plan["artifact_verification"], binding)

    def test_production_enforces_actor_and_staging_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = initialize_runtime_repo(root)
            images = {"api": "ghcr.io/learny-technologies/example@sha256:" + "c" * 64}
            staging = {
                "contract": "learny.delivery/v1",
                "source_revision": revision,
                "images": images,
                "health": "healthy",
            }
            approved = plan_document(
                self.plan_args(
                    root,
                    revision,
                    environment="production",
                    staging_evidence_json=json.dumps(staging),
                )
            )
            self.assertEqual(approved["actor"]["id"], 16990544)
            with self.assertRaisesRegex(ContractError, "not authorized"):
                plan_document(
                    self.plan_args(
                        root,
                        revision,
                        environment="production",
                        actor_id="42",
                        staging_evidence_json=json.dumps(staging),
                    )
                )

    def test_production_class_environment_enforces_actor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = initialize_runtime_repo(root)
            images = {"api": "ghcr.io/learny-technologies/example@sha256:" + "c" * 64}
            staging = {
                "contract": "learny.delivery/v1",
                "source_revision": revision,
                "images": images,
                "health": "healthy",
            }
            for environment in sorted(PRODUCTION_ENVIRONMENTS):
                with self.subTest(environment=environment):
                    with self.assertRaisesRegex(ContractError, "not authorized"):
                        plan_document(
                            self.plan_args(
                                root,
                                revision,
                                environment=environment,
                                actor_id="42",
                                staging_evidence_json=json.dumps(staging),
                            )
                        )

    def test_production_previous_healthy_rollback_does_not_require_staging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = initialize_runtime_repo(root)
            rollback = plan_document(
                self.plan_args(
                    root,
                    revision,
                    environment="production",
                    operation_type="rollback",
                )
            )
            self.assertEqual(rollback["operation_type"], "rollback")

    def test_result_rejects_unbounded_payload(self) -> None:
        plan = {
            "source_revision": "a" * 40,
            "images": {"api": "ghcr.io/learny-technologies/example@sha256:" + "c" * 64},
            "migration_heads": [],
            "rollback_compatible": True,
        }
        result = {
            "contract": "learny.delivery-result/v1",
            "status": "succeeded",
            "health": "healthy",
            "source_revision": "a" * 40,
            "images": plan["images"],
            "migration_heads": [],
            "rollback_eligible": True,
            "evidence": {"provider_response": {"secret": "value"}},
        }
        with self.assertRaisesRegex(ContractError, "bounded scalar"):
            validate_result(result, plan)

        result["evidence"] = {"safe_key": "x" * 257}
        with self.assertRaisesRegex(ContractError, "bounded scalar"):
            validate_result(result, plan)

    def test_result_accepts_canonical_executor_evidence_keys(self) -> None:
        plan = {
            "source_revision": "a" * 40,
            "images": {"api": "ghcr.io/learny-technologies/example@sha256:" + "c" * 64},
            "migration_heads": [],
            "rollback_compatible": True,
        }
        result = {
            "contract": "learny.delivery-result/v1",
            "status": "succeeded",
            "health": "healthy",
            "source_revision": plan["source_revision"],
            "images": plan["images"],
            "migration_heads": [],
            "rollback_eligible": True,
            "evidence": {
                "source_verified": True,
                "artifact_count": 1,
                "migration_heads_verified": True,
                "provenance_workflow": "learny-technologies/.github/reusable-oci-publish.yml",
            },
        }

        self.assertEqual(validate_result(result, plan), result)


class RegistryEvidenceTests(unittest.TestCase):
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
            (ROOT / "tests/fixtures/buildkit-v1-provenance.json").read_text()
        )
        with mock.patch(
            "verify_registry_evidence.registry_evidence",
            return_value=(labels, provenance, "SPDX-2.3"),
        ):
            verify_registry_evidence(
                image="ghcr.io/learny-technologies/control-plane-api@sha256:"
                + "c" * 64,
                repository=repository,
                source_revision=source_revision,
                automation_revision=automation_revision,
            )

    def test_registry_evidence_fails_closed_on_revision_drift(self) -> None:
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
                        "vcs": {"revision": "a" * 40, "source": source_url}
                    }
                },
            },
        }
        with (
            mock.patch(
                "verify_registry_evidence.registry_evidence",
                return_value=(labels, provenance, "SPDX-2.3"),
            ),
            self.assertRaisesRegex(VerificationError, "does not match"),
        ):
            verify_registry_evidence(
                image="ghcr.io/learny-technologies/example@sha256:" + "c" * 64,
                repository=repository,
                source_revision="a" * 40,
                automation_revision="b" * 40,
            )


class DeploymentSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "skills/request-product-deployment/scripts/request_deployment.py"
        spec = importlib.util.spec_from_file_location("request_deployment", path)
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_environment_aliases_are_canonical(self) -> None:
        self.assertEqual(self.module.normalize_environment("stage"), "staging")
        self.assertEqual(self.module.normalize_environment("prod"), "production")

    def test_non_main_staging_source_fails_before_dispatch(self) -> None:
        with mock.patch.object(
            self.module,
            "command",
            side_effect=self.module.DeliveryError("not an ancestor"),
        ):
            with self.assertRaisesRegex(
                self.module.DeliveryError, "reachable from protected main"
            ):
                self.module.require_main_eligible(Path("."), "a" * 40, "staging")

    def test_publication_fingerprint_is_deterministic(self) -> None:
        reference = record_reference(record(), delivery_required=True)
        first = self.module.publication_fingerprint(
            "learny-technologies/example",
            "a" * 40,
            ["web", "api"],
            "b" * 40,
            reference,
        )
        second = self.module.publication_fingerprint(
            "learny-technologies/example",
            "a" * 40,
            ["api", "web"],
            "b" * 40,
            reference,
        )
        self.assertEqual(first, second)

    def test_reusable_artifact_preserves_its_original_publisher_revision(self) -> None:
        source = (ROOT / "skills/request-product-deployment/scripts/request_deployment.py").read_text()
        self.assertIn("release = release_artifact(repository, component, source_sha)", source)
        self.assertIn('"revision": release["automation_revision"]', source)
        self.assertIn("else artifact_verification", source)

    def test_reusable_release_requires_a_successful_trusted_publication_run(self) -> None:
        artifact = {"workflow_run": {"id": 42}}
        run = {
            "id": 42,
            "repository": {"full_name": "learny-technologies/example"},
            "head_repository": {"full_name": "learny-technologies/example"},
            "path": ".github/workflows/image.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "status": "completed",
            "conclusion": "success",
        }
        with mock.patch.object(self.module, "gh_json", return_value=run):
            self.assertTrue(
                self.module.trusted_publication_run(
                    "learny-technologies/example", artifact
                )
            )
        run["conclusion"] = "failure"
        with mock.patch.object(self.module, "gh_json", return_value=run):
            self.assertFalse(
                self.module.trusted_publication_run(
                    "learny-technologies/example", artifact
                )
            )
        run["conclusion"] = "success"
        run["head_branch"] = "agent/untrusted"
        with mock.patch.object(self.module, "gh_json", return_value=run):
            self.assertFalse(
                self.module.trusted_publication_run(
                    "learny-technologies/example", artifact
                )
            )

    def test_reusable_release_rechecks_frozen_record_content(self) -> None:
        content = (
            "---\n"
            "record_contract: v3\n"
            "delivery_contract: github-actions/v1\n"
            "status: frozen\n"
            "linked_to: TASK-A8C05070-DFA6-4EB4-9183-EF948BEB3FF5\n"
            "target: production_release\n"
            "---\n"
        )
        reference = record()
        reference["content_digest"] = hashlib.sha256(content.encode()).hexdigest()
        with mock.patch.object(
            self.module,
            "gh_json",
            return_value={"content": base64.b64encode(content.encode()).decode()},
        ):
            self.assertTrue(self.module.frozen_record_content_matches(reference))
        reference["content_digest"] = "0" * 64
        with mock.patch.object(
            self.module,
            "gh_json",
            return_value={"content": base64.b64encode(content.encode()).decode()},
        ):
            self.assertFalse(self.module.frozen_record_content_matches(reference))

    def test_reusable_release_accepts_dev_target(self) -> None:
        content = (
            "---\n"
            "record_contract: v3\n"
            "delivery_contract: github-actions/v1\n"
            "status: frozen\n"
            "linked_to: TASK-A8C05070-DFA6-4EB4-9183-EF948BEB3FF5\n"
            "target: dev_release\n"
            "---\n"
        )
        reference = record()
        reference["content_digest"] = hashlib.sha256(content.encode()).hexdigest()
        with mock.patch.object(
            self.module,
            "gh_json",
            return_value={"content": base64.b64encode(content.encode()).decode()},
        ):
            self.assertTrue(self.module.frozen_record_content_matches(reference))

    def test_healthy_deployments_uses_only_latest_status(self) -> None:
        deployment = {
            "id": 17,
            "payload": {
                "contract": "learny.delivery/v1",
                "pipeline_id": "backend",
                "source_revision": "a" * 40,
                "images": {"api": "ghcr.io/example/api@sha256:" + "b" * 64},
            },
        }
        statuses = [
            {"id": 1, "created_at": "2026-08-02T10:00:00Z", "state": "success"},
            {"id": 2, "created_at": "2026-08-02T11:00:00Z", "state": "failure"},
        ]
        with mock.patch.object(
            self.module, "gh_json", side_effect=[[deployment], statuses]
        ):
            self.assertEqual(
                self.module.healthy_deployments(
                    "learny-technologies/example", "staging", "backend"
                ),
                [],
            )

    def test_rollback_candidates_require_executor_eligibility(self) -> None:
        deployment = {
            "id": 19,
            "payload": {
                "contract": "learny.delivery/v1",
                "pipeline_id": "backend",
                "source_revision": "a" * 40,
                "images": {"api": "ghcr.io/example/api@sha256:" + "b" * 64},
                "rollback_compatible": False,
                "rollback_eligible": True,
                "artifact_verification": {"api": {}},
            },
        }
        statuses = [{"id": 1, "created_at": "2026-08-02T10:00:00Z", "state": "success"}]
        with mock.patch.object(
            self.module, "gh_json", side_effect=[[deployment], statuses]
        ):
            self.assertEqual(
                self.module.healthy_deployments(
                    "learny-technologies/example",
                    "production",
                    "backend",
                    rollback_only=True,
                ),
                [],
            )

    def test_previous_healthy_requires_distinct_release_identity(self) -> None:
        image = "ghcr.io/example/api@sha256:" + "b" * 64
        deployments = [
            {"source_revision": "a" * 40, "images": {"api": image}},
            {"source_revision": "a" * 40, "images": {"api": image}},
        ]
        args = SimpleNamespace(
            repository_root=Path("."), environment="dev", pipeline="backend"
        )
        with (
            mock.patch.object(
                self.module,
                "repository_name",
                return_value="learny-technologies/example",
            ),
            mock.patch.object(
                self.module, "healthy_deployments", return_value=deployments
            ),
            self.assertRaisesRegex(self.module.DeliveryError, "no previous healthy"),
        ):
            self.module.previous_healthy(args)

    def test_skill_has_no_control_plane_delivery_path(self) -> None:
        root = ROOT / "skills/request-product-deployment"
        combined = "\n".join(
            path.read_text()
            for path in root.rglob("*.*")
            if path.suffix in {".md", ".py", ".yaml"}
        )
        self.assertNotIn("controlpctl", combined)
        self.assertNotIn("/v1/deployments", combined)


if __name__ == "__main__":
    unittest.main()
