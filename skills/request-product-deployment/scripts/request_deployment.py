#!/usr/bin/env python3
"""Plan and dispatch immutable Learny GitHub Actions deployments."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

try:
    import jsonschema  # noqa: F401
    import yaml
except ModuleNotFoundError:
    runtime = (
        Path(tempfile.gettempdir())
        / f"learny-deployment-skill-{sys.version_info.major}{sys.version_info.minor}"
    )
    runtime_python = runtime / "bin" / "python"
    if not runtime_python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True)
        subprocess.run(
            [
                str(runtime_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(REPO_ROOT / "requirements-dev.txt"),
            ],
            check=True,
        )
    os.execv(str(runtime_python), [str(runtime_python), __file__, *sys.argv[1:]])
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from delivery_contract import (  # noqa: E402
    ContractError,
    canonical,
    plan_document,
    record_reference,
    sha256,
)

SHA = re.compile(r"^[0-9a-f]{40}$")
ENVIRONMENT_ALIASES = {"stage": "staging", "prod": "production"}


class DeliveryError(RuntimeError):
    pass


def command(
    args: list[str], *, cwd: Path | None = None, stdin: bytes | None = None
) -> bytes:
    completed = subprocess.run(
        args,
        cwd=cwd,
        input=stdin,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode(errors="replace").strip()
        raise DeliveryError(message or f"command failed: {' '.join(args[:3])}")
    return completed.stdout


def text(args: list[str], *, cwd: Path | None = None) -> str:
    return command(args, cwd=cwd).decode().strip()


def gh_json(args: list[str]) -> object:
    output = command(["gh", "api", *args])
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeliveryError("GitHub returned invalid JSON") from exc


def normalize_environment(value: str) -> str:
    normalized = ENVIRONMENT_ALIASES.get(value, value)
    if normalized not in {"dev", "staging", "production"}:
        raise DeliveryError("environment must be dev, staging, or production")
    return normalized


def repository_name(root: Path) -> str:
    remote = text(["git", "remote", "get-url", "origin"], cwd=root)
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
    if match is None:
        raise DeliveryError("repository root is not a GitHub checkout")
    return match.group(1)


def exact_source(root: Path, requested: str | None) -> str:
    command(["git", "fetch", "--prune", "origin"], cwd=root)
    if requested is None:
        return text(["git", "rev-parse", "origin/main"], cwd=root).lower()
    if SHA.fullmatch(requested) is None:
        raise DeliveryError("source_sha must be an exact 40-character Git SHA")
    try:
        command(["git", "cat-file", "-e", f"{requested}^{{commit}}"], cwd=root)
    except DeliveryError:
        command(["git", "fetch", "origin", requested], cwd=root)
    remote_refs = text(
        ["git", "for-each-ref", "--contains", requested, "refs/remotes/origin"],
        cwd=root,
    )
    if not remote_refs:
        raise DeliveryError("source SHA is not reachable from a remote repository ref")
    return requested


def require_main_eligible(root: Path, source_sha: str, environment: str) -> None:
    if environment not in {"staging", "production"}:
        return
    try:
        command(
            ["git", "merge-base", "--is-ancestor", source_sha, "origin/main"],
            cwd=root,
        )
    except DeliveryError as exc:
        raise DeliveryError(
            f"{environment} requires a source SHA reachable from protected main"
        ) from exc


def record_document(path: Path) -> tuple[dict[str, str], Path]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise DeliveryError("execution record does not exist")
    root = Path(text(["git", "rev-parse", "--show-toplevel"], cwd=path.parent))
    content = path.read_text()
    fields: dict[str, str] = {}
    for name in (
        "linked_to",
        "record_contract",
        "delivery_contract",
        "status",
        "target",
    ):
        match = re.search(rf"^{name}:\s*(\S+)\s*$", content, re.MULTILINE)
        if match is not None:
            fields[name] = match.group(1)
    if fields.get("status") != "frozen":
        raise DeliveryError("deployment requires a frozen execution record")
    relative_path = str(path.relative_to(root))
    command(["git", "fetch", "--prune", "origin", "main"], cwd=root)
    revision = text(
        ["git", "log", "-1", "--format=%H", "--", relative_path], cwd=root
    ).lower()
    if SHA.fullmatch(revision) is None:
        raise DeliveryError("execution record has no committed revision")
    command(["git", "merge-base", "--is-ancestor", revision, "origin/main"], cwd=root)
    committed = command(["git", "show", f"{revision}:{relative_path}"], cwd=root)
    if committed != content.encode():
        raise DeliveryError("execution record differs from its frozen revision")
    reference = {
        "contract": fields.get("record_contract", ""),
        "delivery_contract": fields.get("delivery_contract", ""),
        "task_id": fields.get("linked_to", ""),
        "repository": repository_name(root),
        "path": relative_path,
        "revision": revision,
        "content_digest": hashlib.sha256(content.encode()).hexdigest(),
    }
    return record_reference(reference, delivery_required=True), root


def manifest_at(root: Path, revision: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(
            command(["git", "show", f"{revision}:automation.yaml"], cwd=root)
        )
    except yaml.YAMLError as exc:
        raise DeliveryError("automation.yaml is invalid") from exc
    if not isinstance(value, dict):
        raise DeliveryError("automation.yaml is invalid")
    return value


def pipeline(manifest: dict[str, Any], pipeline_id: str) -> dict[str, Any]:
    pipelines = manifest.get("delivery", {}).get("pipelines", [])
    matches = [item for item in pipelines if item.get("id") == pipeline_id]
    if len(matches) != 1:
        raise DeliveryError("delivery pipeline is missing or ambiguous")
    return matches[0]


def automation_revision(root: Path, delivery_revision: str) -> str:
    workflow = command(
        ["git", "show", f"{delivery_revision}:.github/workflows/deploy.yml"], cwd=root
    ).decode()
    matches = re.findall(r"reusable-deploy\.yml@([0-9a-f]{40})", workflow)
    if len(set(matches)) != 1:
        raise DeliveryError(
            "deploy workflow does not pin one shared automation revision"
        )
    return matches[0]


def release_artifact(
    repository: str,
    component: str,
    source_sha: str,
    automation: str,
    execution_record: dict[str, str],
) -> dict[str, Any] | None:
    name = f"release-{component}-{source_sha}"
    response = gh_json(
        [
            "--method",
            "GET",
            f"repos/{repository}/actions/artifacts",
            "-f",
            f"name={name}",
            "-f",
            "per_page=100",
        ]
    )
    artifacts = response.get("artifacts", []) if isinstance(response, dict) else []
    for artifact in artifacts:
        if artifact.get("expired") is True:
            continue
        archive = command(["gh", "api", str(artifact["archive_download_url"])])
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            document = json.loads(zipped.read("release.json"))
        if (
            document.get("contract") == "learny.release/v1"
            and document.get("repository") == repository
            and document.get("source_revision") == source_sha
            and document.get("automation_revision") == automation
            and document.get("execution_record") == execution_record
            and component in document.get("components", {})
        ):
            return document
    return None


def checkout(root: Path, revision: str, target: Path) -> None:
    command(["git", "clone", "--quiet", "--no-hardlinks", str(root), str(target)])
    command(["git", "checkout", "--quiet", "--detach", revision], cwd=target)


def actor() -> tuple[str, str]:
    value = gh_json(["user"])
    if not isinstance(value, dict) or not isinstance(value.get("id"), int):
        raise DeliveryError("GitHub authentication is unavailable")
    return str(value["login"]), str(value["id"])


def publication_fingerprint(
    repository: str,
    source_sha: str,
    components: list[str],
    automation: str,
    record: dict[str, str],
) -> str:
    return sha256(
        canonical(
            {
                "contract": "learny.publication-request/v1",
                "repository": repository,
                "source_revision": source_sha,
                "components": sorted(components),
                "automation_revision": automation,
                "execution_record": record,
            }
        )
    )


def deployment_plan(
    args: argparse.Namespace,
    *,
    operation_type: str,
    rollback_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = args.repository_root.resolve()
    repository = repository_name(root)
    environment = normalize_environment(args.environment)
    source_sha = (
        str(rollback_payload["source_revision"])
        if rollback_payload is not None
        else exact_source(root, args.source_sha)
    )
    require_main_eligible(root, source_sha, environment)
    delivery_revision = text(["git", "rev-parse", "origin/main"], cwd=root).lower()
    manifest = manifest_at(root, delivery_revision)
    selected = pipeline(manifest, args.pipeline)
    if environment not in selected.get("environments", []):
        raise DeliveryError("pipeline does not manage the requested environment")
    components = [str(item) for item in selected["components"]]
    automation = automation_revision(root, delivery_revision)
    record, record_repository_root = record_document(args.execution_record)
    images: dict[str, str] = {}
    missing: list[str] = []
    if rollback_payload is not None:
        images = {
            str(key): str(value) for key, value in rollback_payload["images"].items()
        }
    else:
        for component in components:
            release = release_artifact(
                repository, component, source_sha, automation, record
            )
            if release is None:
                missing.append(component)
            else:
                images.update(release["components"])
    publication = publication_fingerprint(
        repository, source_sha, components, automation, record
    )
    if missing:
        return {
            "state": "requires_publication",
            "contract": "learny.publication-request/v1",
            "repository": repository,
            "source_revision": source_sha,
            "components": components,
            "missing_components": missing,
            "automation_revision": automation,
            "execution_record": record,
            "fingerprint": publication,
        }
    login, actor_id = actor()
    staging = args.staging_evidence_json
    if (
        environment == "production"
        and operation_type == "promotion"
        and not args.break_glass
    ):
        supplied_staging = json.loads(staging)
        if supplied_staging == {}:
            matched = next(
                (
                    item
                    for item in healthy_deployments(
                        repository, "staging", args.pipeline
                    )
                    if item.get("source_revision") == source_sha
                    and item.get("images") == images
                ),
                None,
            )
            if matched is None:
                raise DeliveryError(
                    "production requires the same immutable digest to be healthy in staging"
                )
            staging = json.dumps(
                {
                    "contract": "learny.delivery/v1",
                    "source_revision": source_sha,
                    "images": images,
                    "health": "healthy",
                    "deployment_id": matched["deployment_id"],
                },
                separators=(",", ":"),
            )
    if rollback_payload is not None:
        heads = rollback_payload.get("migration_heads", [])
    else:
        heads = json.loads(args.migration_heads_json)
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        source_root = temp / "source"
        delivery_root = temp / "delivery"
        automation_root = temp / "automation"
        execution_record_root = temp / "execution-record"
        checkout(root, source_sha, source_root)
        checkout(root, delivery_revision, delivery_root)
        checkout(REPO_ROOT, automation, automation_root)
        checkout(record_repository_root, record["revision"], execution_record_root)
        namespace = SimpleNamespace(
            source_root=source_root,
            delivery_root=delivery_root,
            automation_root=automation_root,
            source_sha=source_sha,
            delivery_revision=delivery_revision,
            automation_revision=automation,
            pipeline_id=args.pipeline,
            environment=environment,
            images_json=json.dumps(images, separators=(",", ":")),
            migration_heads_json=json.dumps(heads, separators=(",", ":")),
            execution_record_json=json.dumps(record, separators=(",", ":")),
            execution_record_root=execution_record_root,
            operation_type=operation_type,
            reason=args.reason,
            actor=login,
            actor_id=actor_id,
            run_id="0",
            run_attempt="0",
            run_url="pending",
            expected_fingerprint="",
            staging_evidence_json=staging,
            rollback_compatible=bool(args.rollback_compatible),
            break_glass=bool(args.break_glass),
        )
        plan = plan_document(namespace)
    plan["state"] = "ready"
    plan["publication_fingerprint"] = publication
    plan["staging_evidence"] = json.loads(staging)
    return plan


def dispatch(repository: str, workflow: str, inputs: dict[str, str]) -> int:
    before = datetime.now(UTC)
    body = json.dumps({"ref": "main", "inputs": inputs}, separators=(",", ":")).encode()
    command(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repository}/actions/workflows/{workflow}/dispatches",
            "--input",
            "-",
        ],
        stdin=body,
    )
    title = (
        f"Publish {inputs['component_id']} @ {inputs['source_sha']}"
        if workflow == "image.yml"
        else f"Deploy {inputs['pipeline_id']} to {inputs['environment']} @ {inputs['source_sha']}"
    )
    for _ in range(30):
        response = gh_json(
            [
                "--method",
                "GET",
                f"repos/{repository}/actions/workflows/{workflow}/runs",
                "-f",
                "event=workflow_dispatch",
                "-f",
                "branch=main",
                "-f",
                "per_page=20",
            ]
        )
        runs = response.get("workflow_runs", []) if isinstance(response, dict) else []
        for run in runs:
            created = datetime.fromisoformat(
                str(run["created_at"]).replace("Z", "+00:00")
            )
            if run.get("display_title") == title and created >= before:
                return int(run["id"])
        time.sleep(2)
    raise DeliveryError("timed out locating the dispatched GitHub workflow run")


def watch(repository: str, run_id: int) -> dict[str, Any]:
    command(["gh", "run", "watch", str(run_id), "-R", repository])
    value = json.loads(
        command(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "-R",
                repository,
                "--json",
                "databaseId,status,conclusion,url,headSha,displayTitle",
            ]
        )
    )
    return value


def deployment_evidence(repository: str, run_id: int) -> dict[str, Any] | None:
    artifacts = gh_json(
        [
            "--method",
            "GET",
            f"repos/{repository}/actions/runs/{run_id}/artifacts",
            "-f",
            "per_page=100",
        ]
    )
    values = artifacts.get("artifacts", []) if isinstance(artifacts, dict) else []
    matches = [
        item
        for item in values
        if isinstance(item.get("name"), str)
        and item["name"].startswith("deployment-")
        and item.get("expired") is not True
    ]
    if len(matches) != 1:
        return None
    archive = command(["gh", "api", str(matches[0]["archive_download_url"])])
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        try:
            plan = json.loads(zipped.read(".deployment-plan.json"))
            result = json.loads(zipped.read(".deployment-result.json"))
        except KeyError as exc:
            raise DeliveryError("deployment evidence artifact is incomplete") from exc
    deployments = gh_json(
        [
            "--method",
            "GET",
            f"repos/{repository}/deployments",
            "-f",
            f"environment={plan['environment_id']}",
            "-f",
            f"ref={plan['source_revision']}",
            "-f",
            "per_page=100",
        ]
    )
    deployment_id = None
    for deployment in deployments if isinstance(deployments, list) else []:
        payload = deployment.get("payload")
        if isinstance(payload, dict) and payload.get("fingerprint") == plan.get(
            "fingerprint"
        ):
            deployment_id = deployment.get("id")
            break
    return {
        "operation_id": plan.get("operation_id"),
        "source_revision": plan.get("source_revision"),
        "images": plan.get("images"),
        "environment": plan.get("environment_id"),
        "pipeline": plan.get("pipeline_id"),
        "fingerprint": plan.get("fingerprint"),
        "status": result.get("status"),
        "health": result.get("health"),
        "failure_code": result.get("failure_code"),
        "deployment_id": deployment_id,
        "deployment_url": (
            f"https://github.com/{repository}/deployments/{plan['environment_id']}"
            if deployment_id is not None
            else None
        ),
    }


def status(repository: str, run_id: int, *, wait: bool) -> dict[str, Any]:
    run = (
        watch(repository, run_id)
        if wait
        else json.loads(
            command(
                [
                    "gh",
                    "run",
                    "view",
                    str(run_id),
                    "-R",
                    repository,
                    "--json",
                    "databaseId,status,conclusion,url,headSha,displayTitle",
                ]
            )
        )
    )
    return {"run": run, "deployment": deployment_evidence(repository, run_id)}


def publish(args: argparse.Namespace) -> dict[str, Any]:
    plan = deployment_plan(args, operation_type="promotion")
    if plan.get("state") != "requires_publication":
        return {"state": "already_published", "plan": plan}
    if args.expected_fingerprint != plan["fingerprint"]:
        raise DeliveryError("publication plan changed after confirmation")
    runs = []
    record_json = json.dumps(plan["execution_record"], separators=(",", ":"))
    for component in plan["missing_components"]:
        run_id = dispatch(
            plan["repository"],
            "image.yml",
            {
                "source_sha": plan["source_revision"],
                "component_id": component,
                "execution_record_json": record_json,
            },
        )
        runs.append(watch(plan["repository"], run_id))
    return {"state": "published", "runs": runs}


def promote(args: argparse.Namespace, *, rollback: bool = False) -> dict[str, Any]:
    rollback_payload = previous_healthy(args) if rollback else None
    plan = deployment_plan(
        args,
        operation_type="rollback" if rollback else "promotion",
        rollback_payload=rollback_payload,
    )
    if plan.get("state") != "ready":
        raise DeliveryError("immutable artifacts must be published before deployment")
    if args.expected_fingerprint != plan["fingerprint"]:
        raise DeliveryError("deployment plan changed after confirmation")
    inputs = {
        "source_sha": plan["source_revision"],
        "environment": plan["environment_id"],
        "pipeline_id": plan["pipeline_id"],
        "images_json": json.dumps(plan["images"], separators=(",", ":")),
        "migration_heads_json": json.dumps(
            plan["migration_heads"], separators=(",", ":")
        ),
        "execution_record_json": json.dumps(
            plan["execution_record"], separators=(",", ":")
        ),
        "expected_fingerprint": plan["fingerprint"],
        "reason": plan["reason"],
        "operation_type": plan["operation_type"],
        "delivery_context_json": json.dumps(
            {
                "rollback_compatible": bool(args.rollback_compatible),
                "staging_evidence": plan["staging_evidence"],
                "break_glass": bool(args.break_glass),
            },
            separators=(",", ":"),
        ),
    }
    run_id = dispatch(plan["repository"], "deploy.yml", inputs)
    return {"state": "dispatched", "run_id": run_id, "plan": plan}


def healthy_deployments(
    repository: str, environment: str, pipeline_id: str
) -> list[dict[str, Any]]:
    deployments = gh_json(
        [
            "--method",
            "GET",
            f"repos/{repository}/deployments",
            "-f",
            f"environment={environment}",
            "-f",
            "per_page=100",
        ]
    )
    healthy: list[dict[str, Any]] = []
    for deployment in deployments if isinstance(deployments, list) else []:
        payload = deployment.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("contract") != "learny.delivery/v1"
        ):
            continue
        if payload.get("pipeline_id") != pipeline_id:
            continue
        statuses = gh_json(
            [
                "--method",
                "GET",
                f"repos/{repository}/deployments/{deployment['id']}/statuses",
                "-f",
                "per_page=20",
            ]
        )
        status_items = statuses if isinstance(statuses, list) else []
        latest = max(
            status_items,
            key=lambda item: (str(item.get("created_at", "")), int(item.get("id", 0))),
            default=None,
        )
        if latest is not None and latest.get("state") == "success":
            healthy.append({**payload, "deployment_id": deployment["id"]})
    return healthy


def previous_healthy(args: argparse.Namespace) -> dict[str, Any]:
    repository = repository_name(args.repository_root.resolve())
    healthy = healthy_deployments(
        repository, normalize_environment(args.environment), args.pipeline
    )
    unique: list[dict[str, Any]] = []
    release_identities: set[bytes] = set()
    for deployment in healthy:
        identity = canonical(
            {
                "source_revision": deployment.get("source_revision"),
                "images": deployment.get("images"),
            }
        )
        if identity not in release_identities:
            release_identities.add(identity)
            unique.append(deployment)
    if len(unique) < 2:
        raise DeliveryError("GitHub has no previous healthy release for this target")
    return unique[1]


def common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--execution-record", type=Path, required=True)
    parser.add_argument("--source-sha")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--migration-heads-json", default="[]")
    parser.add_argument("--staging-evidence-json", default="{}")
    parser.add_argument(
        "--rollback-compatible",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--break-glass", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "publish", "promote", "rollback-plan", "rollback"):
        item = commands.add_parser(name)
        common(item)
        if name in {"publish", "promote", "rollback"}:
            item.add_argument("--expected-fingerprint", required=True)
    status = commands.add_parser("status")
    status.add_argument("--repository", required=True)
    status.add_argument("--run-id", type=int, required=True)
    status.add_argument("--watch", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "plan":
            result = deployment_plan(args, operation_type="promotion")
        elif args.command == "publish":
            result = publish(args)
        elif args.command == "promote":
            result = promote(args)
        elif args.command == "rollback-plan":
            payload = previous_healthy(args)
            result = deployment_plan(
                args, operation_type="rollback", rollback_payload=payload
            )
        elif args.command == "rollback":
            result = promote(args, rollback=True)
        else:
            result = status(args.repository, args.run_id, wait=args.watch)
        print(json.dumps(result, indent=2, sort_keys=True))
    except (
        DeliveryError,
        ContractError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
