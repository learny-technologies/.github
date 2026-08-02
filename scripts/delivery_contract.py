#!/usr/bin/env python3
"""Build and validate immutable GitHub Actions delivery documents."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from validate_automation import ValidationFailure, validate

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE = re.compile(r"^ghcr\.io/learny-technologies/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^learny-technologies/[A-Za-z0-9_.-]+$")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
RECORD_CONTRACT = "v3"
DELIVERY_CONTRACT = "github-actions/v1"
PLAN_CONTRACT = "learny.delivery/v1"


class ContractError(RuntimeError):
    """A delivery document violates the company contract."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(value: str, name: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{name} is not valid JSON") from exc


def require_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise ContractError(f"{name} must be an exact 40-character Git SHA")
    return value


def require_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ContractError(f"{name} is invalid")
    return value


def require_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 440:
        raise ContractError("deployment reason must contain 1-440 characters")
    return value.strip()


def record_reference(value: object, *, delivery_required: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ContractError("execution record reference must be an object")
    required = {
        "contract",
        "delivery_contract",
        "task_id",
        "repository",
        "path",
        "revision",
        "content_digest",
    }
    if set(value) != required:
        raise ContractError("execution record reference has unexpected fields")
    if value.get("contract") != RECORD_CONTRACT:
        raise ContractError("runtime delivery requires execution record contract v3")
    if delivery_required and value.get("delivery_contract") != DELIVERY_CONTRACT:
        raise ContractError(
            "execution record does not authorize GitHub Actions delivery"
        )
    task_id = value.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("TASK-"):
        raise ContractError("execution record task ID is invalid")
    repository = value.get("repository")
    if not isinstance(repository, str) or REPOSITORY.fullmatch(repository) is None:
        raise ContractError("execution record repository is invalid")
    path = value.get("path")
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or ".." in Path(path).parts
        or not path.endswith(".md")
    ):
        raise ContractError("execution record path is invalid")
    revision = require_sha(value.get("revision"), "execution record revision")
    digest = value.get("content_digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ContractError("execution record content digest is invalid")
    return {
        "contract": RECORD_CONTRACT,
        "delivery_contract": str(value["delivery_contract"]),
        "task_id": task_id,
        "repository": repository,
        "path": path,
        "revision": revision,
        "content_digest": digest,
    }


def verify_record_checkout(reference: dict[str, str], root: Path) -> None:
    root = root.resolve()
    if git_head(root) != reference["revision"]:
        raise ContractError("execution record checkout does not match its revision")
    path = root / reference["path"]
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ContractError("execution record is unavailable at its revision") from exc
    if sha256(content) != reference["content_digest"]:
        raise ContractError("execution record content digest does not match")
    text = content.decode(errors="strict")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ContractError("execution record frontmatter is invalid")
    frontmatter = text.split("\n---\n", 1)[0][4:]
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise ContractError("execution record frontmatter is invalid") from exc
    if not isinstance(metadata, dict):
        raise ContractError("execution record frontmatter is invalid")
    expected = {
        "linked_to": reference["task_id"],
        "record_contract": RECORD_CONTRACT,
        "delivery_contract": DELIVERY_CONTRACT,
        "status": "frozen",
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ContractError("execution record is not a frozen v3 delivery record")
    if metadata.get("target") not in {"staging_release", "production_release"}:
        raise ContractError("execution record target cannot authorize runtime delivery")


def image_map(value: object, components: list[str]) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(components):
        raise ContractError("image map must contain exactly the pipeline components")
    normalized: dict[str, str] = {}
    for component in sorted(components):
        image = value.get(component)
        if not isinstance(image, str) or IMAGE.fullmatch(image) is None:
            raise ContractError(
                f"component {component} image is not an immutable Learny digest"
            )
        normalized[component] = image
    return normalized


def artifact_verification_map(
    value: object, components: list[str], automation_revision: str
) -> dict[str, dict[str, object]]:
    if value == {}:
        return {
            component: {
                "mode": "registry",
                "publisher": {
                    "repository": "learny-technologies/.github",
                    "workflow": ".github/workflows/reusable-oci-publish.yml",
                    "revision": automation_revision,
                },
            }
            for component in sorted(components)
        }
    if not isinstance(value, dict) or set(value) != set(components):
        raise ContractError("artifact verification must match the image components")
    normalized: dict[str, dict[str, object]] = {}
    for component in sorted(components):
        item = value[component]
        if not isinstance(item, dict) or set(item) != {"mode", "publisher"}:
            raise ContractError("artifact verification entry is invalid")
        publisher = item.get("publisher")
        if (
            item.get("mode") != "registry"
            or not isinstance(publisher, dict)
            or set(publisher) != {"repository", "workflow", "revision"}
            or publisher.get("repository") != "learny-technologies/.github"
            or publisher.get("workflow") != ".github/workflows/reusable-oci-publish.yml"
        ):
            raise ContractError("artifact publisher binding is invalid")
        normalized[component] = {
            "mode": "registry",
            "publisher": {
                "repository": publisher["repository"],
                "workflow": publisher["workflow"],
                "revision": require_sha(
                    publisher.get("revision"), "artifact publisher revision"
                ),
            },
        }
    return normalized


def migration_heads(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item or len(item) > 160 for item in value
    ):
        raise ContractError("migration heads must be a JSON string array")
    if len(set(value)) != len(value):
        raise ContractError("migration heads must be unique")
    return sorted(value)


def git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip().lower()


def load_pipeline(
    root: Path, pipeline_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = validate(root / "automation.yaml", repository_root=root)
    pipeline = next(
        (
            item
            for item in document["delivery"]["pipelines"]
            if item["id"] == pipeline_id
        ),
        None,
    )
    if pipeline is None:
        raise ContractError(f"unknown delivery pipeline: {pipeline_id}")
    return document, pipeline


def load_authorities(root: Path) -> set[int]:
    path = root / "policies" / "deployment-authorities.yaml"
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError("deployment authority policy is unavailable") from exc
    ids = value.get("production_actor_ids") if isinstance(value, dict) else None
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(item, int) or item <= 0 for item in ids)
    ):
        raise ContractError("production actor policy is invalid")
    return set(ids)


def release_document(args: argparse.Namespace) -> dict[str, object]:
    source = require_sha(args.source_sha, "source SHA")
    automation = require_sha(args.automation_revision, "automation revision")
    component = require_identifier(args.component_id, "component")
    if not isinstance(args.image, str) or not args.image.startswith(
        "ghcr.io/learny-technologies/"
    ):
        raise ContractError("release image is invalid")
    if DIGEST.fullmatch(args.digest) is None:
        raise ContractError("release digest is invalid")
    repository = args.repository
    if REPOSITORY.fullmatch(repository) is None:
        raise ContractError("release repository is invalid")
    record = record_reference(
        read_json(args.execution_record_json, "execution record"),
        delivery_required=False,
    )
    verify_record_checkout(record, args.execution_record_root)
    return {
        "contract": "learny.release/v1",
        "repository": repository,
        "source_revision": source,
        "components": {component: f"{args.image}@{args.digest}"},
        "automation_revision": automation,
        "execution_record": record,
        "publication": {
            "workflow": ".github/workflows/reusable-oci-publish.yml",
            "run_url": args.run_url,
            "reused": bool(args.reused),
            "buildkit_provenance": True,
            "sbom": True,
            "cosign": True,
        },
    }


def plan_document(args: argparse.Namespace) -> dict[str, object]:
    source_root = args.source_root.resolve()
    delivery_root = args.delivery_root.resolve()
    automation_root = args.automation_root.resolve()
    source = require_sha(args.source_sha, "source SHA")
    delivery_revision = require_sha(args.delivery_revision, "delivery revision")
    automation_revision = require_sha(args.automation_revision, "automation revision")
    if git_head(source_root) != source:
        raise ContractError("checked-out release source does not match source SHA")
    if git_head(delivery_root) != delivery_revision:
        raise ContractError(
            "checked-out delivery source does not match delivery revision"
        )
    pipeline_id = require_identifier(args.pipeline_id, "pipeline")
    environment = require_identifier(args.environment, "environment")
    document, pipeline = load_pipeline(delivery_root, pipeline_id)
    if environment not in pipeline["environments"]:
        raise ContractError("pipeline does not authorize the requested environment")
    components = [str(item) for item in pipeline["components"]]
    images = image_map(read_json(args.images_json, "images"), components)
    artifact_verification = artifact_verification_map(
        read_json(args.artifact_verification_json, "artifact verification"),
        components,
        automation_revision,
    )
    heads = migration_heads(read_json(args.migration_heads_json, "migration heads"))
    record = record_reference(
        read_json(args.execution_record_json, "execution record"),
        delivery_required=True,
    )
    verify_record_checkout(record, args.execution_record_root)
    actor_id = int(args.actor_id)
    break_glass = bool(args.break_glass)
    operation_type = args.operation_type
    if operation_type not in {"promotion", "rollback"}:
        raise ContractError("operation type is invalid")
    if environment == "production":
        if actor_id not in load_authorities(automation_root):
            raise ContractError("GitHub actor is not authorized for production")
        staging = read_json(args.staging_evidence_json, "staging evidence")
        if operation_type == "promotion" and not break_glass:
            if (
                not isinstance(staging, dict)
                or staging.get("contract") != PLAN_CONTRACT
            ):
                raise ContractError("production requires successful staging evidence")
            if (
                staging.get("source_revision") != source
                or staging.get("images") != images
            ):
                raise ContractError(
                    "staging evidence does not match production artifacts"
                )
            if staging.get("health") != "healthy":
                raise ContractError("staging evidence is not healthy")
        elif len(args.reason.strip()) < 12:
            raise ContractError("production break-glass requires a meaningful reason")
    definition_path = pipeline.get("definition")
    if not isinstance(definition_path, str):
        raise ContractError("pipeline has no deployment definition")
    definition = source_root / definition_path
    try:
        definition_hash = sha256(definition.read_bytes())
    except OSError as exc:
        raise ContractError("deployment definition is unavailable") from exc
    project_id = document["metadata"].get("project")
    project_id = require_identifier(project_id, "project")
    plan: dict[str, object] = {
        "version": "v1",
        "contract": PLAN_CONTRACT,
        "operation_id": f"github:{args.run_id}:{args.run_attempt}",
        "operation_type": operation_type,
        "project_id": project_id,
        "repository": document["metadata"]["repository"],
        "environment_id": environment,
        "pipeline_id": pipeline_id,
        "source_revision": source,
        "images": images,
        "definition_path": definition_path,
        "definition_hash": definition_hash,
        "migration_heads": heads,
        "rollback_compatible": bool(args.rollback_compatible),
        "source_validation_id": f"github:{record['revision']}",
        "artifact_verification": artifact_verification,
        "executor": {
            "path": pipeline["executor"],
            "revision": delivery_revision,
            "automation_revision": automation_revision,
        },
        "actor": {"login": args.actor, "id": actor_id},
        "execution_record": record,
        "reason": require_reason(args.reason),
        "break_glass": break_glass,
        "run_url": args.run_url,
    }
    fingerprint_payload = {
        key: value
        for key, value in plan.items()
        if key not in {"operation_id", "run_url"}
    }
    plan["fingerprint"] = sha256(canonical(fingerprint_payload))
    if args.expected_fingerprint and args.expected_fingerprint != plan["fingerprint"]:
        raise ContractError("deployment plan fingerprint changed after confirmation")
    return plan


def validate_result(value: object, plan: dict[str, object]) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or value.get("contract") != "learny.delivery-result/v1"
    ):
        raise ContractError("deployment result contract is invalid")
    allowed = {
        "contract",
        "status",
        "health",
        "source_revision",
        "images",
        "migration_heads",
        "rollback_eligible",
        "failure_code",
        "evidence",
    }
    if not set(value).issubset(allowed):
        raise ContractError("deployment result contains unsupported fields")
    if value.get("status") not in {"succeeded", "failed"}:
        raise ContractError("deployment result status is invalid")
    if value.get("health") not in {"healthy", "degraded", "unknown"}:
        raise ContractError("deployment result health is invalid")
    if not isinstance(value.get("rollback_eligible"), bool):
        raise ContractError("deployment result rollback eligibility is invalid")
    if value.get("rollback_eligible") and not plan.get("rollback_compatible"):
        raise ContractError(
            "deployment result cannot make an incompatible release rollbackable"
        )
    if value.get("source_revision") != plan.get("source_revision"):
        raise ContractError("deployment result source revision does not match plan")
    if value.get("images") != plan.get("images"):
        raise ContractError("deployment result images do not match plan")
    if migration_heads(value.get("migration_heads")) != plan.get("migration_heads"):
        raise ContractError("deployment result migration heads do not match plan")
    failure = value.get("failure_code")
    if failure is not None and (
        not isinstance(failure, str) or IDENTIFIER.fullmatch(failure) is None
    ):
        raise ContractError("deployment result failure code is invalid")
    evidence = value.get("evidence", {})
    if (
        not isinstance(evidence, dict)
        or len(evidence) > 20
        or any(
            not isinstance(key, str)
            or IDENTIFIER.fullmatch(key) is None
            or not isinstance(item, (str, int, float, bool, type(None)))
            or (isinstance(item, str) and len(item) > 256)
            for key, item in evidence.items()
        )
    ):
        raise ContractError("deployment result evidence must be bounded scalar data")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser("release")
    release.add_argument("--repository", required=True)
    release.add_argument("--source-sha", required=True)
    release.add_argument("--component-id", required=True)
    release.add_argument("--image", required=True)
    release.add_argument("--digest", required=True)
    release.add_argument("--automation-revision", required=True)
    release.add_argument("--execution-record-json", required=True)
    release.add_argument("--execution-record-root", type=Path, required=True)
    release.add_argument("--run-url", required=True)
    release.add_argument("--reused", action="store_true")
    release.add_argument("--output", type=Path, required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--source-root", type=Path, required=True)
    plan.add_argument("--delivery-root", type=Path, required=True)
    plan.add_argument("--automation-root", type=Path, required=True)
    plan.add_argument("--source-sha", required=True)
    plan.add_argument("--delivery-revision", required=True)
    plan.add_argument("--automation-revision", required=True)
    plan.add_argument("--pipeline-id", required=True)
    plan.add_argument("--environment", required=True)
    plan.add_argument("--images-json", required=True)
    plan.add_argument("--artifact-verification-json", default="{}")
    plan.add_argument("--migration-heads-json", default="[]")
    plan.add_argument("--execution-record-json", required=True)
    plan.add_argument("--execution-record-root", type=Path, required=True)
    plan.add_argument("--operation-type", default="promotion")
    plan.add_argument("--reason", required=True)
    plan.add_argument("--actor", required=True)
    plan.add_argument("--actor-id", required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--run-attempt", required=True)
    plan.add_argument("--run-url", required=True)
    plan.add_argument("--expected-fingerprint", default="")
    plan.add_argument("--staging-evidence-json", default="{}")
    plan.add_argument("--rollback-compatible", action="store_true")
    plan.add_argument("--break-glass", action="store_true")
    plan.add_argument("--output", type=Path, required=True)

    result = commands.add_parser("validate-result")
    result.add_argument("--plan", type=Path, required=True)
    result.add_argument("--result", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "release":
            document = release_document(args)
            args.output.write_bytes(canonical(document) + b"\n")
        elif args.command == "plan":
            document = plan_document(args)
            args.output.write_bytes(canonical(document) + b"\n")
            print(document["fingerprint"])
        else:
            plan = json.loads(args.plan.read_text())
            result = json.loads(args.result.read_text())
            normalized = validate_result(result, plan)
            args.result.write_bytes(canonical(normalized) + b"\n")
    except (
        ContractError,
        ValidationFailure,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
